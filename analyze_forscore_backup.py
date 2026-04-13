#!/usr/bin/env python3
"""
Analyze and extract data from forScore .4sb backup files.

The .4sb format appears to be:
- Header: <--4SBV02--> + metadata (74 bytes)
- Gzip-compressed binary plist data
"""

import struct
import zlib
import plistlib
import sqlite3
import json
import sys
from pathlib import Path
from collections import defaultdict


def extract_4sb_file(filepath):
    """Extract and parse a .4sb backup file."""
    print(f"Analyzing: {filepath}")
    
    with open(filepath, 'rb') as f:
        data = f.read()
    
    print(f"File size: {len(data):,} bytes")
    
    # Find gzip magic number
    offset = data.find(b'\x1f\x8b\x08')
    if offset == -1:
        raise ValueError("Gzip magic number not found")
    
    header = data[:offset]
    print(f"Header: {header.decode('utf-8', errors='ignore')}")
    print(f"Header length: {len(header)} bytes")
    
    # Parse gzip header
    if data[offset:offset+2] != b'\x1f\x8b':
        raise ValueError("Invalid gzip header")
    
    method = data[offset+2]
    flags = data[offset+3]
    
    # Calculate header size (10 bytes base + optional fields)
    header_size = 10
    if flags & 0x04:  # FEXTRA
        if len(data) > offset + header_size + 2:
            extra_len = struct.unpack('<H', data[offset+header_size:offset+header_size+2])[0]
            header_size += 2 + extra_len
    if flags & 0x08:  # FNAME
        name_end = data.find(b'\x00', offset + header_size)
        if name_end != -1:
            header_size = name_end - offset + 1
    if flags & 0x10:  # FCOMMENT
        comment_end = data.find(b'\x00', offset + header_size)
        if comment_end != -1:
            header_size = comment_end - offset + 1
    if flags & 0x02:  # FHCRC
        header_size += 2
    
    # Decompress using zlib (deflate)
    compressed_start = offset + header_size
    print(f"Decompressing from offset {compressed_start}...")
    
    try:
        decompressed = zlib.decompress(data[compressed_start:], -zlib.MAX_WBITS)
        print(f"✓ Decompressed {len(decompressed):,} bytes")
    except Exception as e:
        raise ValueError(f"Decompression failed: {e}")
    
    # Check format
    if decompressed[:8] == b'bplist00':
        print("✓ Format: Binary Property List (bplist)")
        return parse_plist(decompressed)
    elif decompressed[:16] == b'SQLite format 3\x00':
        print("✓ Format: SQLite database")
        return parse_sqlite(decompressed)
    else:
        print(f"Unknown format. First 100 bytes: {decompressed[:100]}")
        return None


def parse_plist(data):
    """Parse binary plist data."""
    try:
        plist = plistlib.loads(data)
        print(f"✓ Successfully parsed plist")
        
        # Analyze structure
        if isinstance(plist, dict):
            print(f"\nTop-level keys (first 10): {list(plist.keys())[:10]}")

            # Look for bookmarks (legacy detail output)
            bookmark_data = find_bookmarks(plist)
            return {
                'format': 'plist',
                'data': plist,
                'bookmarks': bookmark_data,
                'summary': summarize_plist(plist),
            }
        elif isinstance(plist, list):
            print(f"\nPlist is a list with {len(plist)} items")
            return {
                'format': 'plist',
                'data': plist
            }
        else:
            print(f"\nPlist type: {type(plist)}")
            return {
                'format': 'plist',
                'data': plist
            }
    except Exception as e:
        print(f"Error parsing plist: {e}")
        import traceback
        traceback.print_exc()
        return None


