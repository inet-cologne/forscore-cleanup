#!/usr/bin/env python3
"""
Extract embedded binary files from a forScore Archive (.4sb V03) file.

The forScore Archive format stores each embedded file as a record consisting of:
  - An ASCII record header (variable length) containing:
      <index>  <compressed_size>  {%DOCUMENTS_DIR%}/<filename>
  - Followed immediately by the gzip-compressed file data

This script reads records sequentially using the compressed_size from each
header, so it never scans the entire file byte-by-byte.

Usage:
    extract_binaries_forscore_backup.py <input.4sb> [-o OUTPUT_DIR] [-v]
                                        [--midi-doc]

Output directory layout (default):
    <input-stem>/files/   (sibling to the input file)

With -o:
    <OUTPUT_DIR>/files/

Existing files in the output directory are overwritten.
"""

import sys
import os
import re
import gzip
import plistlib
import argparse
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Magic-byte table (fallback when filename has no usable extension)
# ---------------------------------------------------------------------------
MAGIC_TABLE = [
    # (offset, bytes_to_match, extension)
    (0,  b'%PDF',               '.pdf'),
    (0,  b'ID3',                '.mp3'),
    (0,  b'\xff\xfb',           '.mp3'),
    (0,  b'\xff\xf3',           '.mp3'),
    (0,  b'\xff\xf2',           '.mp3'),
    (0,  b'\x89PNG\r\n\x1a\n',  '.png'),
    (0,  b'GIF8',               '.gif'),
    (0,  b'\xff\xd8\xff',       '.jpg'),
    (0,  b'PK\x03\x04',         '.zip'),
    (4,  b'ftyp',               '.m4a'),
    (0,  b'RIFF',               '.wav'),
    (0,  b'fLaC',               '.flac'),
    (0,  b'MThd',               '.midi'),
    (0,  b'OggS',               '.ogg'),
    (0,  b'<?xml',              '.xml'),
    (0,  b'<plist',             '.xml'),
]


def detect_extension(data: bytes) -> str:
    """Return a file extension based on magic bytes, or '.bin' as fallback."""
    for offset, magic, ext in MAGIC_TABLE:
        end = offset + len(magic)
        if len(data) >= end and data[offset:end] == magic:
            return ext

    # AIFF: 'FORM' at 0, 'AIFF' at 8
    if len(data) >= 12 and data[0:4] == b'FORM' and data[8:12] == b'AIFF':
        return '.aiff'

    # XML / CSV heuristics
    if len(data) >= 5:
        try:
            snippet = data[:256].decode('utf-8', errors='strict')
            stripped = snippet.lstrip()
            if stripped.startswith('<'):
                return '.xml'
            first_line = stripped.split('\n')[0]
            if ',' in first_line and all(
                32 <= ord(c) < 127 or c in '\t\r\n' for c in first_line
            ):
                return '.csv'
        except UnicodeDecodeError:
            pass

    return '.bin'


# ---------------------------------------------------------------------------
# forScore header parsing
# ---------------------------------------------------------------------------
_HEADER_SEARCH_WINDOW = 256   # bytes to scan for gzip magic at file start


def _find_header_gzip_start(filepath: str) -> int:
    """
    Return the byte offset of the first gzip magic byte sequence (\\x1f\\x8b\\x08)
    in the file header area.  This is the start of the compressed metadata block.

    The marker sits right after the ASCII header whose length varies between
    forScore versions/locales (e.g. 74 bytes for 'Archiv', 75 bytes for 'Archive').
    """
    with open(filepath, 'rb') as f:
        buf = f.read(_HEADER_SEARCH_WINDOW)
    idx = buf.find(b'\x1f\x8b\x08')
    if idx == -1:
        raise ValueError('gzip magic not found in file header area.')
    return idx


def read_metadata_end(filepath: str) -> int:
    """Return the byte offset where the metadata gzip block ends."""
    gzip_start = _find_header_gzip_start(filepath)

    with open(filepath, 'rb') as f:
        header_bytes = f.read(gzip_start)

    header_str = header_bytes.decode('ascii', errors='replace')
    match = re.search(r'(\d{5,12})(Backup|Archiv)', header_str)
    if not match:
        raise ValueError("Cannot parse metadata size from file header.")

    metadata_size = int(match.group(1))
    return gzip_start + metadata_size


