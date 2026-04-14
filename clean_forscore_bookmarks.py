#!/usr/bin/env python3
"""
Clean duplicate bookmarks from a forScore .4sb Backup (V02) or Archive (V03) file
while preserving all embedded binary records (PDFs, MP3s, draw-annotation PNGs).

Key improvement over v1
-----------------------
v1 silently discarded all embedded-file records after writing the cleaned metadata,
which destroyed draw annotations (ink strokes stored as PNG per page).

v2 copies every record byte-for-byte from the original file into the output file.
Because the record filename format encodes both the PDF name and the page number
(<pdfname>|<page>.png), the association between drawings and scores remains valid
after deduplication: dedup only removes bookmark *entries* in the plist -- the PDF
filenames themselves never change.

Supported file types
--------------------
  V02 Backup  (Backup*.4sb) -- metadata plist + PNG draw annotations only
  V03 Archive (Archiv*.4sb) -- metadata plist + PDFs + audio files + PNG annotations

Output
------
  <output_dir>/<input_stem>-cleaned.4sb

  Default output directory: <input_stem>/ next to the input file.

Usage
-----
  clean_forscore_bookmarks_v2.py <input.4sb> [-o OUTPUT_DIR]
                                 [--dry-run]
                                 [-m | --merge-meta]
                                 [--page2item]
                                 [--last-page-fix]
                                 [--no-dedup]
"""

import argparse
import gzip
import io
import json
import os
import plistlib
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADER_SIZE        = 74          # fixed ASCII file header
NUMERIC_PREFIX_LEN = 32          # fixed-width numeric prefix in each record header
DOCUMENTS_PREFIX   = '{%DOCUMENTS_DIR%}/'
AUX_PREFIX         = '{%AUX_DIR%}/'


# ---------------------------------------------------------------------------
# Progress-display helpers (shared with extract_binaries_forscore_backup.py)
# ---------------------------------------------------------------------------
def _fmt_bytes(n: float) -> str:
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
        pct    = int(100 * current / total)
        filled = int(width * current / total)
    arrow  = '>' if filled < width else ''
    spaces = width - filled - len(arrow)
    bar    = '=' * filled + arrow + ' ' * spaces
    return f'[{bar}] {current}/{total} ({pct}%)'


# ---------------------------------------------------------------------------
# forScore file-header parsing
# ---------------------------------------------------------------------------
def read_file_header(filepath: str) -> dict:
    """
    Read and parse the 74-byte ASCII file header.

    Returns a dict with keys:
        version             - '4SBV02' or '4SBV03'
        is_archive          - True for V03
        is_backup           - True for V02
        metadata_size       - byte length of the metadata gzip block
        metadata_end        - byte offset where the record section begins
        raw                 - the raw 74-byte header bytes
        filename_in_header  - original filename embedded in the header
    """
    with open(filepath, 'rb') as f:
        raw = f.read(HEADER_SIZE)
    text = raw.decode('ascii', errors='replace')

    m = re.search(r'(\d{5,12})(Backup|Archiv)', text)
    if not m:
        raise ValueError(f"Cannot parse metadata size from file header: {text!r}")

    metadata_size = int(m.group(1))
    metadata_end  = HEADER_SIZE + metadata_size

    version_match = re.search(r'4SBV0[23]', text)
    version       = version_match.group(0) if version_match else 'Unknown'

    fn_match = re.search(r'(?:Backup|Archiv).*', text)
    filename_in_header = fn_match.group(0).strip() if fn_match else ''

    return {
        'version':            version,
        'is_archive':         version == '4SBV03',
        'is_backup':          version == '4SBV02',
        'metadata_size':      metadata_size,
        'metadata_end':       metadata_end,
        'raw':                raw,
        'filename_in_header': filename_in_header,
    }