def find_bookmarks(data, path=""):
    """Recursively find bookmark data in plist structure."""
    bookmarks = []
    
    if isinstance(data, dict):
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key
            if 'bookmark' in key.lower() or 'mark' in key.lower():
                print(f"Found bookmark-related key: {current_path}")
                if isinstance(value, (list, dict)):
                    bookmarks.append({
                        'path': current_path,
                        'data': value
                    })
            bookmarks.extend(find_bookmarks(value, current_path))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            bookmarks.extend(find_bookmarks(item, f"{path}[{i}]"))
    
    return bookmarks


def summarize_plist(plist):
    """Compute summary statistics from a parsed forScore plist.

    Returns a dict with:
      - num_songs:              distinct PDFs that have at least one metadata key
      - num_setlists:           number of setlists
      - num_bookmarks:          total bookmark entries (including duplicates)
      - num_unique_bookmarks:   unique bookmarks after deduplication
      - num_duplicate_bookmarks: bookmarks that are duplicates
    """
    if not isinstance(plist, dict):
        return None

    # --- Songs: any key of the form "<filepath>|<suffix>" where filepath ends in
    #     a known score extension.  We count distinct file paths.
    score_extensions = {'.pdf', '.PDF'}
    score_filepaths = set()
    for key in plist:
        if '|' in key and not key.startswith('&'):
            filepath = key.split('|')[0]
            if any(filepath.endswith(ext) for ext in score_extensions):
                score_filepaths.add(filepath)

    # --- Setlists
    setlists = plist.get('&SYS;setlists', [])
    num_setlists = len(setlists)

    # --- Bookmarks: only keys ending in '|bookmarks' (excludes &SET; entries)
    total_bookmarks = 0
    unique_bookmarks = 0
    duplicate_bookmarks = 0

    for key, value in plist.items():
        if not key.endswith('|bookmarks') or not isinstance(value, list):
            continue
        total_bookmarks += len(value)
        seen = set()
        for bm in value:
            if not isinstance(bm, dict):
                continue
            dedup_key = (
                bm.get('FilePath', ''),
                bm.get('First Page', 0),
                bm.get('Title', ''),
            )
            if dedup_key in seen:
                duplicate_bookmarks += 1
            else:
                seen.add(dedup_key)
                unique_bookmarks += 1

    return {
        'num_songs': len(score_filepaths),
        'num_setlists': num_setlists,
        'num_bookmarks': total_bookmarks,
        'num_unique_bookmarks': unique_bookmarks,
        'num_duplicate_bookmarks': duplicate_bookmarks,
    }


def print_summary(stats):
    """Print a formatted summary table."""
    if stats is None:
        print("\nNo summary available (plist format not recognised).")
        return

    dup_pct = (
        100.0 * stats['num_duplicate_bookmarks'] / stats['num_bookmarks']
        if stats['num_bookmarks'] > 0 else 0.0
    )

    print("\n" + "=" * 40)
    print("SUMMARY")
    print("=" * 40)
    print(f"  Songs (PDFs):          {stats['num_songs']:>8,}")
    print(f"  Setlists:              {stats['num_setlists']:>8,}")
    print(f"  Bookmarks (total):     {stats['num_bookmarks']:>8,}")
    print(f"  Bookmarks (unique):    {stats['num_unique_bookmarks']:>8,}")
    print(f"  Bookmarks (duplicate): {stats['num_duplicate_bookmarks']:>8,}  ({dup_pct:.1f}%)")
    print("=" * 40)