# ---------------------------------------------------------------------------
# MIDI preset extraction
# ---------------------------------------------------------------------------

def _read_plist_from_4sb(filepath: str) -> dict:
    """Read and return the metadata plist from a .4sb file."""
    gzip_start = _find_header_gzip_start(filepath)
    with open(filepath, 'rb') as f:
        header = f.read(gzip_start).decode('ascii', errors='replace')
    m = re.search(r'(\d{5,12})(Backup|Archiv)', header)
    if not m:
        raise ValueError('Cannot parse metadata size from file header.')
    metadata_size = int(m.group(1))
    with open(filepath, 'rb') as f:
        f.seek(gzip_start)
        raw = gzip.decompress(f.read(metadata_size))
    if raw[:8] != b'bplist00':
        raise ValueError('Metadata block is not a binary plist.')
    return plistlib.loads(raw)


def _resolve_nska(data: bytes) -> list:
    """
    Decode an NSKeyedArchiver bplist containing an NSArray of NSDictionary
    MIDI command objects.  Returns a list of plain Python dicts.
    """
    inner = plistlib.loads(data)
    objects = inner['$objects']

    def resolve(obj):
        if hasattr(obj, 'data'):           # plistlib.UID
            return resolve(objects[obj.data])
        if isinstance(obj, dict):
            cls_uid = obj.get('$class')
            classname = ''
            if cls_uid is not None:
                cls_obj = objects[cls_uid.data]
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
    result = resolve(top_uid)
    if isinstance(result, list):
        return result
    return [result] if result else []


def _decode_hex_command(hex_str: str) -> str:
    """
    Decode a raw MIDI hex command string (e.g. 'B0077F') into a human-readable
    description.

    Supported status bytes:
      0x80–0x8F  Note Off
      0x90–0x9F  Note On
      0xA0–0xAF  Polyphonic Key Pressure
      0xB0–0xBF  Control Change (CC)
      0xC0–0xCF  Program Change
      0xD0–0xDF  Channel Pressure
      0xE0–0xEF  Pitch Bend
    """
    # Pad to even length
    s = hex_str.strip()
    if len(s) % 2:
        s = '0' + s
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return f'raw hex: {hex_str}'

    if not raw:
        return f'raw hex: {hex_str}'

    status = raw[0]
    kind   = status & 0xF0
    ch     = (status & 0x0F) + 1   # 1-based channel

    b1 = raw[1] if len(raw) > 1 else 0
    b2 = raw[2] if len(raw) > 2 else 0

    # Well-known CC numbers
    CC_NAMES = {
        0: 'Bank Select MSB', 1: 'Modulation', 2: 'Breath Controller',
        4: 'Foot Pedal', 5: 'Portamento Time', 6: 'Data Entry MSB',
        7: 'Volume', 8: 'Balance', 10: 'Pan', 11: 'Expression',
        12: 'Effect Control 1', 13: 'Effect Control 2',
        16: 'General Purpose 1', 17: 'General Purpose 2',
        18: 'General Purpose 3', 19: 'General Purpose 4',
        20: 'Unregistered 20', 21: 'Unregistered 21', 22: 'Unregistered 22',
        26: 'Unregistered 26', 27: 'Unregistered 27',
        32: 'Bank Select LSB', 64: 'Sustain Pedal', 65: 'Portamento On/Off',
        66: 'Sostenuto', 67: 'Soft Pedal', 68: 'Legato', 69: 'Hold 2',
        91: 'Reverb', 92: 'Tremolo', 93: 'Chorus', 94: 'Detune', 95: 'Phaser',
        120: 'All Sound Off', 121: 'Reset All Controllers',
        122: 'Local Control', 123: 'All Notes Off',
    }

    if kind == 0xB0:
        cc_name = CC_NAMES.get(b1, f'CC#{b1}')
        return f'CC on Ch.{ch}  {cc_name} (#{b1}) = {b2}  [{hex_str}]'
    if kind == 0xC0:
        return f'Program Change on Ch.{ch}  PC={b1}  [{hex_str}]'
    if kind == 0x90:
        return f'Note On  Ch.{ch}  note={b1}  vel={b2}  [{hex_str}]'
    if kind == 0x80:
        return f'Note Off  Ch.{ch}  note={b1}  vel={b2}  [{hex_str}]'
    if kind == 0xE0:
        bend = (b2 << 7 | b1) - 8192
        return f'Pitch Bend  Ch.{ch}  value={bend}  [{hex_str}]'
    if kind == 0xD0:
        return f'Channel Pressure  Ch.{ch}  value={b1}  [{hex_str}]'
    if kind == 0xA0:
        return f'Poly Key Pressure  Ch.{ch}  note={b1}  value={b2}  [{hex_str}]'

    return f'raw hex: {hex_str}'