def rebuild_header(original_raw: bytes, new_metadata_size: int) -> bytes:
    """
    Return a new 74-byte header identical to original_raw except the metadata
    size field is updated.  Header length (74 bytes) is preserved exactly.
    """
    text = original_raw.decode('ascii', errors='replace')
    m    = re.search(r'(\d{5,12})(Backup|Archiv)', text)
    if not m:
        return original_raw   # cannot rewrite; return original unchanged

    old_str = m.group(1)
    new_str = str(new_metadata_size)

    # Right-align within the same character width as original
    padded   = new_str.rjust(len(old_str)) if len(new_str) <= len(old_str) else new_str
    new_text = text[:m.start(1)] + padded + text[m.end(1):]

    # Truncate or pad to exactly HEADER_SIZE bytes
    encoded = new_text.encode('ascii', errors='replace')
    if len(encoded) < HEADER_SIZE:
        encoded = encoded + b' ' * (HEADER_SIZE - len(encoded))
    elif len(encoded) > HEADER_SIZE:
        encoded = encoded[:HEADER_SIZE]
    return encoded


# ---------------------------------------------------------------------------
# Metadata (plist) extraction
# ---------------------------------------------------------------------------
def read_plist(filepath: str, metadata_end: int) -> dict:
    """Read and decompress the metadata block; return the parsed plist dict."""
    metadata_size = metadata_end - HEADER_SIZE
    with open(filepath, 'rb') as f:
        f.seek(HEADER_SIZE)
        gz_bytes = f.read(metadata_size)
    raw_plist = gzip.decompress(gz_bytes)
    if raw_plist[:8] != b'bplist00':
        raise ValueError("Metadata block is not an Apple Binary plist (bplist00).")
    return plistlib.loads(raw_plist)


def compress_plist(plist: dict) -> bytes:
    """Serialize and gzip-compress a plist dict.  Returns compressed bytes."""
    raw = plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=9) as gz:
        gz.write(raw)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Record-header parsing (sequential, no magic-byte scan)
