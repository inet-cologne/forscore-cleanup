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

Output
------
  <input_stem>-2MS/
      MobileSheets.db          fresh MobileSheets database (importable by the app)

Usage
-----
  forScore2MS_v2.py <input.4sb> [-o OUTPUT_DIR] [--dry-run] [-v]

Options
-------
  -o OUTPUT_DIR    Output directory  (default: <input-stem>-2MS/ next to input)
  --dry-run        Parse and report without writing any database
  -v, --verbose    Print each inserted song title
"""

import argparse
import gzip
import os
import plistlib
import re
import shutil
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADER_SIZE = 74

# forScore key encoding: 0-11 = major, 100-111 = minor (repeated in steps of 100)
MAJOR_KEYS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
MINOR_KEYS = ['Cm', 'Dbm', 'Dm', 'Ebm', 'Em', 'Fm', 'F#m', 'Gm', 'Abm', 'Am', 'Bbm', 'Bm']


# ---------------------------------------------------------------------------
# .4sb reader  (re-used from clean_forscore_bookmarks_v2.py)
# ---------------------------------------------------------------------------
def read_plist_from_4sb(filepath: str) -> dict:
    """
    Read the 74-byte header, determine metadata size, decompress the metadata
    gzip block, and return the parsed plist dict.
    """
    with open(filepath, 'rb') as f:
        header = f.read(HEADER_SIZE).decode('ascii', errors='replace')

    m = re.search(r'(\d{5,12})(Backup|Archiv)', header)
    if not m:
        raise ValueError(f"Cannot parse metadata size from file header: {header!r}")

    metadata_size = int(m.group(1))

    with open(filepath, 'rb') as f:
        f.seek(HEADER_SIZE)
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

    For each PDF that has '|bookmarks', each bookmark becomes a song entry
    with its own page range.  PDFs without bookmarks become whole-PDF entries.
    Both types are included.

    Returns a list of dicts with keys:
        type        - 'bookmark' or 'single'
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
# MobileSheets DB creation
# ---------------------------------------------------------------------------
MS_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS Songs (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,
    Title           VARCHAR(255),
    Difficulty      INTEGER,
    Custom          VARCHAR(255) DEFAULT '',
    Custom2         VARCHAR(255) DEFAULT '',
    LastPage        INTEGER,
    OrientationLock INTEGER,
    Duration        INTEGER,
    Stars           INTEGER DEFAULT 0,
    VerticalZoom    FLOAT DEFAULT 1,
    SortTitle       VARCHAR(255) DEFAULT '',
    Sharpen         INTEGER DEFAULT 0,
    SharpenLevel    INTEGER DEFAULT 4,
    CreationDate    INTEGER DEFAULT 0,
    LastModified    INTEGER DEFAULT 0,
    Keywords        VARCHAR(255) DEFAULT '',
    AutoStartAudio  INTEGER,
    SongId          INTEGER
);

CREATE TABLE IF NOT EXISTS Files (
    Id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    SongId              INTEGER,
    Path                VARCHAR(255),
    PageOrder           VARCHAR(255),
    FileSize            INTEGER,
    LastModified        INTEGER,
    Source              INTEGER,
    Type                INTEGER,
    Password            VARCHAR(255) DEFAULT '',
    SourceFilePageCount INTEGER,
    FileHash            INTEGER,
    Width               INTEGER,
    Height              INTEGER
);

CREATE TABLE IF NOT EXISTS AudioFiles (
    Id            INTEGER PRIMARY KEY AUTOINCREMENT,
    SongId        INTEGER,
    Title         VARCHAR(255),
    File          VARCHAR(255),
    FileSource    INTEGER DEFAULT 0,
    StartPos      INTEGER DEFAULT 0,
    EndPos        INTEGER DEFAULT 0,
    FileSize      INTEGER DEFAULT 0,
    LastModified  INTEGER DEFAULT 0,
    APosition     INTEGER DEFAULT -1,
    BPosition     INTEGER DEFAULT -1,
    ABEnabled     INTEGER DEFAULT 0,
    FullDuration  INTEGER DEFAULT 0,
    Volume        FLOAT DEFAULT 0.75,
    Artist        VARCHAR(255) DEFAULT '',
    PitchShift    INTEGER DEFAULT 0,
    TempoSpeed    FLOAT DEFAULT 1.0,
    AudioFileHash INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS Composer (
    Id           INTEGER PRIMARY KEY AUTOINCREMENT,
    Name         VARCHAR(255),
    DateCreated  INTEGER DEFAULT 0,
    LastModified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ComposerSongs (
    Id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ComposerId INTEGER,
    SongId     INTEGER
);

CREATE TABLE IF NOT EXISTS Genres (
    Id           INTEGER PRIMARY KEY AUTOINCREMENT,
    Type         VARCHAR(255),
    DateCreated  INTEGER DEFAULT 0,
    LastModified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS GenresSongs (
    Id      INTEGER PRIMARY KEY AUTOINCREMENT,
    GenreId INTEGER,
    SongId  INTEGER
);

CREATE TABLE IF NOT EXISTS Key (
    Id           INTEGER PRIMARY KEY AUTOINCREMENT,
    Name         VARCHAR(255),
    DateCreated  INTEGER DEFAULT 0,
    LastModified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS KeySongs (
    Id     INTEGER PRIMARY KEY AUTOINCREMENT,
    KeyId  INTEGER,
    SongId INTEGER
);

CREATE TABLE IF NOT EXISTS Signature (
    Id           INTEGER PRIMARY KEY AUTOINCREMENT,
    Name         VARCHAR(255),
    DateCreated  INTEGER DEFAULT 0,
    LastModified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS SignatureSongs (
    Id          INTEGER PRIMARY KEY AUTOINCREMENT,
    SignatureId INTEGER,
    SongId      INTEGER
);

CREATE TABLE IF NOT EXISTS Tempos (
    Id         INTEGER PRIMARY KEY AUTOINCREMENT,
    SongId     INTEGER,
    Tempo      INTEGER,
    TempoIndex INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS Setlists (
    Id           INTEGER PRIMARY KEY AUTOINCREMENT,
    Name         VARCHAR(255),
    LastPage     INTEGER DEFAULT 0,
    LastIndex    INTEGER DEFAULT 0,
    SortBy       INTEGER DEFAULT 0,
    Ascending    INTEGER DEFAULT 1,
    DateCreated  INTEGER DEFAULT 0,
    LastModified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS SetlistSong (
    Id        INTEGER PRIMARY KEY AUTOINCREMENT,
    SetlistId INTEGER,
    SongId    INTEGER
);
"""