def _format_command(cmd: dict, index: int) -> str:
    """Format a single decoded MIDI command dict as a Markdown bullet block."""
    kind = cmd.get('kind', '')
    lines = [f'**Command {index}:** ', '']

    if kind == 'programChange':
        ch    = cmd.get('channel', '?')
        pc    = cmd.get('value', '?')
        msb   = cmd.get('msb', '?')
        lsb   = cmd.get('lsb', '?')
        lines[0] += 'Program Change'
        lines[1]  = (
            f'  - Channel: {ch}\n'
            f'  - Program (value): {pc}\n'
            f'  - Bank MSB: {msb}\n'
            f'  - Bank LSB: {lsb}'
        )
    elif kind == 'hex':
        hex_val = cmd.get('value', '')
        lines[0] += f'CC / raw hex  →  {_decode_hex_command(hex_val)}'
        lines[1]  = ''
    else:
        lines[0] += f'Unknown kind: `{kind}`'
        lines[1]  = f'  - raw: `{cmd}`'

    return lines[0] + ('\n' + lines[1] if lines[1] else '')


def extract_midi_doc(filepath: str, base_dir: Path) -> tuple[Path, int, int]:
    """
    Read &SYS;presets from the archive plist, decode each preset's MIDI
    commands and write a midi-presets.md file to base_dir (parallel to files/).

    Returns (path, preset_count, total_command_count).
    """
    plist = _read_plist_from_4sb(filepath)
    presets = plist.get('&SYS;presets', [])

    total_commands = 0

    lines = [
        '# forScore MIDI Presets',
        '',
        f'{len(presets)} presets found.',
        '',
        '---',
        '',
    ]

    for preset in presets:
        title = preset.get('title', '(untitled)')
        cmds_raw = preset.get('commands', b'')

        lines.append(f'## {title}')
        lines.append('')

        if not cmds_raw:
            lines.append('_(no commands)_')
            lines.append('')
            lines.append('---')
            lines.append('')
            continue

        try:
            cmds = _resolve_nska(cmds_raw)
        except Exception as e:
            lines.append(f'_(could not decode commands: {e})_')
            lines.append('')
            lines.append('---')
            lines.append('')
            continue

        if not cmds:
            lines.append('_(no commands)_')
        else:
            for i, cmd in enumerate(cmds, 1):
                lines.append(_format_command(cmd, i))
                lines.append('')
            total_commands += len(cmds)

        lines.append('---')
        lines.append('')

    out_path = base_dir / 'midi-presets.md'
    out_path.write_text('\n'.join(lines), encoding='utf-8')
    return out_path, len(presets), total_commands


# Record header format (ASCII, fixed structure, ends right before \x1f\x8b\x08):
#
#   Archive: <32-char numeric prefix>{%DOCUMENTS_DIR%}/<filename>
#   Backup:  <32-char numeric prefix><filename>
#
# The 32-char numeric prefix contains two right-aligned numbers separated by
# spaces: a record index and the compressed size of the following gzip block.
#
# Examples:
#   '              51         5378356{%DOCUMENTS_DIR%}/1-09 Have You Met Miss Jones_.mp3'
#   '              38           41096Ain\'t Misbehavin\' - Tenor Bb.pdf|2.png'
#   '              37           163202023-12-01_Set-List-Frechen.pdf|1.png'
#
# The fixed 32-char width is critical: without it the compressed_size cannot be
# parsed reliably when the filename starts with digits (e.g. a date like
# "2023-12-01_..."), because a naive regex would absorb the leading digits of
# the filename into the compressed_size number.