# ---------------------------------------------------------------------------
def parse_record_header(filepath: str, pos: int) -> dict | None:
    """
    Read and parse the ASCII record header at `pos`.

    Returns a dict with:
        raw_header      - raw bytes from pos up to (not including) the gzip magic
        filename        - decoded filename (path prefixes stripped)
        raw_filename    - full filename as it appears in the record header
        compressed_size - byte length of the gzip data block
        gzip_start      - absolute file offset of the gzip magic bytes
        next_pos        - absolute offset of the next record header

    Returns None at end-of-file or if no gzip marker is found.
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

    # compressed_size is the last number in the fixed 32-char numeric prefix
    numeric_part = header_text[:NUMERIC_PREFIX_LEN]
    nums         = re.findall(r'\d+', numeric_part)
    if not nums:
        return None
    compressed_size = int(nums[-1])

    # Full filename (with any prefix intact) starts at character 32
    raw_filename = header_text[NUMERIC_PREFIX_LEN:].strip() or None

    # Strip known path prefixes for the clean filename
    filename = raw_filename
    if filename:
        if filename.startswith(DOCUMENTS_PREFIX):
            filename = filename[len(DOCUMENTS_PREFIX):]
        elif filename.startswith(AUX_PREFIX):
            filename = filename[len(AUX_PREFIX):]

    gzip_start = pos + gz_idx
    return {
        'raw_header':      buf[:gz_idx],
        'filename':        filename,
        'raw_filename':    raw_filename,
        'compressed_size': compressed_size,
        'gzip_start':      gzip_start,
        'next_pos':        gzip_start + compressed_size,
    }


def count_records(filepath: str, start_pos: int, file_size: int) -> int:
    """Walk all record headers and return the total count (fast: headers only)."""
    pos   = start_pos
    count = 0
    t0    = time.monotonic()

    while pos < file_size:
        rec = parse_record_header(filepath, pos)
        if rec is None:
            break
        count += 1
        pos    = rec['next_pos']

        elapsed = time.monotonic() - t0
        pct     = int(100 * pos / file_size)
        speed   = (pos - start_pos) / elapsed if elapsed > 0 else 0
        eta     = (file_size - pos) / speed   if speed  > 0 else 0
        _clear_line()
        sys.stdout.write(
            f'  Scanning...  {pct:3d}%  {_fmt_bytes(pos)}/{_fmt_bytes(file_size)}'
            f'  {_fmt_speed(speed)}  ETA {_fmt_eta(eta)}  --  {count} records'
        )
        sys.stdout.flush()

    elapsed = time.monotonic() - t0
    _clear_line()
    speed_avg = (file_size - start_pos) / elapsed if elapsed > 0 else 0
    print(f'  Scan done: {count} records  ({_fmt_eta(elapsed)}, {_fmt_speed(speed_avg)})')
    return count


# ---------------------------------------------------------------------------
# Bookmark cleaning (identical logic to v1, re-implemented cleanly)
# ---------------------------------------------------------------------------
def find_bookmark_lists(plist: dict) -> list:
    """
    Return all dicts describing every '|bookmarks' key found.
    Setlist keys (&SET;...) and other non-bookmark keys are excluded.
    """
    result = []
    for key, value in plist.items():
        if key.endswith('|bookmarks') and isinstance(value, list):
            result.append({'parent': plist, 'key': key, 'bookmarks': value})
    return result


def _bookmark_dedup_key(bm: dict) -> tuple:
    return (bm.get('FilePath', ''), bm.get('First Page', 0), bm.get('Title', ''))


def deduplicate_bookmarks(bookmark_list: list, merge_meta: bool = False) -> tuple:
    """
    Remove duplicates from a flat bookmark list.
    Returns (unique_list, n_removed).
    If merge_meta=True, missing fields in the kept entry are filled from duplicates.
    """
    seen      = {}
    unique    = []
    n_removed = 0
    IDENTITY_FIELDS = {'FilePath', 'First Page', 'Last Page', 'Title', 'Identifier'}

    for bm in bookmark_list:
        key = _bookmark_dedup_key(bm)
        if key in seen:
            if merge_meta and isinstance(bm, dict) and isinstance(seen[key], dict):
                for field, value in bm.items():
                    if field in IDENTITY_FIELDS:
                        continue
                    if seen[key].get(field) in (None, ''):
                        if value not in (None, ''):
                            seen[key][field] = value
            n_removed += 1
        else:
            seen[key] = bm
            unique.append(bm)

    return unique, n_removed


def fix_last_page(plist: dict) -> int:
    """Set Last Page = First Page wherever Last Page == 0.  Returns fix count."""
    count = 0
    for key, value in plist.items():
        if not key.endswith('|bookmarks') or not isinstance(value, list):
            continue
        for bm in value:
            if isinstance(bm, dict) and bm.get('Last Page') == 0:
                bm['Last Page'] = bm.get('First Page', 0)
                count += 1
    return count


def copy_score_meta_to_bookmarks(plist: dict) -> int:
    """
    For every bookmark where Last Page == First Page (page-bookmark style),
    copy available score-level metadata into the bookmark dict if not already
    present.  Returns count of bookmarks touched.
    """
    META_MAP = {
        'composer':  'Composer',
        'genre':     'Genre',
        'keywords':  'Keyword',
        'key':       'Key',
        'bpm':       'BPM',
        'signature': 'Signature',
    }
    count = 0
    for key, value in plist.items():
        if not key.endswith('|bookmarks') or not isinstance(value, list):
            continue
        filepath = key[:-len('|bookmarks')]
        score_meta = {}
        for src_key, dst_key in META_MAP.items():
            full_key = f'{filepath}|{src_key}'
            if full_key in plist and plist[full_key] not in (None, ''):
                score_meta[dst_key] = plist[full_key]
        if not score_meta:
            continue
        for bm in value:
            if not isinstance(bm, dict):
                continue
            lp = bm.get('Last Page')
            fp = bm.get('First Page')
            if lp == fp:   # page-style bookmark
                touched = False
                for dst_key, val in score_meta.items():
                    if bm.get(dst_key) in (None, ''):
                        bm[dst_key] = val
                        touched = True
                if touched:
                    count += 1
    return count


def clean_plist(plist: dict,
                no_dedup:      bool = False,
                merge_meta:    bool = False,
                last_page_fix: bool = False,
                page2item:     bool = False) -> dict:
    """
    Apply all requested cleaning operations to the plist in-place.
    Returns a stats dict with counts of changes.
    """
    stats = {
        'bookmark_lists':   0,
        'total_bookmarks':  0,
        'removed_dupes':    0,
        'last_page_fixed':  0,
        'page2item_copied': 0,
    }

    bookmark_lists = find_bookmark_lists(plist)
    stats['bookmark_lists']  = len(bookmark_lists)
    stats['total_bookmarks'] = sum(len(bl['bookmarks']) for bl in bookmark_lists)

    if not no_dedup:
        total_removed = 0
        for bl in bookmark_lists:
            orig    = bl['bookmarks']
            unique, n_removed = deduplicate_bookmarks(orig, merge_meta=merge_meta)
            bl['parent'][bl['key']] = unique
            total_removed += n_removed
            if n_removed > 0:
                print(f"    {bl['key']}: {len(orig)} -> {len(unique)} (-{n_removed})")
        stats['removed_dupes'] = total_removed

    if last_page_fix or page2item:
        stats['last_page_fixed'] = fix_last_page(plist)

    if page2item:
        stats['page2item_copied'] = copy_score_meta_to_bookmarks(plist)

    return stats


# ---------------------------------------------------------------------------
# Writing the output .4sb file
# ---------------------------------------------------------------------------
def write_cleaned_4sb(
    input_path:          str,
    output_path:         str,
    original_raw_header: bytes,
    new_metadata_gz:     bytes,
    metadata_end:        int,
    file_size:           int,
    total_records:       int,
) -> None:
    """
    Write a new .4sb file:
      1. Updated 74-byte file header (metadata_size updated)
      2. New metadata gzip block (cleaned plist)
      3. All original records copied verbatim (record header + compressed data)

    Records are streamed in 8 MB chunks for memory efficiency on large archives.
    """
    COPY_CHUNK = 8 * 1024 * 1024   # 8 MB

    new_header = rebuild_header(original_raw_header, len(new_metadata_gz))

    t0           = time.monotonic()
    t_last_min   = t0
    bytes_copied = 0
    records_done = 0
    pos          = metadata_end

    with open(input_path, 'rb') as src, open(output_path, 'wb') as dst:
        # 1. Write updated file header
        dst.write(new_header)

        # 2. Write new metadata gzip
        dst.write(new_metadata_gz)

        # 3. Copy all records verbatim
        while pos < file_size:
            elapsed   = time.monotonic() - t0
            speed     = bytes_copied / elapsed if elapsed > 0 else 0
            remaining = (file_size - metadata_end) - bytes_copied
            eta       = remaining / speed if speed > 0 else 0

            _clear_line()
            sys.stdout.write(
                f'  {_bar(records_done, total_records)}'
                f'  {_fmt_speed(speed)}  ETA {_fmt_eta(eta)}'
            )
            sys.stdout.flush()

            # Read record header (up to 512 bytes to find gzip magic)
            src.seek(pos)
            buf = src.read(512)
            if not buf:
                break

            gz_idx = buf.find(b'\x1f\x8b\x08')
            if gz_idx == -1:
                break   # no more records

            header_text = buf[:gz_idx].decode('latin-1', errors='replace')
            nums        = re.findall(r'\d+', header_text[:NUMERIC_PREFIX_LEN])
            if not nums:
                break
            compressed_size = int(nums[-1])

            gzip_start = pos + gz_idx
            next_pos   = gzip_start + compressed_size

            # Write ASCII record header bytes verbatim
            dst.write(buf[:gz_idx])

            # Stream compressed gzip data in chunks
            bytes_remaining = compressed_size
            src.seek(gzip_start)
            while bytes_remaining > 0:
                chunk_size = min(COPY_CHUNK, bytes_remaining)
                chunk      = src.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                bytes_remaining -= len(chunk)

            bytes_copied += gz_idx + compressed_size   # header + data
            records_done += 1
            pos = next_pos

            # 60-second periodic status line
            now = time.monotonic()
            if now - t_last_min >= 60:
                t_last_min = now
                elapsed2   = time.monotonic() - t0
                print()
                print(
                    f'  [{_now()}  {_fmt_eta(elapsed2)} elapsed]'
                    f'  {records_done}/{total_records} records copied'
                    f'  {_fmt_bytes(bytes_copied)} written'
                )

    elapsed = time.monotonic() - t0
    _clear_line()
    print(
        f'  {_bar(records_done, total_records)}'
        f'  done in {_fmt_eta(elapsed)}'
        f'  ({records_done} records, {_fmt_bytes(bytes_copied)})'
    )


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
            f'Error: file does not appear to be a forScore .4sb file '
            f'(neither 4SBV02 nor 4SBV03 found in header).'
        )


# ---------------------------------------------------------------------------
# JSON export helper
# ---------------------------------------------------------------------------
def _to_json_safe(obj):
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json_safe(i) for i in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, bytes):
        return obj.hex() if len(obj) < 1000 else f'<{len(obj)} bytes>'
    return str(obj)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=(
            'Clean duplicate bookmarks from a forScore .4sb file, '
            'preserving all embedded records (PDFs, MP3s, annotation PNGs).'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        'input', metavar='INPUT.4sb',
        help='Path to a forScore Backup (V02) or Archive (V03) .4sb file',
    )
    parser.add_argument(
        '-o', '--output-dir', metavar='OUTPUT_DIR',
        help='Output directory (default: <input-stem>/ next to input file)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Analyse only; do not write any output files',
    )
    parser.add_argument(
        '-m', '--merge-meta', action='store_true',
        help='Merge metadata from duplicate bookmarks into the kept entry',
    )
    parser.add_argument(
        '--no-dedup', action='store_true',
        help='Skip bookmark deduplication (still applies --last-page-fix / --page2item if set)',
    )
    parser.add_argument(
        '--last-page-fix', action='store_true',
        help='Set Last Page = First Page wherever Last Page is 0',
    )
    parser.add_argument(
        '--page2item', action='store_true',
        help=(
            'Like --last-page-fix but also copies score-level metadata '
            '(Composer, Genre, Key, BPM...) into page-style bookmarks'
        ),
    )
    args = parser.parse_args()

    validate_input(args.input)

    p         = Path(args.input).resolve()
    file_size = p.stat().st_size
    ts_start  = _now()

    print(f'Started:  {ts_start}')
    print(f'Input:    {p.name}  ({_fmt_bytes(file_size)})')

    # --- Parse file header ---------------------------------------------------
    try:
        hdr = read_file_header(str(p))
    except ValueError as e:
        sys.exit(f'Error reading file header: {e}')

    file_type = 'Archive (V03)' if hdr['is_archive'] else 'Backup (V02)'
    print(f'Type:     {file_type}')
    print(
        f'Metadata: {_fmt_bytes(hdr["metadata_size"])} compressed  '
        f'(records start at offset {hdr["metadata_end"]:,})'
    )
    print()

    # --- Determine output paths ----------------------------------------------
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = p.parent / p.stem

    out_4sb  = out_dir / f'{p.stem}-cleaned.4sb'
    out_json = out_dir / f'{p.stem}-cleaned.json'

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f'Output:   {out_4sb}')
        print()

    # --- Read and parse plist ------------------------------------------------
    print('Reading metadata...')
    try:
        plist = read_plist(str(p), hdr['metadata_end'])
    except Exception as e:
        sys.exit(f'Error reading metadata plist: {e}')
    print(f'  Parsed plist: {len(plist):,} top-level keys')
    print()

    # --- Count records (Phase 1) ---------------------------------------------
    payload_size = file_size - hdr['metadata_end']
    print(f'Phase 1 -- Counting records  (payload {_fmt_bytes(payload_size)})...')
    total_records = count_records(str(p), hdr['metadata_end'], file_size)
    print()

    # --- Clean plist ---------------------------------------------------------
    print('Cleaning plist...')
    if args.no_dedup:
        print('  Deduplication: SKIPPED (--no-dedup)')
    stats = clean_plist(
        plist,
        no_dedup=args.no_dedup,
        merge_meta=args.merge_meta,
        last_page_fix=args.last_page_fix,
        page2item=args.page2item,
    )

    print()
    print('Cleaning summary:')
    print(f'  Bookmark lists processed:  {stats["bookmark_lists"]:>6,}')
    print(f'  Total bookmarks (before):  {stats["total_bookmarks"]:>6,}')
    if not args.no_dedup:
        after = stats['total_bookmarks'] - stats['removed_dupes']
        pct   = (
            100.0 * stats['removed_dupes'] / stats['total_bookmarks']
            if stats['total_bookmarks'] else 0.0
        )
        print(f'  Duplicates removed:        {stats["removed_dupes"]:>6,}  ({pct:.1f}%)')
        print(f'  Bookmarks after:           {after:>6,}')
    if stats['last_page_fixed']:
        print(f'  Last Page = 0 fixed:       {stats["last_page_fixed"]:>6,}')
    if stats['page2item_copied']:
        print(f'  Page->Item meta copied:    {stats["page2item_copied"]:>6,}')
    print()

    if args.dry_run:
        print('Dry run -- no output written.')
        return

    # --- Compress new plist --------------------------------------------------
    print('Compressing cleaned metadata...')
    new_metadata_gz = compress_plist(plist)
    old_sz = hdr['metadata_size']
    new_sz = len(new_metadata_gz)
    delta  = new_sz - old_sz
    sign   = '+' if delta >= 0 else ''
    print(f'  {_fmt_bytes(old_sz)}  ->  {_fmt_bytes(new_sz)}  ({sign}{_fmt_bytes(abs(delta))} {"more" if delta >= 0 else "less"})')
    print()

    # --- Save cleaned JSON (for debugging) -----------------------------------
    print('Writing debug JSON...')
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(_to_json_safe(plist), f, indent=2, ensure_ascii=False)
    print(f'  {out_json}')
    print()

    # --- Write output .4sb (Phase 2) -----------------------------------------
    print(f'Phase 2 -- Writing cleaned .4sb  ({total_records} records to copy)...')
    write_cleaned_4sb(
        input_path=str(p),
        output_path=str(out_4sb),
        original_raw_header=hdr['raw'],
        new_metadata_gz=new_metadata_gz,
        metadata_end=hdr['metadata_end'],
        file_size=file_size,
        total_records=total_records,
    )
    print()

    # --- Final summary -------------------------------------------------------
    out_size  = out_4sb.stat().st_size
    ts_end    = _now()
    size_diff = out_size - file_size
    sign      = '+' if size_diff >= 0 else ''

    print('=' * 56)
    print('DONE')
    print('=' * 56)
    print(f'  Started:          {ts_start}')
    print(f'  Finished:         {ts_end}')
    print()
    print(f'  Input:            {_fmt_bytes(file_size)}')
    print(f'  Output:           {_fmt_bytes(out_size)}')
    print(f'  Size difference:  {sign}{_fmt_bytes(abs(size_diff))} (metadata cleaned)')
    print()
    print(f'  Records copied:   {total_records:,}')
    print(f'  Dupes removed:    {stats["removed_dupes"]:,}')
    print()
    print(f'  Output file:      {out_4sb}')
    print(f'  Debug JSON:       {out_json}')
    print('=' * 56)


if __name__ == '__main__':
    main()
