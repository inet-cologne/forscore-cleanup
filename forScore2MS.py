#!/usr/bin/env python3
"""
forScore2MS_v2.py -- Import forScore .4sb data directly into a MobileSheets SQLite database.

Unlike the v1 scripts (forScore2MS_db.py etc.) this script reads the .4sb file
directly -- no JSON intermediate step required.

Supported input
---------------
  Backup*.4sb  (V02)  -- metadata plist only
  Archiv*.4sb  (V03)  -- metadata plist + embedded PDFs and audio

What is imported (Stage 1)
--------------------------
  Songs         -- every PDF in the library, with full metadata
  Bookmarks     -- each bookmark becomes a virtual song (page-range slice of a PDF)
  Setlists      -- all 110+ setlists with their song membership and display order
  Audio links   -- linked MP3/M4A tracks from '<file>|Tracks' keys
  MIDI presets  -- forScore MIDI presets as MobileSheets Smart Buttons (opt-in)

Output
------
  <input_stem>-2MS/
      mobilesheets.db          fresh MobileSheets database (importable by the app)

Usage
-----
  forScore2MS_v2.py <input.4sb> [-o OUTPUT_DIR] [--dry-run] [-v]
                    [--midi-presets --midi-song SONG_TITLE]
                    [--midi-presets --midi-presets-new-song SONG_TITLE]

Options
-------
  -o OUTPUT_DIR              Output directory  (default: <input-stem>-2MS/ next to input)
  --dry-run                  Parse and report without writing any database
  -v, --verbose              Print each inserted song title
  --midi-presets             Import forScore MIDI presets as Smart Buttons
  --midi-song TITLE          Attach Smart Buttons to this existing song in the DB
  --midi-presets-new-song T  Create a new placeholder song and attach Smart Buttons to it

MIDI preset import
------------------
  forScore stores MIDI presets globally (not per-song) under &SYS;presets.
  Each preset contains one or more MIDI commands (Program Change or CC).

  MobileSheets Smart Buttons are always attached to a specific song.  The
  recommended workflow is to use a dedicated "MIDI settings" song (a PDF with
  button-position guide graphics) as the target:

    --midi-presets --midi-song "Aerophone-Midi-Settings-02"

  Or create a fresh placeholder song automatically:

    --midi-presets --midi-presets-new-song "MIDI Presets"

  Buttons are laid out in a 4-column grid starting at position (100, 100).
  Each Smart Button sends all MIDI commands of its corresponding forScore preset.
"""

import argparse
import gzip
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADER_SIZE = 74   # kept for reference; actual gzip offset is found dynamically
_HEADER_SEARCH_WINDOW = 256

# forScore key encoding: 0-11 = major, 100-111 = minor (repeated in steps of 100)
MAJOR_KEYS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
MINOR_KEYS = ['Cm', 'Dbm', 'Dm', 'Ebm', 'Em', 'Fm', 'F#m', 'Gm', 'Abm', 'Am', 'Bbm', 'Bm']