def parse_sqlite(data):
    """Parse SQLite database."""
    # Write to temp file
    temp_db = Path('temp_forscore_backup.db')
    with open(temp_db, 'wb') as f:
        f.write(data)
    
    print(f"Saved SQLite database to: {temp_db}")
    
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    
    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"\nFound {len(tables)} tables:")
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        print(f"  - {table}: {count} rows")
    
    # Look for bookmark tables
    bookmark_tables = [t for t in tables if 'bookmark' in t.lower() or 'mark' in t.lower()]
    
    bookmark_data = {}
    for table in bookmark_tables:
        print(f"\nAnalyzing table: {table}")
        cursor.execute(f"SELECT * FROM {table} LIMIT 5")
        columns = [desc[0] for desc in cursor.description]
        print(f"  Columns: {columns}")
        rows = cursor.fetchall()
        print(f"  Sample rows: {len(rows)}")
        
        # Get all data
        cursor.execute(f"SELECT * FROM {table}")
        all_rows = cursor.fetchall()
        bookmark_data[table] = {
            'columns': columns,
            'rows': all_rows
        }
    
    conn.close()
    
    return {
        'format': 'sqlite',
        'tables': tables,
        'bookmarks': bookmark_data,
        'db_path': str(temp_db)
    }


def analyze_bookmarks(bookmark_data):
    """Analyze bookmarks for duplicates."""
    print("\n" + "="*60)
    print("BOOKMARK ANALYSIS")
    print("="*60)
    
    if not bookmark_data:
        print("No bookmark data found")
        return
    
    if isinstance(bookmark_data, dict):
        # SQLite format
        for table_name, table_info in bookmark_data.items():
            print(f"\nTable: {table_name}")
            columns = table_info['columns']
            rows = table_info['rows']
            
            print(f"Total rows: {len(rows)}")
            
            # Try to identify key columns for duplicate detection
            # Common patterns: score_id, page, title, etc.
            key_columns = []
            for col in columns:
                if any(keyword in col.lower() for keyword in ['id', 'score', 'page', 'title', 'name']):
                    key_columns.append(col)
            
            if key_columns:
                print(f"Key columns for duplicate detection: {key_columns}")
                
                # Find duplicates
                seen = {}
                duplicates = []
                
                for row in rows:
                    row_dict = dict(zip(columns, row))
                    # Create a key from key columns
                    key_parts = [str(row_dict.get(col, '')) for col in key_columns]
                    key = tuple(key_parts)
                    
                    if key in seen:
                        duplicates.append({
                            'original': seen[key],
                            'duplicate': row_dict
                        })
                    else:
                        seen[key] = row_dict
                
                print(f"Unique bookmarks: {len(seen)}")
                print(f"Duplicates found: {len(duplicates)}")
                
                if duplicates:
                    print("\nSample duplicates:")
                    for i, dup in enumerate(duplicates[:5]):
                        print(f"\n  Duplicate {i+1}:")
                        print(f"    Original: {dup['original']}")
                        print(f"    Duplicate: {dup['duplicate']}")
    
    elif isinstance(bookmark_data, list):
        # Plist format
        print(f"Found {len(bookmark_data)} bookmark-related structures")
        for item in bookmark_data:
            print(f"\nPath: {item['path']}")
            print(f"Type: {type(item['data'])}")
            if isinstance(item['data'], list):
                print(f"Items: {len(item['data'])}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_forscore_backup.py <backup_file.4sb>")
        sys.exit(1)
    
    filepath = Path(sys.argv[1])
    if not filepath.exists():
        print(f"Error: File not found: {filepath}")
        sys.exit(1)
    
    try:
        result = extract_4sb_file(filepath)
        
        if result and 'bookmarks' in result:
            analyze_bookmarks(result['bookmarks'])

        if result and 'summary' in result:
            print_summary(result['summary'])

        # Save extracted data as JSON for further analysis
        if result:
            output_file = filepath.with_suffix('.json')
            # Convert to JSON-serializable format
            json_data = convert_to_json_serializable(result)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False, default=str)
            print(f"\n✓ Extracted data saved to: {output_file}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def convert_to_json_serializable(obj):
    """Convert data to JSON-serializable format."""
    if isinstance(obj, dict):
        return {k: convert_to_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_to_json_serializable(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    elif isinstance(obj, bytes):
        return obj.hex() if len(obj) < 1000 else f"<{len(obj)} bytes>"
    else:
        return str(obj)


if __name__ == '__main__':
    main()