_NUMERIC_PREFIX_LEN = 32
_DOCUMENTS_DIR_PREFIX = '{%DOCUMENTS_DIR%}/'


def parse_record_header(filepath: str, pos: int) -> dict | None:
    """
    Read and parse the ASCII record header at `pos`.

    Returns a dict with:
        filename        - original filename from header
        compressed_size - byte length of the gzip data block
        gzip_start      - absolute file offset where gzip data begins
        next_pos        - absolute file offset of the next record header

    Returns None if no valid record is found (end of file or parse failure).
    """
    with open(filepath, 'rb') as f:
        f.seek(pos)
        buf = f.read(512)

    if not buf:
        return None

    gz_idx = buf.find(b'\x1f\x8b\x08')
    if gz_idx == -1:
        return None

    header_text = buf[:gz_idx].decode('latin-1', errors='replace')

    # Extract compressed_size from the fixed 32-char numeric prefix
    numeric_part = header_text[:_NUMERIC_PREFIX_LEN]
    nums = re.findall(r'\d+', numeric_part)
    if not nums:
        return None
    compressed_size = int(nums[-1])   # last number = compressed_size

    # Extract filename from the rest (strip optional {%DOCUMENTS_DIR%}/ prefix)
    path_part = header_text[_NUMERIC_PREFIX_LEN:]
    if path_part.startswith(_DOCUMENTS_DIR_PREFIX):
        path_part = path_part[len(_DOCUMENTS_DIR_PREFIX):]
    filename = path_part.strip() or None

    gzip_start = pos + gz_idx
    return {
        'filename':        filename,
        'compressed_size': compressed_size,
        'gzip_start':      gzip_start,
        'next_pos':        gzip_start + compressed_size,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _fmt_bytes(n: int | float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def _fmt_speed(bps: float) -> str:
    return _fmt_bytes(int(bps)) + '/s'


def _fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 86400 * 2:
        return '--:--'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h}h {m:02d}m {s:02d}s'
    if m:
        return f'{m}m {s:02d}s'
    return f'{s}s'


def _now() -> str:
    return datetime.now().strftime('%H:%M:%S')


def _clear_line():
    sys.stdout.write('\r\033[K')


def _bar(current: int, total: int, width: int = 35) -> str:
    if total == 0:
        pct, filled = 100, width
    else:
        pct   = int(100 * current / total)
        filled = int(width * current / total)
    arrow  = '>' if filled < width else ''
    spaces = width - filled - len(arrow)
    bar    = '=' * filled + arrow + ' ' * spaces
    return f'[{bar}] {current}/{total} ({pct}%)'


# ---------------------------------------------------------------------------
# Phase 1 – count records (fast: just follow headers)
# ---------------------------------------------------------------------------
def count_records(filepath: str, start_pos: int, file_size: int) -> int:
    """
    Walk all record headers sequentially and return the total count.
    This is fast because we only read 512-byte headers, not the data.
    """
    pos = start_pos
    count = 0
    t_start = time.monotonic()

    while pos < file_size:
        rec = parse_record_header(filepath, pos)
        if rec is None:
            break
        count += 1
        pos = rec['next_pos']

        elapsed = time.monotonic() - t_start
        pct = int(100 * pos / file_size)
        speed = (pos - start_pos) / elapsed if elapsed > 0 else 0
        eta   = (file_size - pos) / speed if speed > 0 else 0
        _clear_line()
        sys.stdout.write(
            f'Counting records...  {pct:3d}%  {_fmt_bytes(pos)} / {_fmt_bytes(file_size)}'
            f'  {_fmt_speed(speed)}  ETA {_fmt_eta(eta)}'
            f'  —  {count} records'
        )
        sys.stdout.flush()

    elapsed = time.monotonic() - t_start
    _clear_line()
    sys.stdout.write(
        f'Scan complete.  {count} records found  '
        f'({_fmt_eta(elapsed)}, {_fmt_speed((file_size - start_pos) / elapsed if elapsed > 0 else 0)})\n'
    )
    sys.stdout.flush()
    return count


# ---------------------------------------------------------------------------
# Phase 2 – extract
# ---------------------------------------------------------------------------
def extract_records(
    filepath: str,
    start_pos: int,
    file_size: int,
    total: int,
    out_dir: Path,
    verbose: bool,
) -> tuple[int, int, dict[str, int]]:
    """
    Walk records sequentially, decompress each one, write to out_dir.

    Returns:
        (extracted, skipped, ext_counts)
    """
    pos = start_pos
    extracted  = 0
    skipped    = 0
    fallback_n = 0
    ext_counts: dict[str, int] = defaultdict(int)
    bytes_read = 0

    t_start    = time.monotonic()
    t_last_min = t_start   # for 60-second periodic status lines

    with open(filepath, 'rb') as f:
        i = 0
        while pos < file_size:
            # -- progress bar --------------------------------------------------
            elapsed   = time.monotonic() - t_start
            speed_b   = bytes_read / elapsed if elapsed > 0 else 0
            remaining = (file_size - start_pos - bytes_read)
            eta       = remaining / speed_b if speed_b > 0 else 0
            _clear_line()
            sys.stdout.write(
                f'{_bar(i, total)}'
                f'  {_fmt_speed(speed_b)}  ETA {_fmt_eta(eta)}'
                f'  ok={extracted} skip={skipped}'
            )
            sys.stdout.flush()

            # -- 60-second periodic status (printed on a new line) -------------
            now = time.monotonic()
            if now - t_last_min >= 60:
                t_last_min = now
                print()
                print(
                    f'  [{_now()}  {_fmt_eta(elapsed)} elapsed]'
                    f'  {i}/{total} processed'
                    f'  {extracted} extracted  {skipped} skipped'
                    f'  {_fmt_bytes(bytes_read)} read'
                )

            # -- parse record header -------------------------------------------
            rec = parse_record_header(filepath, pos)
            if rec is None:
                break

            filename        = rec['filename']
            compressed_size = rec['compressed_size']
            gzip_start      = rec['gzip_start']

            # -- decompress ----------------------------------------------------
            try:
                f.seek(gzip_start)
                compressed = f.read(compressed_size)
                content    = gzip.decompress(compressed)
            except Exception:
                # Fallback: payload is not gzip-compressed, treat as raw data.
                # Some Archive.4sb variants store file payloads uncompressed
                # despite having gzip magic bytes in the record header area.
                content = compressed

            if not content:
                skipped += 1
                pos = rec['next_pos']
                bytes_read += compressed_size
                i += 1
                continue

            # -- determine output filename ------------------------------------
            if filename:
                out_name = filename.strip()
                # sanitize unsafe chars but keep the original name structure
                out_name = re.sub(r'[/\\:\x00]', '_', out_name)
                ext = Path(out_name).suffix.lower() or detect_extension(content)
            else:
                fallback_n += 1
                ext      = detect_extension(content)
                out_name = f'artifact_{fallback_n:05d}{ext}'

            # -- write ---------------------------------------------------------
            out_path = out_dir / out_name
            out_path.write_bytes(content)
            extracted  += 1
            ext_counts[ext] += 1
            bytes_read += compressed_size

            if verbose:
                _clear_line()
                print(f'  {out_name}  ({_fmt_bytes(len(content))})')

            pos = rec['next_pos']
            i  += 1

    # final bar
    elapsed = time.monotonic() - t_start
    _clear_line()
    sys.stdout.write(
        f'{_bar(extracted + skipped, total)}'
        f'  done in {_fmt_eta(elapsed)}'
        f'  ok={extracted} skip={skipped}\n'
    )
    sys.stdout.flush()

    return extracted, skipped, dict(ext_counts)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_input(filepath: str) -> None:
    p = Path(filepath)
    if not p.exists():
        sys.exit(f'Error: file not found: {filepath}')
    if not p.name.startswith(('Archiv', 'Archive')) or p.suffix.lower() != '.4sb':
        sys.exit(
            f'Error: expected an Archive file whose name starts with "Archiv" or "Archive" '
            f'and ends with ".4sb", got: {p.name}'
        )
    with open(filepath, 'rb') as f:
        header = f.read(_HEADER_SEARCH_WINDOW).decode('ascii', errors='replace')
    if '<--4SBV03-->' not in header:
        sys.exit(
            'Error: file does not appear to be a V03 Archive '
            '(header marker "<--4SBV03-->" not found).'
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Extract embedded files from a forScore Archive (.4sb V03).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        'input', metavar='INPUT.4sb',
        help='Path to a forScore Archive file (Archiv*.4sb)',
    )
    parser.add_argument(
        '-o', '--output-dir', metavar='OUTPUT_DIR',
        help='Base output directory (default: <input-stem>/ next to input file)',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print each extracted filename and size',
    )
    parser.add_argument(
        '--midi-doc', action='store_true',
        help='Decode MIDI presets from the archive metadata and write midi-presets.md to the output directory',
    )
    args = parser.parse_args()

    validate_input(args.input)

    p         = Path(args.input).resolve()
    file_size = p.stat().st_size

    base_dir = Path(args.output_dir) if args.output_dir else p.parent / p.stem
    out_dir  = base_dir / 'files'
    out_dir.mkdir(parents=True, exist_ok=True)

    t_wall_start = time.monotonic()
    ts_start     = _now()

    print(f'Started:  {ts_start}')
    print(f'Input:    {p.name} ({_fmt_bytes(file_size)})')
    print(f'Output:   {out_dir}')
    print()

    try:
        metadata_end = read_metadata_end(str(p))
    except ValueError as e:
        sys.exit(f'Error reading metadata: {e}')

    payload_size = file_size - metadata_end
    print(f'Metadata: {_fmt_bytes(metadata_end)}  |  Payload: {_fmt_bytes(payload_size)}')
    print()

    # Phase 1 – count records
    print('Phase 1 — Counting records...')
    total = count_records(str(p), metadata_end, file_size)
    print()

    if total == 0:
        print('No records found. Nothing to extract.')
        sys.exit(0)

    # Phase 2 – extract
    print(f'Phase 2 — Extracting {total} records...')
    extracted, skipped, ext_counts = extract_records(
        str(p), metadata_end, file_size, total, out_dir, args.verbose
    )
    print()

    # Phase 3 – MIDI doc (optional)
    midi_path = None
    midi_presets = 0
    midi_commands = 0
    if args.midi_doc:
        print('Phase 3 — Generating MIDI presets doc...')
        try:
            midi_path, midi_presets, midi_commands = extract_midi_doc(str(p), base_dir)
            print(f'  Presets:  {midi_presets}')
            print(f'  Commands: {midi_commands}')
            print(f'  Written:  {midi_path}')
        except Exception as e:
            print(f'  Warning: MIDI doc generation failed: {e}')
        print()

    ts_end   = _now()
    elapsed  = time.monotonic() - t_wall_start

    # Summary
    print('=' * 52)
    print('SUMMARY')
    print('=' * 52)
    print(f'  Started:           {ts_start}')
    print(f'  Finished:          {ts_end}')
    print(f'  Duration:          {_fmt_eta(elapsed)}')
    print()
    print(f'  Blocks scanned:    {total:>6}')
    print(f'  Files extracted:   {extracted:>6}')
    print(f'  Skipped (corrupt): {skipped:>6}')
    if ext_counts:
        print()
        print('  By file type:')
        for ext, count in sorted(ext_counts.items(), key=lambda x: -x[1]):
            label = ext if ext else '(no ext)'
            print(f'    {label:<12}  {count:>6}')
    print('=' * 52)
    print(f'  Output: {out_dir}')
    if midi_path:
        print(f'  MIDI doc: {midi_path}')
        print(f'    {midi_presets} presets, {midi_commands} commands')
    print('=' * 52)


if __name__ == '__main__':
    main()