# ---------------------------------------------------------------------------
# PDF page count helper (macOS only — uses mdls, no external Python libs)
# ---------------------------------------------------------------------------
def get_pdf_page_count(pdf_path: Path) -> int:
    """Return the number of pages in a PDF using macOS Spotlight metadata.
    Returns 0 if the file does not exist or the page count cannot be determined.
    """
    if not pdf_path.exists():
        return 0
    try:
        r = subprocess.run(
            ['mdls', '-name', 'kMDItemNumberOfPages', str(pdf_path)],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.strip().split('=')[-1].strip()
        if val and val != '(null)':
            return int(val)
    except Exception:
        pass
    return 0



# ---------------------------------------------------------------------------
# .4sb reader  (re-used from clean_forscore_bookmarks_v2.py)
# ---------------------------------------------------------------------------
def read_plist_from_4sb(filepath: str) -> dict:
    """
    Read the file header, determine metadata size, decompress the metadata
    gzip block, and return the parsed plist dict.

    The gzip start offset is found dynamically (not assumed to be 74) to
    support both 'Archiv' (74-byte header) and 'Archive' (75-byte header).
    """
    with open(filepath, 'rb') as f:
        window = f.read(_HEADER_SEARCH_WINDOW)

    gz_idx = window.find(b'\x1f\x8b\x08')
    if gz_idx == -1:
        raise ValueError('gzip magic not found in file header area.')

    header = window[:gz_idx].decode('ascii', errors='replace')
    m = re.search(r'(\d{5,12})(Backup|Archiv)', header)
    if not m:
        raise ValueError(f"Cannot parse metadata size from file header: {header!r}")

    metadata_size = int(m.group(1))

    with open(filepath, 'rb') as f:
        f.seek(gz_idx)
        gz_bytes = f.read(metadata_size)

    raw = gzip.decompress(gz_bytes)
    if raw[:8] != b'bplist00':
        raise ValueError("Metadata block is not a binary plist.")
    return plistlib.loads(raw)


# ---------------------------------------------------------------------------
# Key / signature / BPM helpers
# ---------------------------------------------------------------------------
def decode_key(value) -> str | None:
    """Convert a forScore integer key code to a human-readable key name."""
    if value is None or value == '':
        return None
    try:
        k = int(value)
        idx = k % 100
        if 0 <= idx <= 11:
            # Even hundreds -> major, odd hundreds -> minor
            if (k // 100) % 2 == 1:
                return MINOR_KEYS[idx]
            return MAJOR_KEYS[idx]
    except (ValueError, TypeError):
        pass
    return str(value) if value else None


def decode_bpm(value) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def decode_signature(value) -> str | None:
    if value is None or value == '':
        return None
    return str(value)


# ---------------------------------------------------------------------------
# plist extraction helpers
# ---------------------------------------------------------------------------
def extract_songs_and_bookmarks(plist: dict) -> list:
    """
    Build a flat list of song dicts from the plist.

    Three types of entries are produced:

    'bookmark'  — one entry per bookmark inside a multi-song PDF
    'single'    — one entry per standalone single-PDF score (forScore title)
    'pdf'       — one additional entry per distinct PDF file, with
                  title = filename without extension.  This covers both
                  bookmark-host PDFs and single-PDF scores, giving every
                  raw file its own song entry.

    Returns a list of dicts with keys:
        type        - 'bookmark', 'single', or 'pdf'
        filepath    - PDF filename (e.g. 'My Song.pdf')
        title       - display title
        first_page  - 1-based first page (int)
        last_page   - 1-based last page; -1 = whole PDF
        composer    - string or ''
        genre       - string or ''
        keyword     - string or ''
        key         - decoded key string or None
        signature   - string or None
        bpm         - int or None
        identifier  - UUID string from forScore or ''
    """
    songs = []
    bookmark_pdfs: set[str] = set()

    # --- Pass 1: bookmarks ---------------------------------------------------
    for key, value in plist.items():
        if not key.endswith('|bookmarks') or not isinstance(value, list):
            continue
        filepath = key[:-len('|bookmarks')]
        bookmark_pdfs.add(filepath)

        for bm in value:
            if not isinstance(bm, dict):
                continue
            fp = int(bm.get('First Page', 1) or 1)
            lp = int(bm.get('Last Page', fp) or fp)
            if lp == 0:
                lp = fp
            songs.append({
                'type':       'bookmark',
                'filepath':   filepath,
                'title':      bm.get('Title', '') or filepath,
                'first_page': fp,
                'last_page':  lp,
                'composer':   bm.get('Composer', '') or '',
                'genre':      bm.get('Genre', '') or '',
                'keyword':    bm.get('Keyword', '') or '',
                'key':        decode_key(bm.get('Key')),
                'signature':  decode_signature(bm.get('Signature')),
                'bpm':        decode_bpm(bm.get('BPM')),
                'identifier': bm.get('Identifier', '') or '',
            })

    # --- Pass 2: single-PDF scores (not in any bookmark list) ---------------
    for key, value in plist.items():
        if not key.endswith('|title'):
            continue
        filepath = key[:-len('|title')]
        if filepath in bookmark_pdfs:
            continue   # already handled as bookmarks
        if not (filepath.lower().endswith('.pdf') or filepath.lower().endswith('.PDF')):
            continue

        title = value or filepath.rsplit('.', 1)[0]

        songs.append({
            'type':       'single',
            'filepath':   filepath,
            'title':      title,
            'first_page': 1,
            'last_page':  -1,   # -1 = whole PDF
            'composer':   plist.get(f'{filepath}|composer', '') or '',
            'genre':      plist.get(f'{filepath}|genre', '') or '',
            'keyword':    plist.get(f'{filepath}|keywords', '') or '',
            'key':        decode_key(plist.get(f'{filepath}|key')),
            'signature':  decode_signature(plist.get(f'{filepath}|signature')),
            'bpm':        decode_bpm(plist.get(f'{filepath}|bpm')),
            'identifier': '',
        })

    # --- Pass 3: one 'pdf' entry per PDF that hosts bookmarks ---------------
    # For bookmark-host PDFs (e.g. Real Books), we also want a whole-file
    # entry so MobileSheets can open the raw PDF directly.
    # Single-PDF scores are excluded: their 'single' entry already covers the
    # whole file (and typically has the same title as the filename), so adding
    # a 'pdf' entry would create a duplicate.
    for filepath in sorted(bookmark_pdfs):
        stem = filepath.rsplit('.', 1)[0] if '.' in filepath else filepath
        songs.append({
            'type':       'pdf',
            'filepath':   filepath,
            'title':      stem,
            'first_page': 1,
            'last_page':  -1,
            'composer':   plist.get(f'{filepath}|composer', '') or '',
            'genre':      plist.get(f'{filepath}|genre', '') or '',
            'keyword':    plist.get(f'{filepath}|keywords', '') or '',
            'key':        decode_key(plist.get(f'{filepath}|key')),
            'signature':  decode_signature(plist.get(f'{filepath}|signature')),
            'bpm':        decode_bpm(plist.get(f'{filepath}|bpm')),
            'identifier': '',
        })

    return songs


def extract_setlists(plist: dict) -> list:
    """
    Return a list of setlist dicts in display order.

    Each dict has:
        name   - setlist name
        songs  - list of (filepath, title) tuples in setlist order
    """
    ordered_keys = plist.get('&SYS;setlists', []) or []
    setlists = []

    for key in ordered_keys:
        if not isinstance(key, str) or not key.startswith('&SET;'):
            continue
        name    = key[len('&SET;'):]
        entries = plist.get(key, []) or []
        songs   = []
        for entry in entries:
            if isinstance(entry, dict):
                fp    = entry.get('FilePath', '') or ''
                title = entry.get('Title', '') or ''
                if fp or title:
                    songs.append((fp, title))
        setlists.append({'name': name, 'songs': songs})

    # Also include any &SET; keys not listed in &SYS;setlists
    seen_keys = {f'&SET;{sl["name"]}' for sl in setlists}
    for key in plist:
        if key.startswith('&SET;') and key not in seen_keys:
            name    = key[len('&SET;'):]
            entries = plist.get(key, []) or []
            songs   = []
            for entry in entries:
                if isinstance(entry, dict):
                    fp    = entry.get('FilePath', '') or ''
                    title = entry.get('Title', '') or ''
                    if fp or title:
                        songs.append((fp, title))
            setlists.append({'name': name, 'songs': songs})

    return setlists


def extract_audio_links(plist: dict) -> dict:
    """
    Return a dict mapping filepath -> list of audio track dicts.

    Each track dict has:
        title    - track title
        filename - audio filename
    """
    audio = defaultdict(list)
    for key, value in plist.items():
        if not key.endswith('|Tracks'):
            continue
        filepath = key[:-len('|Tracks')]
        if isinstance(value, list):
            for track in value:
                if isinstance(track, dict):
                    t = {
                        'title':    track.get('Song Title', '') or '',
                        'filename': track.get('Song Title', '') or '',   # forScore stores filename in 'Song Title'
                    }
                    audio[filepath].append(t)
        elif isinstance(value, dict):
            t = {
                'title':    value.get('Song Title', '') or '',
                'filename': value.get('Song Title', '') or '',
            }
            audio[filepath].append(t)
    return dict(audio)


# ---------------------------------------------------------------------------
# MIDI preset extraction (forScore → MobileSheets)
# ---------------------------------------------------------------------------

def _resolve_nska(data: bytes) -> list:
    """
    Decode an NSKeyedArchiver bplist containing an NSArray of NSDictionary
    MIDI command objects.  Returns a list of plain Python dicts.
    """
    inner   = plistlib.loads(data)
    objects = inner['$objects']

    def resolve(obj):
        if hasattr(obj, 'data'):           # plistlib.UID
            return resolve(objects[obj.data])
        if isinstance(obj, dict):
            cls_uid   = obj.get('$class')
            classname = ''
            if cls_uid is not None:
                cls_obj   = objects[cls_uid.data]
                classname = cls_obj.get('$classname', '')
            if classname == 'NSArray':
                return [resolve(x) for x in obj.get('NS.objects', [])]
            if classname == 'NSDictionary':
                keys   = [resolve(k) for k in obj.get('NS.keys', [])]
                values = [resolve(v) for v in obj.get('NS.objects', [])]
                return dict(zip(keys, values))
            return {k: resolve(v) for k, v in obj.items() if k != '$class'}
        if isinstance(obj, list):
            return [resolve(x) for x in obj]
        return obj

    top_uid = inner['$top']['root']
    result  = resolve(top_uid)
    if isinstance(result, list):
        return result
    return [result] if result else []


def _hex_to_ms_midi(hex_str: str) -> list[dict]:
    """
    Decode a forScore raw hex MIDI command string (e.g. 'BB167F') into one or
    more MobileSheets SmartButtonMIDI-compatible dicts.

    Returns a list because a single hex string may encode multiple 3-byte
    messages when the string is longer than 6 hex chars.

    Each dict has keys matching SmartButtonMIDI columns:
        CommandType  - 0=ProgramChange, 1=ControlChange
        Channel      - 0-based (0–15)
        MSB          - Bank MSB (PC) or CC number (CC)
        LSB          - Bank LSB (PC) or 0 (CC)
        Value        - PC number (PC) or CC value (CC)
        SendMSB      - 1
        SendLSB      - 1
        SendValue    - 1
        Label        - human-readable description
    """
    s = hex_str.strip()
    if len(s) % 2:
        s = '0' + s
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return []

    results = []
    i = 0
    while i + 2 < len(raw):
        status = raw[i]
        b1     = raw[i + 1]
        b2     = raw[i + 2]
        kind   = status & 0xF0
        ch     = status & 0x0F   # 0-based

        if kind == 0xB0:
            # Control Change
            results.append({
                'CommandType': 1,
                'Channel':     ch,
                'MSB':         b1,   # CC number
                'LSB':         0,
                'Value':       b2,   # CC value
                'SendMSB':     1,
                'SendLSB':     1,
                'SendValue':   1,
                'Label':       f'CC#{b1}={b2} Ch{ch+1}',
            })
        elif kind == 0xC0:
            # Program Change (2-byte message; b2 unused)
            results.append({
                'CommandType': 0,
                'Channel':     ch,
                'MSB':         0,
                'LSB':         0,
                'Value':       b1,
                'SendMSB':     1,
                'SendLSB':     1,
                'SendValue':   1,
                'Label':       f'PC{b1} Ch{ch+1}',
            })
        # other status bytes are silently skipped
        i += 3

    return results


def extract_midi_presets(plist: dict) -> list[dict]:
    """
    Extract forScore MIDI presets from the plist.

    Returns a list of preset dicts, each with:
        title     - preset name (str)
        commands  - list of MS-compatible command dicts (see _hex_to_ms_midi)
    """
    raw_presets = plist.get('&SYS;presets', [])
    if not isinstance(raw_presets, list):
        return []

    presets = []
    for preset in raw_presets:
        if not isinstance(preset, dict):
            continue
        title    = preset.get('title', '') or '(untitled)'
        cmds_raw = preset.get('commands', b'')

        ms_commands: list[dict] = []

        if cmds_raw:
            try:
                decoded = _resolve_nska(cmds_raw)
            except Exception:
                decoded = []

            for cmd in decoded:
                if not isinstance(cmd, dict):
                    continue
                kind = cmd.get('kind', '')

                if kind == 'programChange':
                    ch  = int(cmd.get('channel', 1) or 1) - 1   # forScore is 1-based
                    pc  = int(cmd.get('value', 0) or 0)
                    msb = cmd.get('msb', -1)
                    lsb = cmd.get('lsb', -1)
                    send_msb = 0 if (msb is None or msb < 0) else 1
                    send_lsb = 0 if (lsb is None or lsb < 0) else 1
                    ms_commands.append({
                        'CommandType': 0,
                        'Channel':     max(0, ch),
                        'MSB':         max(0, int(msb)) if send_msb else 0,
                        'LSB':         max(0, int(lsb)) if send_lsb else 0,
                        'Value':       pc,
                        'SendMSB':     send_msb,
                        'SendLSB':     send_lsb,
                        'SendValue':   1,
                        'Label':       f'PC{pc} Ch{max(1,ch+1)}',
                    })

                elif kind == 'hex':
                    hex_val = cmd.get('value', '') or ''
                    ms_commands.extend(_hex_to_ms_midi(hex_val))

        presets.append({'title': title, 'commands': ms_commands})

    return presets


# ---------------------------------------------------------------------------
# MobileSheets DB creation
# ---------------------------------------------------------------------------
MS_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS Defaults(Id INTEGER PRIMARY KEY,Initialized INTEGER);
CREATE TABLE IF NOT EXISTS Songs(Id INTEGER PRIMARY KEY,Title VARCHAR(255),Difficulty INTEGER,Custom VARCHAR(255) DEFAULT '',Custom2 VARCHAR(255) DEFAULT '',LastPage INTEGER,OrientationLock INTEGER,Duration INTEGER,Stars INTEGER DEFAULT 0,VerticalZoom FLOAT DEFAULT 1,SortTitle VARCHAR(255) DEFAULT '',Sharpen INTEGER DEFAULT 0,SharpenLevel INTEGER DEFAULT 4,CreationDate INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0,Keywords VARCHAR(255) DEFAULT '',AutoStartAudio INTEGER,SongId INTEGER);
CREATE TABLE IF NOT EXISTS SongDisplaySettings(Id INTEGER PRIMARY KEY,SongId INTEGER,UseDefaultAdapter INTEGER,PortraitAdapterType INTEGER,LandscapeAdapterType INTEGER,UseDefaultScaleMode INTEGER,PortraitScaleMode INTEGER,LandscapeScaleMode INTEGER);
CREATE INDEX IF NOT EXISTS song_ds_id_idx ON SongDisplaySettings(SongId);
CREATE TABLE IF NOT EXISTS SourceType(Id INTEGER PRIMARY KEY,Type VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS SourceTypeSongs(Id INTEGER PRIMARY KEY,SourceTypeId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS scrtype_type_id_idx ON SourceTypeSongs(SourceTypeId);
CREATE INDEX IF NOT EXISTS srctype_song_id_idx ON SourceTypeSongs(SongId);
CREATE TABLE IF NOT EXISTS CustomGroup(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS CustomGroupSongs(Id INTEGER PRIMARY KEY,GroupId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS cgroup_group_id_idx ON CustomGroupSongs(GroupId);
CREATE INDEX IF NOT EXISTS cgroup_song_id_idx ON CustomGroupSongs(SongId);
CREATE TABLE IF NOT EXISTS Composer(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ComposerSongs(Id INTEGER PRIMARY KEY,ComposerId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS composer_composer_id_idx ON ComposerSongs(ComposerId);
CREATE TABLE IF NOT EXISTS Files(Id INTEGER PRIMARY KEY,SongId INTEGER,Path VARCHAR(255),PageOrder VARCHAR(255),FileSize INTEGER,LastModified INTEGER,Source INTEGER,Type INTEGER,Password VARCHAR(255) DEFAULT '',SourceFilePageCount INTEGER,FileHash INTEGER,Width INTEGER,Height INTEGER);
CREATE INDEX IF NOT EXISTS files_song_id_idx ON Files(SongId);
CREATE TABLE IF NOT EXISTS HalfPagePos(Id INTEGER PRIMARY KEY,SongId INTEGER,Page INTEGER,Position FLOAT);
CREATE INDEX IF NOT EXISTS half_page_song_id_idx ON HalfPagePos(SongId);
CREATE TABLE IF NOT EXISTS Metrics(Id INTEGER PRIMARY KEY,Type INTEGER,EntryId INTEGER,Date INTEGER,TimeVisible INTEGER);
CREATE INDEX IF NOT EXISTS metrics_id_idx ON Metrics(EntryId);
CREATE TABLE IF NOT EXISTS Goals(Id INTEGER PRIMARY KEY,Duration INTEGER,Views INTEGER,Type INTEGER,EntryId INTEGER);
CREATE INDEX IF NOT EXISTS goals_id_idx ON Goals(EntryId);
CREATE TABLE IF NOT EXISTS Books(Id INTEGER PRIMARY KEY,Title VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS BookSongs(Id INTEGER PRIMARY KEY,BookId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS book_book_id_idx ON BookSongs(BookId);
CREATE INDEX IF NOT EXISTS book_song_id_idx ON BookSongs(SongId);
CREATE TABLE IF NOT EXISTS Artists(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ArtistsSongs(Id INTEGER PRIMARY KEY,ArtistId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS artist_artist_id_idx ON ArtistsSongs(ArtistId);
CREATE INDEX IF NOT EXISTS artist_song_id_idx ON ArtistsSongs(SongId);
CREATE TABLE IF NOT EXISTS Genres(Id INTEGER PRIMARY KEY,Type VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS GenresSongs(Id INTEGER PRIMARY KEY,GenreId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS genre_genre_id_idx ON GenresSongs(GenreId);
CREATE INDEX IF NOT EXISTS genre_song_id_idx ON GenresSongs(SongId);
CREATE TABLE IF NOT EXISTS Key(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS KeySongs(Id INTEGER PRIMARY KEY,KeyId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS key_key_id_idx ON KeySongs(KeyId);
CREATE INDEX IF NOT EXISTS key_song_id_idx ON KeySongs(SongId);
CREATE TABLE IF NOT EXISTS Signature(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS SignatureSongs(Id INTEGER PRIMARY KEY,SignatureId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS sig_sig_id_idx ON SignatureSongs(SignatureId);
CREATE INDEX IF NOT EXISTS sig_song_id_idx ON SignatureSongs(SongId);
CREATE TABLE IF NOT EXISTS Years(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS YearsSongs(Id INTEGER PRIMARY KEY,YearId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS year_year_id_idx ON YearsSongs(YearId);
CREATE INDEX IF NOT EXISTS year_song_id_idx ON YearsSongs(SongId);
CREATE TABLE IF NOT EXISTS ZoomPerPage(Id INTEGER PRIMARY KEY,SongId INTEGER,Page INTEGER,Zoom FLOAT,PortPanX FLOAT,PortPanY FLOAT,LandZoom FLOAT,LandPanX FLOAT,LandPanY FLOAT,FirstHalfY INTEGER,SecondHalfY INTEGER);
CREATE INDEX IF NOT EXISTS zoom_song_id_idx ON ZoomPerPage(SongId);
CREATE TABLE IF NOT EXISTS Crop(Id INTEGER PRIMARY KEY,SongId INTEGER,Page INTEGER,Left INTEGER,Top INTEGER,Right INTEGER,Bottom INTEGER,Rotation INTEGER);
CREATE INDEX IF NOT EXISTS crop_song_id_idx ON Crop(SongId);
CREATE TABLE IF NOT EXISTS AutoScroll(Id INTEGER PRIMARY KEY,SongId INTEGER,Behavior INTEGER,PauseDuration INTEGER,Speed INTEGER,FixedDuration INTEGER,ScrollPercent INTEGER,ScrollOnLoad INTEGER,TimeBeforeScroll INTEGER);
CREATE INDEX IF NOT EXISTS auto_scroll_song_id_idx ON AutoScroll(SongId);
CREATE TABLE IF NOT EXISTS MetronomeSettings(Id INTEGER PRIMARY KEY,SongId INTEGER,Sig1 INTEGER,Sig2 INTEGER,Subdivision INTEGER,SoundFX INTEGER,AccentFirst INTEGER,AutoStart INTEGER DEFAULT 0,CountIn INTEGER DEFAULT 0,NumberCount INTEGER DEFAULT 0,AutoTurn INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS metronome_song_id_idx ON MetronomeSettings(SongId);
CREATE TABLE IF NOT EXISTS MetronomeBeatsPerPage(Id INTEGER PRIMARY KEY,SongId INTEGER,Page INTEGER,BeatsPerPage INTEGER);
CREATE INDEX IF NOT EXISTS bpp_song_id_idx ON MetronomeBeatsPerPage(SongId);
CREATE INDEX IF NOT EXISTS bpp_page_idx ON MetronomeBeatsPerPage(Page);
CREATE TABLE IF NOT EXISTS Tempos(Id INTEGER PRIMARY KEY,SongId INTEGER,Tempo INTEGER,TempoIndex INTEGER);
CREATE INDEX IF NOT EXISTS tempo_song_id_idx ON Tempos(SongId);
CREATE TABLE IF NOT EXISTS AudioFiles(Id INTEGER PRIMARY KEY,SongId INTEGER,Title VARCHAR(255),File VARCHAR(255),FileSource INTEGER,StartPos INTEGER,EndPos INTEGER,FileSize INTEGER,LastModified INTEGER,APosition INTEGER DEFAULT -1,BPosition INTEGER DEFAULT -1,ABEnabled INTEGER DEFAULT 0,FullDuration INTEGER DEFAULT 0,Volume FLOAT DEFAULT 0.75,Artist VARCHAR(255) DEFAULT '',PitchShift INTEGER DEFAULT 0,TempoSpeed FLOAT DEFAULT 1.0,AudioFileHash INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS audio_song_id_idx ON AudioFiles(SongId);
CREATE TABLE IF NOT EXISTS SongNotes(Id INTEGER PRIMARY KEY,SongId INTEGER,ShowNotesOnLoad INTEGER,DisplayTime INTEGER,Notes VARCHAR(1024),TextSize INTEGER DEFAULT 24,Alignment INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS song_notes_id_idx ON SongNotes(SongId);
CREATE TABLE IF NOT EXISTS Bookmarks(Id INTEGER PRIMARY KEY,SongId INTEGER,Name VARCHAR(255),PageNum INTEGER,ShowInLibrary INTEGER,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS bookmark_song_id_idx ON Bookmarks(SongId);
CREATE TABLE IF NOT EXISTS Recent(Id INTEGER PRIMARY KEY,Song INTEGER,RecentType INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS Setlists(Id INTEGER PRIMARY KEY,Name VARCHAR(255),LastPage INTEGER,LastIndex INTEGER,SortBy INTEGER,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS SetlistSong(Id INTEGER PRIMARY KEY,SetlistId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS setlist_setlist_id_idx ON SetlistSong(SetlistId);
CREATE INDEX IF NOT EXISTS setlist_song_id_idx ON SetlistSong(SongId);
CREATE TABLE IF NOT EXISTS SetlistSongNotes(Id INTEGER PRIMARY KEY,SetlistId INTEGER,SongId INTEGER,ShowNotesOnLoad INTEGER,DisplayTime INTEGER,Notes VARCHAR(1024),TextSize INTEGER DEFAULT 24,Alignment INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS setlist_notes_id_idx ON SetlistSongNotes(SetlistId);
CREATE INDEX IF NOT EXISTS setlist_notes_song_id_idx ON SetlistSongNotes(SongId);
CREATE TABLE IF NOT EXISTS Collections(Id INTEGER PRIMARY KEY,Name VARCHAR(255),SortBy INTEGER DEFAULT 1,Ascending INTEGER DEFAULT 1,DateCreated INTEGER DEFAULT 0,LastModified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS CollectionSong(Id INTEGER PRIMARY KEY,CollectionId INTEGER,SongId INTEGER);
CREATE INDEX IF NOT EXISTS col_col_id_idx ON CollectionSong(CollectionId);
CREATE INDEX IF NOT EXISTS col_song_id_idx ON CollectionSong(SongId);
CREATE TABLE IF NOT EXISTS Links(Id INTEGER PRIMARY KEY,SongId INTEGER,StartPointX FLOAT,StartPointY FLOAT,EndPointX FLOAT,EndPointY FLOAT,StartPage INTEGER,EndPage INTEGER,ZoomXStart FLOAT,ZoomYStart FLOAT,ZoomXEnd FLOAT,ZoomYEnd FLOAT,Radius INTEGER,Version INTEGER);
CREATE INDEX IF NOT EXISTS links_song_id_idx ON Links(SongId);
CREATE TABLE IF NOT EXISTS AnnotationsBase(Id INTEGER PRIMARY KEY,SongId INTEGER,Page INTEGER,Type INTEGER,Opacity INTEGER,Zoom FLOAT,ZoomY FLOAT,Version INTEGER,SourcePageWidth FLOAT,SourcePageHeight FLOAT,Layer INTEGER);
CREATE INDEX IF NOT EXISTS ann_song_id_idx ON AnnotationsBase(SongId);
CREATE TABLE IF NOT EXISTS Layers(Id INTEGER PRIMARY KEY,SongId INTEGER,Page INTEGER,LayerIndex INTEGER,Name INTEGER,Visible INTEGER);
CREATE INDEX IF NOT EXISTS layers_song_id_idx ON Layers(SongId);
CREATE TABLE IF NOT EXISTS DrawAnnotations(Id INTEGER PRIMARY KEY,BaseId INTEGER,LineColor INTEGER,FillColor INTEGER,LineWidth FLOAT,DrawMode INTEGER,PenMode INTEGER,SmoothMode INTEGER);
CREATE INDEX IF NOT EXISTS draw_ann_id_idx ON DrawAnnotations(BaseId);
CREATE TABLE IF NOT EXISTS StampAnnotations(Id INTEGER PRIMARY KEY,BaseId INTEGER,Type INTEGER,Size FLOAT,FilePath VARCHAR(255),Color INTEGER,Font INTEGER,Symbol VARCHAR(255));
CREATE INDEX IF NOT EXISTS stamp_ann_id_idx ON StampAnnotations(BaseId);
CREATE TABLE IF NOT EXISTS TextboxAnnotations(Id INTEGER PRIMARY KEY,BaseId INTEGER,TextColor INTEGER,Text VARCHAR(255),FontFamily INTEGER,FontSize FLOAT,FontStyle INTEGER,FillColor INTEGER,BorderColor INTEGER,TextAlign INTEGER,HasBorder INTEGER,BorderWidth FLOAT,AutoSize INTEGER,LineSpacing FLOAT);
CREATE INDEX IF NOT EXISTS tb_ann_id_idx ON TextboxAnnotations(BaseId);
CREATE TABLE IF NOT EXISTS AnnotationPoints(Id INTEGER PRIMARY KEY,AnnotationId INTEGER,Points BLOB,Count INTEGER);
CREATE INDEX IF NOT EXISTS ann_ann_id_idx ON AnnotationPoints(AnnotationId);
CREATE TABLE IF NOT EXISTS TextDisplaySettings(Id INTEGER PRIMARY KEY,FileId INTEGER,SongId INTEGER,FontFamily INTEGER,DefaultFont VARCHAR(255),TitleSize INTEGER,TitleFont VARCHAR(255),TitleBold INTEGER,TitleItalic INTEGER,TitleColor INTEGER,ShowTitle INTEGER,SubtitleSize INTEGER,SubtitleFont VARCHAR(255),SubtitleBold INTEGER,SubtitleItalic INTEGER,SubtitleColor INTEGER,ShowSubtitle INTEGER,MetaSize INTEGER,MetaFont VARCHAR(255),MetaBold INTEGER,MetaItalic INTEGER,MetaColor INTEGER,ShowMeta INTEGER,LyricsSize INTEGER,LyricsFont VARCHAR(255),LyricsBold INTEGER,LyricsItalic INTEGER,LyricsColor INTEGER,ShowLyrics INTEGER,ChordsSize INTEGER,ChordsFont VARCHAR(255),ChordStyle INTEGER,ChordColor INTEGER,ChordHighlight INTEGER,ShowChords INTEGER,ChorusSize INTEGER,ChorusFont VARCHAR(255),ChorusBold INTEGER,ChorusItalic INTEGER,ChorusColor INTEGER,TabSize INTEGER,TabFont VARCHAR(255),TabBold INTEGER,TabItalic INTEGER,TabColor INTEGER,ShowTabs INTEGER,GridTextSize INTEGER,GridChordFont VARCHAR(255),GridChordBold INTEGER,GridChordItalic INTEGER,GridChordColor INTEGER,ShowGrids INTEGER,GridCommentsSize INTEGER,GridCommentsFont VARCHAR(255),GridCommentsBold INTEGER,GridCommentsItalic INTEGER,GridCommentsColor INTEGER,GridCommentsHighlight INTEGER,CommentsSize INTEGER,CommentsFont VARCHAR(255),CommentsBold INTEGER,CommentsItalic INTEGER,CommentsColor INTEGER,ShowComments INTEGER,CommentsItalicSize INTEGER,CommentsItalicFont VARCHAR(255),CommentsItalicBold INTEGER,CommentsItalicItalic INTEGER,CommentsItalicColor INTEGER,CommentsItalicHighlight INTEGER,ShowCommentsItalic INTEGER,CommentBoxSize INTEGER,CommentBoxFont VARCHAR(255),CommentBoxBold INTEGER,CommentBoxItalic INTEGER,CommentBoxColor INTEGER,CommentBoxHighlight INTEGER,ShowCommentBox INTEGER,FooterSize INTEGER,FooterFont VARCHAR(255),FooterBold INTEGER,FooterItalic INTEGER,FooterColor INTEGER,ShowFooter INTEGER,LabelsSize INTEGER,LabelsFont VARCHAR(255),LabelsBold INTEGER,LabelsItalic INTEGER,LabelsColor INTEGER,LabelsUnderline INTEGER,ShowLabels INTEGER,DiagramTextSize INTEGER,DiagramTextFont VARCHAR(255),DiagramTextBold INTEGER,DiagramTextItalic INTEGER,DiagramTextColor INTEGER,LineSpacing FLOAT,ChordLineSpacing FLOAT,GridLineColor INTEGER,GridLineThickness INTEGER,ShowDiagrams INTEGER,ShowChorusLabel INTEGER,SectionIndent INTEGER,DiagramScale FLOAT,Instrument VARCHAR(255),Transpose INTEGER,TransposeKey INTEGER,Capo INTEGER,NumberChords INTEGER,EnableTranpose INTEGER,EnableCapo INTEGER,Structure VARCHAR(255),Key INTEGER,Encoding INTEGER,RTL INTEGER,UseSharps INTEGER DEFAULT 0,UseImprovedSpacing INT DEFAULT 0,NumberOfSteps INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS text_display_id_idx ON TextDisplaySettings(SongId);
CREATE TABLE IF NOT EXISTS AbcSettings(Id INTEGER PRIMARY KEY,FileId INTEGER,SongId INTEGER,TransposeLetter INTEGER,TransposeAccidentals INTEGER,Letter INTEGER,Accidentals INTEGER,PageScale FLOAT,LeftMargin INTEGER,TopMargin INTEGER,RightMargin INTEGER,BottomMargin INTEGER,Version INTEGER,TitleFont VARCHAR(255) DEFAULT '',LyricsFont VARCHAR(255) DEFAULT '',MusicFont VARCHAR(255) DEFAULT '',ChordsFont VARCHAR(255) DEFAULT '',NumberOfSteps INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS abc_id_idx ON AbcSettings(SongId);
CREATE TABLE IF NOT EXISTS MIDI(Id INTEGER PRIMARY KEY,SongId INTEGER,CommandType INTEGER,Cable INTEGER,Channel INTEGER,MSB INTEGER,LSB INTEGER,Value INTEGER,CustomField INTEGER,SendOnLoad INTEGER,LoadOnRecv INTEGER,SendMSB INTEGER DEFAULT 1,SendLSB INTEGER DEFAULT 1,SendValue INTEGER DEFAULT 1,InputPort VARCHAR(255) DEFAULT '',OutputPort VARCHAR(255) DEFAULT '',Label VARCHAR(255) DEFAULT '');
CREATE INDEX IF NOT EXISTS midi_song_id_idx ON MIDI(SongId);
CREATE TABLE IF NOT EXISTS MidiSysex(Id INTEGER PRIMARY KEY,MidiId INTEGER,SongId INTEGER,SysexBytes BLOB);
CREATE INDEX IF NOT EXISTS midi_sysex_midi_id_idx ON MidiSysex(MidiId);
CREATE TABLE IF NOT EXISTS BatchMIDI(Id INTEGER PRIMARY KEY,ParentId INTEGER,SongId INTEGER,CommandType INTEGER,Cable INTEGER,Channel INTEGER,MSB INTEGER,LSB INTEGER,Value INTEGER,CustomField INTEGER,SendMSB INTEGER DEFAULT 1,SendLSB INTEGER DEFAULT 1,SendValue INTEGER DEFAULT 1,Label VARCHAR(255) DEFAULT '');
CREATE INDEX IF NOT EXISTS batch_midi_song_id_idx ON BatchMIDI(SongId);
CREATE INDEX IF NOT EXISTS batch_midi_parent_id_idx ON BatchMIDI(ParentId);
CREATE TABLE IF NOT EXISTS BatchMidiSysex(Id INTEGER PRIMARY KEY,MidiId INTEGER,SongId INTEGER,SysexBytes BLOB);
CREATE INDEX IF NOT EXISTS batch_midi_sysex_midi_id_idx ON BatchMidiSysex(MidiId);
CREATE TABLE IF NOT EXISTS SmartButtons(Id INTEGER PRIMARY KEY,SongId INTEGER,Label VARCHAR(255),Page INTEGER,Action INTEGER,Value INTEGER,Value2 INTEGER,XPos FLOAT,YPos FLOAT,ZoomX FLOAT,ZoomY FLOAT,File VARCHAR(255),Size INTEGER,Version INTEGER);
CREATE INDEX IF NOT EXISTS smart_buttons_song_id_idx ON SmartButtons(SongId);
CREATE TABLE IF NOT EXISTS SmartButtonMIDI(Id INTEGER PRIMARY KEY,ButtonId INTEGER,SongId INTEGER,CommandType INTEGER,Cable INTEGER,Channel INTEGER,MSB INTEGER,LSB INTEGER,Value INTEGER,CustomField INTEGER,SendMSB INTEGER DEFAULT 1,SendLSB INTEGER DEFAULT 1,SendValue INTEGER DEFAULT 1,OutputPort VARCHAR(255) DEFAULT '',Label VARCHAR(255) DEFAULT '');
CREATE INDEX IF NOT EXISTS smart_midi_button_id_idx ON SmartButtonMIDI(ButtonId);
CREATE TABLE IF NOT EXISTS SmartMidiSysex(Id INTEGER PRIMARY KEY,MidiId INTEGER,SongId INTEGER,SysexBytes BLOB);
CREATE INDEX IF NOT EXISTS smart_midi_sysex_midi_id_idx ON SmartMidiSysex(MidiId);
CREATE TABLE IF NOT EXISTS MidiAction(Id INTEGER PRIMARY KEY,Action VARCHAR(255));
CREATE TABLE IF NOT EXISTS BatchMidiAction(Id INTEGER PRIMARY KEY,ParentId INTEGER,CommandType INTEGER,Cable INTEGER,Channel INTEGER,MSB INTEGER,LSB INTEGER,Value INTEGER,CustomField INTEGER,SendMSB INTEGER DEFAULT 1,SendLSB INTEGER DEFAULT 1,SendValue INTEGER DEFAULT 1,InputPort VARCHAR(255) DEFAULT '',Label VARCHAR(255) DEFAULT '');
CREATE INDEX IF NOT EXISTS midi_action_commands_parent_id_idx ON BatchMidiAction(ParentId);
CREATE TABLE IF NOT EXISTS MidiActionSysex(Id INTEGER PRIMARY KEY,MidiId INTEGER,SysexBytes BLOB);
CREATE INDEX IF NOT EXISTS midi_action_sysex_midi_id_idx ON MidiActionSysex(MidiId);
"""


# Unicode codepoints that crash MobileSheets (too new for the app's emoji renderer).
# U+1FA00..U+1FFFF = Unicode 12+ extended pictographs / symbols (e.g. 🪈 U+1FA88 "FLUTE")
_UNSUPPORTED_EMOJI_RANGE = (0x1FA00, 0x1FFFF)


def _strip_unsupported_emoji(text: str) -> str:
    """Remove emoji codepoints that MobileSheets cannot render without crashing."""
    return ''.join(
        ch for ch in text
        if not (_UNSUPPORTED_EMOJI_RANGE[0] <= ord(ch) <= _UNSUPPORTED_EMOJI_RANGE[1])
    ).strip()


class MobileSheetsDB:
    """Lightweight MobileSheets database writer."""

    def __init__(self, db_path: str):
        self.conn   = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.conn.executescript(MS_SCHEMA)
        self.conn.commit()

        # Seed required singleton rows that MobileSheets expects to exist.
        # Defaults: app reads this with a forced unwrap — must have exactly one row.
        self.cursor.execute(
            'INSERT OR IGNORE INTO Defaults (Id, Initialized) VALUES (1, 1)'
        )
        # MidiAction: fixed lookup table (3 action-type rows).
        for action_id in (1, 2, 3):
            self.cursor.execute(
                'INSERT OR IGNORE INTO MidiAction (Id, Action) VALUES (?, 0)',
                (action_id,),
            )
        self.conn.commit()

        # In-memory caches to avoid duplicate lookups
        self._composers:   dict[str, int] = {}
        self._genres:      dict[str, int] = {}
        self._keys:        dict[str, int] = {}
        self._signatures:  dict[str, int] = {}
        self._setlists:    dict[str, int] = {}
        self._collections: dict[str, int] = {}

    # --- Lookup-or-create helpers -------------------------------------------

    def _get_or_create(self, cache: dict, table: str, name_col: str,
                       value: str) -> int | None:
        if not value:
            return None
        if value in cache:
            return cache[value]
        now = int(time.time() * 1000)
        self.cursor.execute(
            f'INSERT INTO {table} ({name_col}, DateCreated, LastModified) VALUES (?, ?, ?)',
            (value, now, now),
        )
        row_id = self.cursor.lastrowid
        cache[value] = row_id
        return row_id

    def _composer_id(self, name: str) -> int | None:
        return self._get_or_create(self._composers, 'Composer', 'Name', name)

    def _genre_id(self, genre: str) -> int | None:
        return self._get_or_create(self._genres, 'Genres', 'Type', genre)

    def _key_id(self, key: str) -> int | None:
        return self._get_or_create(self._keys, 'Key', 'Name', key)

    def _sig_id(self, sig: str) -> int | None:
        return self._get_or_create(self._signatures, 'Signature', 'Name', sig)

    def _setlist_id(self, name: str) -> int | None:
        return self._get_or_create(self._setlists, 'Setlists', 'Name', name)

    def _collection_id(self, name: str) -> int | None:
        return self._get_or_create(self._collections, 'Collections', 'Name', name)

    # --- Song insertion ------------------------------------------------------

    def insert_song(self, song: dict, audio_tracks: list | None = None,
                    page_count: int = 0) -> int:
        """
        Insert a song and all associated metadata rows.

        page_count  -- total pages in the PDF.  Sets Files.SourceFilePageCount
                   for every entry and the full range for whole-file
                   entries. Pass 0 when the count is unknown.

        Returns the new Songs.Id.
        """
        now = int(time.time() * 1000)

        # For whole-file entries, use page_count as LastPage if available.
        last_page_val = page_count if (song['last_page'] == -1 and page_count > 0) else 0

        self.cursor.execute(
            """
            INSERT INTO Songs
                (Title, Difficulty, Custom, Custom2, LastPage, OrientationLock,
                 Duration, Stars, VerticalZoom, SortTitle, Sharpen, SharpenLevel,
                 Keywords, AutoStartAudio, CreationDate, LastModified, SongId)
            VALUES (?, 0, ?, ?, ?, 0, 0, 0, 1.0, '', 0, 4, ?, 0, ?, ?, 0)
            """,
            (
                song['title'],
                song['filepath'],                                        # Custom = filepath
                song.get('identifier') or str(uuid.uuid4()),            # Custom2 = UUID
                last_page_val,
                song.get('keyword', '') or '',
                now,
                now,
            ),
        )
        song_id = self.cursor.lastrowid

        # --- Files entry (PDF association with page range) ------------------
        if song['last_page'] == -1:
            if page_count > 1:
                page_order = f'1-{page_count}'
            elif page_count == 1:
                page_order = '1'
            else:
                page_order = ''
        elif song['first_page'] == song['last_page']:
            page_order = str(song['first_page'])
        else:
            pages = range(song['first_page'], song['last_page'] + 1)
            page_order = ','.join(map(str, pages))

        # Files.Id must equal Songs.Id — MobileSheets joins on Id, not SongId
        self.cursor.execute(
            """
            INSERT INTO Files
                (Id, SongId, Path, PageOrder, Type, Source, LastModified,
                 FileSize, SourceFilePageCount, FileHash, Width, Height)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, -1, -1)
            """,
            (song_id, song_id, song['filepath'], page_order, 1, 1, now, page_count),
        )

        # --- Composer (pipe-separated list supported) -----------------------
        for part in (song.get('composer', '') or '').split('|'):
            part = part.strip()
            if part:
                cid = self._composer_id(part)
                if cid:
                    self.cursor.execute(
                        'INSERT INTO ComposerSongs (ComposerId, SongId) VALUES (?, ?)',
                        (cid, song_id),
                    )

        # --- Genre (pipe-separated list supported) --------------------------
        for part in (song.get('genre', '') or '').split('|'):
            part = part.strip()
            if part:
                gid = self._genre_id(part)
                if gid:
                    self.cursor.execute(
                        'INSERT INTO GenresSongs (GenreId, SongId) VALUES (?, ?)',
                        (gid, song_id),
                    )

        # --- Key ------------------------------------------------------------
        if song.get('key'):
            kid = self._key_id(song['key'])
            if kid:
                self.cursor.execute(
                    'INSERT INTO KeySongs (KeyId, SongId) VALUES (?, ?)',
                    (kid, song_id),
                )

        # --- Signature ------------------------------------------------------
        if song.get('signature'):
            sid = self._sig_id(song['signature'])
            if sid:
                self.cursor.execute(
                    'INSERT INTO SignatureSongs (SignatureId, SongId) VALUES (?, ?)',
                    (sid, song_id),
                )

        # --- Tempo ----------------------------------------------------------
        if song.get('bpm'):
            self.cursor.execute(
                'INSERT INTO Tempos (SongId, Tempo, TempoIndex) VALUES (?, ?, ?)',
                (song_id, song['bpm'], 0),
            )

        # --- Audio tracks ---------------------------------------------------
        for track in (audio_tracks or []):
            if track.get('filename'):
                self.cursor.execute(
                    """
                    INSERT INTO AudioFiles
                        (SongId, Title, File, FileSource, LastModified)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (song_id, track.get('title', ''), track['filename'], 0, now),
                )

        # --- AutoScroll (required 1:1 row per song) -------------------------
        self.cursor.execute(
            """
            INSERT INTO AutoScroll
                (SongId, Behavior, PauseDuration, Speed, FixedDuration,
                 ScrollPercent, ScrollOnLoad, TimeBeforeScroll)
            VALUES (?, 0, 8000, 3, 1000, 20, 0, 2000)
            """,
            (song_id,),
        )

        # --- MetronomeSettings (required 1:1 row per song) ------------------
        self.cursor.execute(
            """
            INSERT INTO MetronomeSettings
                (SongId, Sig1, Sig2, Subdivision, SoundFX, AccentFirst,
                 AutoStart, CountIn, NumberCount, AutoTurn)
            VALUES (?, 2, 0, 0, 0, 0, 0, 0, 1, 0)
            """,
            (song_id,),
        )

        return song_id

    def add_to_setlist(self, setlist_name: str, song_id: int) -> None:
        slid = self._setlist_id(setlist_name)
        if slid:
            self.cursor.execute(
                'INSERT INTO SetlistSong (SetlistId, SongId) VALUES (?, ?)',
                (slid, song_id),
            )

    def add_to_collection(self, collection_name: str, song_id: int) -> None:
        """Add a song to a MobileSheets Collection (= "Sammlung").
        The collection is created if it does not yet exist.
        """
        cid = self._collection_id(collection_name)
        if cid:
            self.cursor.execute(
                'INSERT INTO CollectionSong (CollectionId, SongId) VALUES (?, ?)',
                (cid, song_id),
            )

    def insert_midi_presets_as_smart_buttons(
        self,
        song_id: int,
        presets: list[dict],
        columns: int = 0,
        x_start: float = 30.0,
        y_start: float = 500.0,
        x_spacing: float = 0.0,
        y_spacing: float = 180.0,
        zoom: float = 4.467,
        gap: float = 30.0,
        screen_width: float = 2800.0,
    ) -> tuple[int, int]:
        """
        Insert forScore MIDI presets as SmartButtons on the given song.

        Each preset becomes one SmartButton (Action=0 = send MIDI).
        The MIDI commands of each preset are inserted into SmartButtonMIDI.

        Layout is computed automatically from the labels:

        1. The widest label determines button_width via the empirical formula:
               button_width = 15.8 * effective_chars + 79.6
           where emoji count as 2 effective chars (iOS renders them ~2× wide).

        2. From button_width, gap, x_start, and screen_width the optimal
           column count is derived:
               columns = floor((screen_width - x_start) / (button_width + gap))
           clamped to [1, len(presets)].

        3. x_spacing = button_width + gap  (uniform across all rows).

        Pass columns > 0 or x_spacing > 0 to override the auto values.

        Returns (buttons_inserted, commands_inserted).
        """

        def _label_width(label: str) -> float:
            """Estimate rendered button width in MobileSheets screen-pt.

            Emoji characters are rendered roughly twice as wide as regular
            characters on iOS, so we count each emoji as 2 units.
            Empirical formula (calibrated from screenshots):
                width = 15.8 * effective_chars + 79.6
            """
            effective_chars = 0
            for ch in label:
                cp = ord(ch)
                if (0x1F300 <= cp <= 0x1FAFF) or (0x2600 <= cp <= 0x27BF):
                    effective_chars += 2  # emoji ≈ 2× wide
                else:
                    effective_chars += 1
            return 27.0 * effective_chars + 80.0

        # --- auto-compute layout ---
        all_labels = [_strip_unsupported_emoji(p['title']) for p in presets]
        max_button_width = max((_label_width(lbl) for lbl in all_labels), default=100.0)

        if x_spacing > 0:
            effective_spacing = x_spacing
        else:
            min_spacing = max_button_width + gap

            if columns > 0:
                effective_columns = columns
            else:
                # How many columns fit with minimum spacing?
                effective_columns = max(1, int((screen_width - x_start) / min_spacing))
                effective_columns = min(effective_columns, len(presets))

            # Spread columns evenly across the full screen width (justified).
            # spacing = distance between button start points so that the last
            # button starts at screen_width - x_start - max_button_width.
            if effective_columns > 1:
                effective_spacing = (screen_width - x_start - max_button_width) / (effective_columns - 1)
            else:
                effective_spacing = screen_width - x_start

        if columns > 0:
            effective_columns = columns
        elif x_spacing > 0:
            effective_columns = max(1, int((screen_width - x_start) / x_spacing))
            effective_columns = min(effective_columns, len(presets))

        # clamp to number of presets
        effective_columns = min(effective_columns, len(presets))

        buttons_inserted  = 0
        commands_inserted = 0

        for idx, preset in enumerate(presets):
            col = idx % effective_columns
            row = idx // effective_columns
            x   = x_start + col * effective_spacing
            y   = y_start + row * y_spacing

            self.cursor.execute(
                """
                INSERT INTO SmartButtons
                    (SongId, Label, Page, Action, Value, Value2,
                     XPos, YPos, ZoomX, ZoomY, File, Size, Version)
                VALUES (?, ?, 0, 0, -1, 0, ?, ?, ?, ?, '', 1, 1)
                """,
                (song_id, _strip_unsupported_emoji(preset['title']), x, y, zoom, zoom),
            )
            button_id = self.cursor.lastrowid
            buttons_inserted += 1

            for cmd in preset.get('commands', []):
                self.cursor.execute(
                    """
                    INSERT INTO SmartButtonMIDI
                        (ButtonId, SongId, CommandType, Cable, Channel,
                         MSB, LSB, Value, CustomField,
                         SendMSB, SendLSB, SendValue, OutputPort, Label)
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?, ?, '', ?)
                    """,
                    (
                        button_id,
                        song_id,
                        cmd['CommandType'],
                        cmd['Channel'],
                        cmd['MSB'],
                        cmd['LSB'],
                        cmd['Value'],
                        cmd['SendMSB'],
                        cmd['SendLSB'],
                        cmd['SendValue'],
                        cmd.get('Label', ''),
                    ),
                )
                commands_inserted += 1

        return buttons_inserted, commands_inserted

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()



# ---------------------------------------------------------------------------
# Minimal PDF generator (stdlib only — no reportlab / fpdf)
# ---------------------------------------------------------------------------
def _make_title_pdf(dest_path: str, title: str) -> None:
    """
    Write a minimal, valid single-page A4 Portrait PDF that shows *title*
    centred on a white page.  Uses only the PDF spec (no external libraries).

    The text is drawn with Helvetica-Bold (a PDF standard font — no font
    embedding required).  Lines longer than ~60 characters are word-wrapped
    automatically.
    """

    # --- helpers ---
    def _pdf_str(s: str) -> bytes:
        """Escape a Python string for use as a PDF literal string."""
        return ('(' + s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ')').encode('latin-1', errors='replace')

    # A4 Portrait in points: 595 × 842
    page_w, page_h = 595, 842
    font_size      = 24
    line_height    = font_size * 1.4
    max_chars_line = 50   # rough estimate at font_size=24

    # Word-wrap title
    words = title.split()
    lines: list[str] = []
    cur = ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars_line:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)

    # Top of page (with margin)
    block_h  = len(lines) * line_height
    y_top    = page_h - 60

    # Build content stream
    stream_parts = [
        b'BT',
        b'/F1 ' + str(font_size).encode() + b' Tf',
    ]
    for i, line in enumerate(lines):
        x = page_w / 2
        y = y_top - i * line_height
        # Use Td for first line, then TD offsets would be messy — place each
        # line absolutely via a text matrix.
        stream_parts.append(
            str(x).encode() + b' ' + f'{y:.2f}'.encode() + b' Td'
            if i == 0 else
            b'0 ' + f'{-line_height:.2f}'.encode() + b' Td'
        )
        # Centre: compute width approx (Helvetica-Bold ~0.55 × font_size per char)
        approx_w = len(line) * font_size * 0.55
        x_offset  = -approx_w / 2
        # Combine offset + text in one go using Td (relative)
        if i == 0:
            # Overwrite the last Td with a proper x-offset version
            stream_parts[-1] = (
                f'{x + x_offset:.2f} {y:.2f} Td'.encode()
            )
        else:
            stream_parts[-1] = f'{x_offset:.2f} {-line_height:.2f} Td'.encode()
        stream_parts.append(_pdf_str(line) + b' Tj')
    stream_parts.append(b'ET')

    content_stream = b'\n'.join(stream_parts)

    # --- Build PDF objects ---
    objects: list[bytes] = []   # index 0 = object 1

    # Object 1: Catalog
    objects.append(b'<< /Type /Catalog /Pages 2 0 R >>')

    # Object 2: Pages
    objects.append(b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>')

    # Object 3: Page
    objects.append(
        f'<< /Type /Page /Parent 2 0 R '
        f'/MediaBox [0 0 {page_w} {page_h}] '
        f'/Contents 4 0 R '
        f'/Resources << /Font << /F1 5 0 R >> >> '
        f'>>'.encode()
    )

    # Object 4: Content stream
    objects.append(
        b'<< /Length ' + str(len(content_stream)).encode() + b' >>\nstream\n'
        + content_stream + b'\nendstream'
    )

    # Object 5: Font (Helvetica-Bold — standard PDF font, no embedding needed)
    objects.append(
        b'<< /Type /Font /Subtype /Type1 '
        b'/BaseFont /Helvetica-Bold '
        b'/Encoding /WinAnsiEncoding >>'
    )

    # --- Serialise ---
    buf  = bytearray()
    xref: list[int] = []

    buf += b'%PDF-1.4\n'
    for i, obj_data in enumerate(objects, 1):
        xref.append(len(buf))
        buf += f'{i} 0 obj\n'.encode()
        buf += obj_data
        buf += b'\nendobj\n'

    xref_pos = len(buf)
    n_obj    = len(objects) + 1   # +1 for the free entry
    buf += b'xref\n'
    buf += f'0 {n_obj}\n'.encode()
    buf += b'0000000000 65535 f \n'
    for off in xref:
        buf += f'{off:010d} 00000 n \n'.encode()

    buf += b'trailer\n'
    buf += f'<< /Size {n_obj} /Root 1 0 R >>\n'.encode()
    buf += b'startxref\n'
    buf += f'{xref_pos}\n'.encode()
    buf += b'%%EOF\n'

    Path(dest_path).write_bytes(bytes(buf))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_input(filepath: str) -> None:
    p = Path(filepath)
    if not p.exists():
        sys.exit(f'Error: file not found: {filepath}')
    if p.suffix.lower() != '.4sb':
        sys.exit(f'Error: expected a .4sb file, got: {p.name}')
    with open(filepath, 'rb') as f:
        header = f.read(_HEADER_SEARCH_WINDOW).decode('ascii', errors='replace')
    if '4SBV02' not in header and '4SBV03' not in header:
        sys.exit(
            'Error: file does not appear to be a forScore .4sb file '
            '(neither 4SBV02 nor 4SBV03 found in header).'
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=(
            'Import forScore .4sb data directly into a fresh MobileSheets SQLite database.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        'input', metavar='INPUT.4sb',
        help='forScore Backup (V02) or Archive (V03) .4sb file',
    )
    parser.add_argument(
        '-o', '--output-dir', metavar='OUTPUT_DIR',
        help='Output directory (default: <input-stem>-2MS/ next to input file)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Parse and report statistics without writing any database',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print each inserted song title',
    )
    parser.add_argument(
        '--midi-presets', action='store_true',
        help='Import forScore MIDI presets as Smart Buttons into the database',
    )
    parser.add_argument(
        '--midi-song', metavar='SONG_TITLE',
        help=(
            'Title of the existing MobileSheets song that should receive the '
            'Smart Buttons (must already exist in the database). '
            'Required when --midi-presets is used on an existing DB; '
            'ignored for new databases (use --midi-presets-new-song instead).'
        ),
    )
    parser.add_argument(
        '--midi-presets-new-song', metavar='SONG_TITLE',
        help=(
            'Create a new placeholder song with this title and attach all '
            'MIDI presets as Smart Buttons to it. '
            'Use when your placeholder PDF does not yet exist in the database.'
        ),
    )
    parser.add_argument(
        '--midi-columns', metavar='N', type=int, default=0,
        help=(
            'Number of Smart Button columns (default: 0 = auto-compute from '
            'button width and screen width).'
        ),
    )
    parser.add_argument(
        '--midi-spacing', metavar='PT', type=float, default=0,
        help=(
            'Horizontal spacing between Smart Buttons in points '
            '(default: 0 = auto-compute from longest label). '
            'Override if buttons overlap or to force a specific layout.'
        ),
    )
    parser.add_argument(
        '--pdf-dir', metavar='DIR',
        help=(
            'Directory containing the extracted PDF files (default: '
            '<input-stem>/files/ next to the archive). '
            'The page count of each PDF is read via mdls '
            'and used for the full-document PageOrder, Songs.LastPage and '
            'Files.SourceFilePageCount. Missing PDFs are reported but still imported.'
        ),
    )
    parser.add_argument(
        '--pdf-collection', metavar='NAME', default='',
        help=(
            'Add all "pdf" whole-file entries (one per bookmark-host PDF) '
            'to a MobileSheets Collection (Sammlung) with this name. '
            'The collection is created if it does not exist. '
            'Example: --pdf-collection "Books"'
        ),
    )
    args = parser.parse_args()

    validate_input(args.input)

    p        = Path(args.input).resolve()
    ts_start = datetime.now().strftime('%H:%M:%S')

    print(f'Started:  {ts_start}')
    print(f'Input:    {p.name}')
    print()

    # --- Read plist ----------------------------------------------------------
    print('Reading metadata...')
    try:
        plist = read_plist_from_4sb(str(p))
    except Exception as e:
        sys.exit(f'Error reading .4sb file: {e}')
    print(f'  {len(plist):,} top-level plist keys')
    print()

    # --- Extract data --------------------------------------------------------
    print('Extracting songs and bookmarks...')
    songs = extract_songs_and_bookmarks(plist)
    n_bookmarks = sum(1 for s in songs if s['type'] == 'bookmark')
    n_singles   = sum(1 for s in songs if s['type'] == 'single')
    n_pdfs      = sum(1 for s in songs if s['type'] == 'pdf')
    print(f'  {len(songs):,} songs total  ({n_bookmarks:,} bookmarks, {n_singles:,} single PDFs, {n_pdfs:,} PDF files)')

    print('Extracting setlists...')
    setlists = extract_setlists(plist)
    total_sl_songs = sum(len(sl['songs']) for sl in setlists)
    print(f'  {len(setlists):,} setlists  ({total_sl_songs:,} song-setlist assignments)')

    print('Extracting audio links...')
    audio_links = extract_audio_links(plist)
    total_tracks = sum(len(v) for v in audio_links.values())
    print(f'  {total_tracks:,} audio tracks across {len(audio_links):,} PDFs')

    # --- Extract MIDI presets (if requested) ---------------------------------
    midi_presets        = []
    n_midi_presets      = 0
    n_midi_buttons      = 0
    n_midi_commands_ins = 0

    if args.midi_presets or args.midi_presets_new_song:
        print('Extracting MIDI presets...')
        midi_presets   = extract_midi_presets(plist)
        n_midi_presets = len(midi_presets)
        total_cmds     = sum(len(p['commands']) for p in midi_presets)
        print(f'  {n_midi_presets} presets  ({total_cmds} total commands)')

    print()

    if args.dry_run:
        print('Dry run -- no database written.')
        if midi_presets:
            print()
            print('MIDI presets (dry run preview):')
            for p_ in midi_presets[:5]:
                print(f'  {p_["title"]}  ({len(p_["commands"])} commands)')
            if len(midi_presets) > 5:
                print(f'  ... and {len(midi_presets) - 5} more')
        return

    # --- Output path ---------------------------------------------------------
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = p.parent / f'{p.stem}-2MS'

    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / 'mobilesheets.db'
    print(f'Output:   {db_path}')
    print()

    # --- Create database (always start fresh) --------------------------------
    for suffix in ('', '-shm', '-wal'):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    print('Creating MobileSheets database...')
    db = MobileSheetsDB(str(db_path))

    pdf_dir = (Path(args.pdf_dir).resolve() if args.pdf_dir
               else Path(args.input).resolve().with_suffix('') / 'files')
    pdf_page_counts: dict[str, int] = {}
    unknown_pdf_pages: dict[str, str] = {}
    if not pdf_dir.is_dir():
        print(f'  Warning: PDF directory not found: {pdf_dir}')

    # Build a lookup: (filepath, title) -> song_id  for setlist assignment
    song_id_map: dict[tuple, int] = {}

    t0 = time.monotonic()
    errors = 0

    for i, song in enumerate(songs, 1):
        try:
            tracks = audio_links.get(song['filepath'], [])

            filepath = song['filepath']
            if filepath not in pdf_page_counts:
                pdf_path = pdf_dir / filepath
                pdf_page_counts[filepath] = get_pdf_page_count(pdf_path)
                if pdf_page_counts[filepath] <= 0:
                    unknown_pdf_pages[filepath] = (
                        'file missing' if not pdf_path.is_file() else 'page count unavailable'
                    )
            page_count = pdf_page_counts[filepath]

            song_id = db.insert_song(song, audio_tracks=tracks, page_count=page_count)
            key     = (song['filepath'], song['title'])
            song_id_map[key] = song_id

            # Add 'pdf' whole-file entries to the requested collection
            if song['type'] == 'pdf' and args.pdf_collection:
                db.add_to_collection(args.pdf_collection, song_id)

            if args.verbose:
                print(f'  [{i:>5}] {song["title"]}')
            elif i % 500 == 0:
                elapsed = time.monotonic() - t0
                rate    = i / elapsed if elapsed > 0 else 0
                print(f'  {i:>6}/{len(songs)}  ({rate:.0f} songs/s)')

        except Exception as e:
            print(f'  Warning: could not insert "{song.get("title", "?")}" -- {e}')
            errors += 1

    print(f'  {len(songs) - errors:,} songs inserted  ({errors} errors)')
    if unknown_pdf_pages:
        print(f'  Warning: page count unavailable for {len(unknown_pdf_pages)} PDFs; metadata retained:')
        for filepath, reason in sorted(unknown_pdf_pages.items()):
            print(f'    {filepath} ({reason})')
    print()

    # --- Setlists ------------------------------------------------------------
    print('Adding setlist assignments...')
    sl_assigned = 0
    sl_missing  = 0

    for sl in setlists:
        for fp, title in sl['songs']:
            key = (fp, title)
            if key in song_id_map:
                db.add_to_setlist(sl['name'], song_id_map[key])
                sl_assigned += 1
            else:
                sl_missing += 1

    print(f'  {sl_assigned:,} assignments added  ({sl_missing} unmatched entries)')
    print()

    # --- MIDI presets as Smart Buttons ---------------------------------------
    if midi_presets:
        midi_song_id  = None

        if args.midi_presets_new_song:
            # Create a fresh placeholder song
            title_new = args.midi_presets_new_song
            print(f'Creating MIDI placeholder song "{title_new}"...')

            # Generate a title-page PDF next to the database — plain A4 Portrait.
            pdf_filename  = title_new + '.pdf'
            pdf_path      = out_dir / pdf_filename
            _make_title_pdf(str(pdf_path), title_new)
            print(f'  Created title PDF: {pdf_path.name}')

            placeholder = {
                'type':       'single',
                'filepath':   pdf_filename,
                'title':      title_new,
                'first_page': 1,
                'last_page':  1,
                'composer':   '',
                'genre':      '',
                'keyword':    '',
                'key':        None,
                'signature':  None,
                'bpm':        None,
                'identifier': str(uuid.uuid4()),
            }
            midi_song_id = db.insert_song(placeholder)
            print(f'  Created with SongId={midi_song_id}')

        elif args.midi_song:
            # Look up existing song by title
            print(f'Looking up MIDI target song "{args.midi_song}"...')
            db.cursor.execute(
                'SELECT Id FROM Songs WHERE Title=? LIMIT 1', (args.midi_song,)
            )
            row = db.cursor.fetchone()
            if row:
                midi_song_id = row[0]
                print(f'  Found SongId={midi_song_id}')
            else:
                print(f'  Warning: song "{args.midi_song}" not found — skipping MIDI preset import.')

        if midi_song_id is not None:
            print('Inserting MIDI presets as Smart Buttons...')
            n_midi_buttons, n_midi_commands_ins = db.insert_midi_presets_as_smart_buttons(
                midi_song_id, midi_presets,
                columns=args.midi_columns,
                x_spacing=args.midi_spacing,
            )
            print(f'  {n_midi_buttons} Smart Buttons inserted')
            print(f'  {n_midi_commands_ins} MIDI commands inserted')
            print()

    # --- Commit & close ------------------------------------------------------
    print('Committing...')
    db.commit()
    db.close()

    ts_end  = datetime.now().strftime('%H:%M:%S')
    elapsed = time.monotonic() - t0

    # --- Summary -------------------------------------------------------------
    db_size = db_path.stat().st_size
    print()
    print('=' * 52)
    print('DONE')
    print('=' * 52)
    print(f'  Started:          {ts_start}')
    print(f'  Finished:         {ts_end}')
    print(f'  Duration:         {int(elapsed)}s')
    print()
    print(f'  Songs inserted:   {len(songs) - errors:,}')
    print(f'    Bookmarks:      {n_bookmarks:,}')
    print(f'    Single PDFs:    {n_singles:,}')
    print(f'    PDF files:      {n_pdfs:,}')
    print(f'  Setlists:         {len(setlists):,}')
    print(f'  SL assignments:   {sl_assigned:,}')
    print(f'  Audio tracks:     {total_tracks:,}')
    if n_midi_presets:
        print(f'  MIDI presets:     {n_midi_presets}  →  {n_midi_buttons} Smart Buttons, {n_midi_commands_ins} commands')
    if args.pdf_collection:
        print(f'  PDF collection:   "{args.pdf_collection}"  ({n_pdfs} entries)')
    print(f'  Errors:           {errors}')
    print()
    print(f'  Database:         {db_path}')
    if args.midi_presets_new_song:
        pdf_name = args.midi_presets_new_song + '.pdf'
        print(f'  Title PDF:        {out_dir / pdf_name}')
    print(f'  DB size:          {db_size / 1024:.1f} KB')
    print('=' * 52)
    print()
    print('Next steps:')
    print('  1. Copy mobilesheets.db to your iPad/device')
    print('  2. Switch library in MobileSheets to the new database')
    print('  3. Ensure all PDFs are present in the MobileSheets folder')


if __name__ == '__main__':
    main()