class MobileSheetsDB:
    """Lightweight MobileSheets database writer."""

    def __init__(self, db_path: str):
        self.conn   = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.conn.executescript(MS_SCHEMA)
        self.conn.commit()

        # In-memory caches to avoid duplicate lookups
        self._composers:  dict[str, int] = {}
        self._genres:     dict[str, int] = {}
        self._keys:       dict[str, int] = {}
        self._signatures: dict[str, int] = {}
        self._setlists:   dict[str, int] = {}

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

    # --- Song insertion ------------------------------------------------------

    def insert_song(self, song: dict, audio_tracks: list | None = None) -> int:
        """
        Insert a song and all associated metadata rows.
        Returns the new Songs.Id.
        """
        now = int(time.time() * 1000)

        self.cursor.execute(
            """
            INSERT INTO Songs
                (Title, Custom, Custom2, Keywords, CreationDate, LastModified, SongId)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                song['title'],
                song['filepath'],                                        # Custom = filepath
                song.get('identifier') or str(uuid.uuid4()),            # Custom2 = UUID
                song.get('keyword', '') or '',
                now,
                now,
                0,
            ),
        )
        song_id = self.cursor.lastrowid

        # --- Files entry (PDF association with page range) ------------------
        if song['last_page'] == -1:
            page_order = ''   # empty = whole PDF
        elif song['first_page'] == song['last_page']:
            page_order = str(song['first_page'])
        else:
            pages = range(song['first_page'], song['last_page'] + 1)
            page_order = ','.join(map(str, pages))

        self.cursor.execute(
            """
            INSERT INTO Files
                (SongId, Path, PageOrder, Type, Source, LastModified)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (song_id, song['filepath'], page_order, 1, 1, now),
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

        return song_id

    def add_to_setlist(self, setlist_name: str, song_id: int) -> None:
        slid = self._setlist_id(setlist_name)
        if slid:
            self.cursor.execute(
                'INSERT INTO SetlistSong (SetlistId, SongId) VALUES (?, ?)',
                (slid, song_id),
            )

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


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
        header = f.read(HEADER_SIZE).decode('ascii', errors='replace')
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
    print(f'  {len(songs):,} songs total  ({n_bookmarks:,} bookmarks, {n_singles:,} single PDFs)')

    print('Extracting setlists...')
    setlists = extract_setlists(plist)
    total_sl_songs = sum(len(sl['songs']) for sl in setlists)
    print(f'  {len(setlists):,} setlists  ({total_sl_songs:,} song-setlist assignments)')

    print('Extracting audio links...')
    audio_links = extract_audio_links(plist)
    total_tracks = sum(len(v) for v in audio_links.values())
    print(f'  {total_tracks:,} audio tracks across {len(audio_links):,} PDFs')
    print()

    if args.dry_run:
        print('Dry run -- no database written.')
        return

    # --- Output path ---------------------------------------------------------
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = p.parent / f'{p.stem}-2MS'

    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / 'MobileSheets.db'
    print(f'Output:   {db_path}')
    print()

    # --- Create database -----------------------------------------------------
    print('Creating MobileSheets database...')
    db = MobileSheetsDB(str(db_path))

    # Build a lookup: (filepath, title) -> song_id  for setlist assignment
    song_id_map: dict[tuple, int] = {}

    t0 = time.monotonic()
    errors = 0

    for i, song in enumerate(songs, 1):
        try:
            tracks   = audio_links.get(song['filepath'], [])
            song_id  = db.insert_song(song, audio_tracks=tracks)
            key      = (song['filepath'], song['title'])
            song_id_map[key] = song_id

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
    print(f'  Setlists:         {len(setlists):,}')
    print(f'  SL assignments:   {sl_assigned:,}')
    print(f'  Audio tracks:     {total_tracks:,}')
    print(f'  Errors:           {errors}')
    print()
    print(f'  Database:         {db_path}')
    print(f'  DB size:          {db_size / 1024:.1f} KB')
    print('=' * 52)
    print()
    print('Next steps:')
    print('  1. Copy MobileSheets.db to your Android device')
    print('  2. Replace the existing database in MobileSheets storage')
    print('  3. Ensure all PDFs are present in the MobileSheets folder')


if __name__ == '__main__':
    main()
