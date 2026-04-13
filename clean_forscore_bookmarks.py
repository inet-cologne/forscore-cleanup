#!/usr/bin/env python3
"""
Clean duplicate bookmarks from forScore .4sb backup and archive files.

This script:
1. Extracts data from a .4sb backup/archive file
2. Identifies duplicate bookmarks
3. Removes duplicates
4. Creates a cleaned .4sb backup/archive file
5. For archive files: Extracts embedded files (PDFs, MP3s, etc.) and re-embeds them
"""

import struct
import zlib
import plistlib
import json
import sys
import os
import shutil
from pathlib import Path
from collections import defaultdict
import argparse
import gzip
import io
import re


def detect_file_type(filepath):
    """Detect if file is Backup or Archive based on filename and header."""
    filename = Path(filepath).name
    is_archive = filename.startswith('Archiv')
    is_backup = filename.startswith('Backup')
    
    # Also check header version
    with open(filepath, 'rb') as f:
        header = f.read(100)
    
    header_str = header.decode('utf-8', errors='ignore')
    is_v03 = '<--4SBV03-->' in header_str
    is_v02 = '<--4SBV02-->' in header_str
    
    # V03 is typically Archive, V02 is typically Backup
    if is_v03 and not is_backup:
        is_archive = True
    elif is_v02 and not is_archive:
        is_backup = True
    
    return {
        'is_archive': is_archive,
        'is_backup': is_backup,
        'version': 'V03' if is_v03 else 'V02' if is_v02 else 'Unknown'
    }


def calculate_gzip_header_size(data, offset):
    """Calculate the size of a gzip header starting at offset."""
    if data[offset:offset+2] != b'\x1f\x8b':
        return None
    
    method = data[offset+2]
    flags = data[offset+3]
    
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
    
    return header_size


def find_gzip_block_end(filepath, gzip_start, max_search=100*1024*1024):
    """Find the end of a gzip block by reading and decompressing it."""
    header_size = None
    with open(filepath, 'rb') as f:
        f.seek(gzip_start)
        header_data = f.read(100)
        header_size = calculate_gzip_header_size(header_data, 0)
        if header_size is None:
            return None
    
    compressed_start = gzip_start + header_size
    
    # Use gzip module to read the block - it will stop at the end
    with open(filepath, 'rb') as f:
        f.seek(gzip_start)
        try:
            with gzip.GzipFile(fileobj=f, mode='rb') as gz:
                # Read all data - this will position us at the end of the gzip block
                gz.read()
                # Get current position
                gzip_end = f.tell()
                return gzip_end
        except Exception:
            # Fallback: look for next gzip magic
            f.seek(compressed_start + 1000)
            chunk = f.read(min(max_search, 10*1024*1024))
            next_gzip = chunk.find(b'\x1f\x8b\x08')
            if next_gzip != -1:
                return compressed_start + 1000 + next_gzip
            return None


def extract_4sb_file(filepath, extract_artifacts=False, artifacts_dir=None, return_artifacts_data=False):
    """Extract and parse a .4sb backup/archive file."""
    print(f"Extracting: {filepath}")
    
    file_type = detect_file_type(filepath)
    print(f"File type: {'Archive' if file_type['is_archive'] else 'Backup'} (Version: {file_type['version']})")
    
    file_size = os.path.getsize(filepath)
    print(f"File size: {file_size:,} bytes ({file_size / (1024**3):.2f} GB)")
    
    # Read the 74-byte ASCII header
    with open(filepath, 'rb') as f:
        header_bytes = f.read(74)
    
    # Parse header to get metadata size
    # Format: <--4SBV02-->              30         1367604Backup...
    # The metadata size is an ASCII number before "Backup" or "Archiv"
    import re
    header_str = header_bytes.decode('ascii')
    match = re.search(r'(\d{5,10})(Backup|Archiv)', header_str)
    if not match:
        raise ValueError("Could not parse metadata size from header")
    
    metadata_size_from_header = int(match.group(1))
    print(f"Header indicates metadata size: {metadata_size_from_header:,} bytes")
    
    # Calculate where metadata ends and additional data begins
    # Header is 74 bytes, then comes metadata
    metadata_start = 74
    metadata_end = metadata_start + metadata_size_from_header
    
    # For large files, we need to read in chunks
    # But for metadata, we can read a reasonable chunk first
    with open(filepath, 'rb') as f:
        # Read first 100MB to get metadata
        initial_data = f.read(100 * 1024 * 1024)
    
    # Find first gzip magic number (metadata)
    offset = initial_data.find(b'\x1f\x8b\x08')
    if offset == -1:
        raise ValueError("Gzip magic number not found")
    
    if offset != 74:
        print(f"Warning: Gzip starts at {offset}, expected 74")
    
    header = initial_data[:offset]
    
    # The FIRST gzip block contains the actual metadata (plist)
    # Find its extent by decompressing
    # But for reading additional data, use metadata_end from header!
    with open(filepath, 'rb') as f:
        f.seek(offset)
        # Read a reasonable chunk to get the first gzip block
        first_gzip_data = f.read(min(metadata_size_from_header, 50 * 1024 * 1024))
    
    # Decompress the first gzip block to get the plist
    # Try to decompress just the first stream
    try:
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        decompressed = decompressor.decompress(first_gzip_data[10:])  # Skip gzip header (10 bytes)
        # Note: decompressor.unused_data contains any data after the first stream
    except Exception as e:
        raise ValueError(f"Could not decompress metadata: {e}")
    
    # Parse plist
    if decompressed[:8] != b'bplist00':
        raise ValueError("Unsupported format - not a binary plist")
    
    plist = plistlib.loads(decompressed)
    
    result = {
        'header': header,
        'gzip_header': b'',  # Not needed separately anymore
        'data': plist,
        'file_type': file_type,
        'metadata_start': metadata_start,
        'metadata_end': metadata_end,
        'filepath': filepath,
        'file_size': file_size
    }
    
    # Extract artifacts if requested and this is an archive
    artifacts_data = None
    if file_type['is_archive']:
        if extract_artifacts and artifacts_dir:
            # Extract files to directory
            extract_artifacts_from_archive(filepath, metadata_gzip_end, artifacts_dir)
        
        # Always read artifacts data if we need to return it (for re-embedding)
        if return_artifacts_data:
            artifacts_data = read_artifacts_data(filepath, metadata_gzip_end)
    
    if return_artifacts_data and artifacts_data:
        result['artifacts_data'] = artifacts_data
    
    return result


def read_artifacts_data(filepath, metadata_end):
    """Read all data after metadata (artifacts) from archive file.
    
    Artifacts are stored as gzip blocks. We read them as-is to preserve the format.
    """
    file_size = os.path.getsize(filepath)
    artifacts_size = file_size - metadata_end
    
    print(f"Reading artifacts data: {artifacts_size:,} bytes ({artifacts_size / (1024**3):.2f} GB)")
    
    # Simply read all data after metadata_end - this preserves the gzip block structure
    with open(filepath, 'rb') as f:
        f.seek(metadata_end)
        artifacts_data = f.read()
    
    print(f"✓ Read {len(artifacts_data):,} bytes of artifacts data")
    return artifacts_data


def extract_artifacts_from_archive(filepath, metadata_end, artifacts_dir):
    """Extract embedded files from archive.
    
    Files in archive are stored in separate gzip blocks after the metadata block.
    Each gzip block contains one file (PDF, MP3, etc.).
    """
    print(f"\nExtracting artifacts from archive...")
    print(f"Metadata ends at offset: {metadata_end:,}")
    
    file_size = os.path.getsize(filepath)
    artifacts_size = file_size - metadata_end
    print(f"Artifacts size: {artifacts_size:,} bytes ({artifacts_size / (1024**3):.2f} GB)")
    
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    
    # File signatures to detect file type after decompression
    file_signatures = {
        b'%PDF': '.pdf',
        b'ID3': '.mp3',
        b'\xff\xfb': '.mp3',
        b'\x89PNG': '.png',
        b'GIF8': '.gif',
        b'\xff\xd8\xff': '.jpg',
        b'PK\x03\x04': '.zip',  # ZIP file
    }
    
    extracted_count = 0
    
    print("Finding all gzip blocks containing files...")
    
    # First, find all gzip block positions
    gzip_positions = []
    chunk_size = 100 * 1024 * 1024  # 100MB chunks
    current_pos = metadata_end
    
    print("  Scanning for gzip blocks...")
    with open(filepath, 'rb') as f:
        while current_pos < file_size:
            f.seek(current_pos)
            chunk = f.read(chunk_size)
            if not chunk:
                break
            
            # Find all gzip magic numbers in this chunk
            offset = 0
            while True:
                pos = chunk.find(b'\x1f\x8b\x08', offset)
                if pos == -1:
                    break
                gzip_abs_pos = current_pos + pos
                gzip_positions.append(gzip_abs_pos)
                offset = pos + 1
            
            current_pos += chunk_size - 1000  # Overlap to catch blocks at boundaries
    
    print(f"  Found {len(gzip_positions)} gzip blocks")
    
    # Now extract each gzip block
    print("  Extracting files from gzip blocks...")
    for i, gzip_pos in enumerate(gzip_positions):
        try:
            with open(filepath, 'rb') as gz_file:
                gz_file.seek(gzip_pos)
                with gzip.GzipFile(fileobj=gz_file, mode='rb') as gz:
                    decompressed = gz.read()
                    
                    # Determine file type
                    file_ext = None
                    for sig, ext in file_signatures.items():
                        if decompressed.startswith(sig):
                            file_ext = ext
                            break
                    
                    if file_ext is None:
                        file_ext = '.bin'  # Default extension
                    
                    # Save file
                    filename = f"artifact_{extracted_count:05d}{file_ext}"
                    filepath_out = artifacts_dir / filename
                    with open(filepath_out, 'wb') as out_f:
                        out_f.write(decompressed)
                    
                    extracted_count += 1
                    if extracted_count % 50 == 0:
                        print(f"    Extracted {extracted_count}/{len(gzip_positions)} files...")
        
        except Exception as e:
            # Skip invalid gzip blocks
            continue
    
    print(f"✓ Extracted {extracted_count} artifact files to {artifacts_dir}")
    return extracted_count


def read_file_chunk(filepath, start, size):
    """Read a chunk of a file."""
    with open(filepath, 'rb') as f:
        f.seek(start)
        return f.read(size)


def find_all_bookmarks(plist_data, path=""):
    """Recursively find all bookmark lists in the plist structure.

    Only keys ending in '|bookmarks' are treated as bookmark lists.
    Setlist keys (&SET;...) and other lists that happen to contain
    dicts with 'FilePath'/'Title' fields are explicitly excluded to
    avoid accidentally deduplicating setlist entries.
    """
    bookmark_lists = []
    
    if isinstance(plist_data, dict):
        for key, value in plist_data.items():
            current_path = f"{path}.{key}" if path else key
            # Only consider keys that are actual bookmark lists
            if key.endswith('|bookmarks') and isinstance(value, list):
                bookmark_lists.append({
                    'path': current_path,
                    'parent': plist_data,
                    'key': key,
                    'bookmarks': value
                })
            # Recurse into nested structures, but not into setlists or bookmark lists
            elif not key.startswith('&SET;') and not key.endswith('|bookmarks'):
                bookmark_lists.extend(find_all_bookmarks(value, current_path))
    elif isinstance(plist_data, list):
        for i, item in enumerate(plist_data):
            bookmark_lists.extend(find_all_bookmarks(item, f"{path}[{i}]"))
    
    return bookmark_lists


def create_bookmark_key(bookmark):
    """Create a unique key for a bookmark to identify duplicates."""
    file_path = bookmark.get('FilePath', '')
    first_page = bookmark.get('First Page', 0)
    title = bookmark.get('Title', '')
    return (file_path, first_page, title)


def remove_duplicate_bookmarks(bookmark_list, merge_meta=False):
    """Remove duplicate bookmarks from a list.

    By default, keeps the first occurrence of each logical bookmark.
    If merge_meta=True, metadata from later duplicates is merged into the
    first occurrence (only filling missing/empty fields).
    """
    seen = {}
    unique_bookmarks = []
    duplicates = []
    
    for bookmark in bookmark_list:
        key = create_bookmark_key(bookmark)
        
        if key in seen:
            original = seen[key]

            # Optionally merge metadata from duplicate into the original
            if merge_meta and isinstance(bookmark, dict) and isinstance(original, dict):
                for field, value in bookmark.items():
                    # Skip core identity fields
                    if field in {"FilePath", "First Page", "Last Page", "Title", "Identifier"}:
                        continue

                    # Consider value "missing" if None or empty string in original
                    if field not in original or original.get(field) in (None, ""):
                        if value not in (None, ""):
                            original[field] = value

            duplicates.append({
                'original': original,
                'duplicate': bookmark,
                'key': key
            })
        else:
            seen[key] = bookmark
            unique_bookmarks.append(bookmark)
    
    return unique_bookmarks, duplicates


def convert_page_to_item_bookmarks(plist_data):
    """Convert Page Bookmarks (Last Page = 0) to Item Bookmarks.
    
    Page Bookmarks are simple navigation markers with minimal metadata.
    Item Bookmarks are treated like virtual scores with full metadata.
    
    This function:
    1. Sets Last Page = First Page for single-page bookmarks
    2. Copies metadata from the score level (Genre, Composer, Keyword, etc.)
    """
    fixed_count = 0
    
    def process_dict(obj, score_metadata=None):
        nonlocal fixed_count
        
        if isinstance(obj, dict):
            # Check if this is a bookmarks list
            for key, value in list(obj.items()):
                if key.endswith('|bookmarks') and isinstance(value, list):
                    # Extract the FilePath from the key
                    filepath = key.replace('|bookmarks', '')
                    
                    # Get score-level metadata for this file
                    score_meta = {}
                    for meta_key in ['composer', 'genre', 'keywords', 'key', 'bpm', 'signature']:
                        meta_full_key = f"{filepath}|{meta_key}"
                        if meta_full_key in obj:
                            # Map to bookmark field names (capitalized)
                            bookmark_field = meta_key.capitalize() if meta_key != 'keywords' else 'Keyword'
                            bookmark_field = 'Key' if meta_key == 'key' else bookmark_field
                            bookmark_field = 'BPM' if meta_key == 'bpm' else bookmark_field
                            bookmark_field = 'Signature' if meta_key == 'signature' else bookmark_field
                            score_meta[bookmark_field] = obj[meta_full_key]
                    
                    # Process each bookmark
                    for bookmark in value:
                        if isinstance(bookmark, dict):
                            last_page = bookmark.get('Last Page', None)
                            first_page = bookmark.get('First Page', None)
                            
                            # Check if it's a Page Bookmark (Last Page = 0)
                            if last_page == 0 and first_page is not None:
                                # Convert to Item Bookmark
                                bookmark['Last Page'] = first_page
                                
                                # Copy score-level metadata to bookmark
                                for field, value in score_meta.items():
                                    if field not in bookmark:
                                        bookmark[field] = value
                                
                                fixed_count += 1
                
                # Recurse into nested structures
                elif isinstance(value, (dict, list)):
                    process_dict(value, score_metadata)
        
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    process_dict(item, score_metadata)
    
    process_dict(plist_data)
    return plist_data, fixed_count


def fix_last_page_zero(plist_data):
    """Fix bookmarks where Last Page = 0 by setting it to First Page value."""
    fixed_count = 0
    
    def fix_bookmark(obj):
        nonlocal fixed_count
        if isinstance(obj, dict):
            # Check if this is a bookmark entry
            if 'First Page' in obj and 'Last Page' in obj:
                if obj.get('Last Page') == 0:
                    first_page = obj.get('First Page', 0)
                    obj['Last Page'] = first_page
                    fixed_count += 1
            # Recursively process all values
            for value in obj.values():
                fix_bookmark(value)
        elif isinstance(obj, list):
            for item in obj:
                fix_bookmark(item)
    
    fix_bookmark(plist_data)
    return plist_data, fixed_count


def clean_bookmarks_in_plist(plist_data, merge_meta=False, no_dedup=False):
    """Clean all bookmark lists in the plist structure.

    If merge_meta=True, metadata from duplicate bookmarks is merged into
    the first occurrence before duplicates are removed.
    If no_dedup=True, skip deduplication entirely (keeps all bookmarks).
    """
    bookmark_lists = find_all_bookmarks(plist_data)
    
    print(f"\nFound {len(bookmark_lists)} bookmark lists to process")
    
    if no_dedup:
        print("  ⚠ Deduplication SKIPPED (--no-dedup flag set)")
        total_original = sum(len(bm_list['bookmarks']) for bm_list in bookmark_lists)
        return plist_data, []
    
    total_original = 0
    total_unique = 0
    total_duplicates = 0
    all_duplicates = []
    
    for bm_list in bookmark_lists:
        original_count = len(bm_list['bookmarks'])
        unique_bookmarks, duplicates = remove_duplicate_bookmarks(
            bm_list['bookmarks'],
            merge_meta=merge_meta
        )
        unique_count = len(unique_bookmarks)
        dup_count = original_count - unique_count
        
        total_original += original_count
        total_unique += unique_count
        total_duplicates += dup_count
        
        if dup_count > 0:
            print(f"  {bm_list['path']}: {original_count} -> {unique_count} (removed {dup_count} duplicates)")
            all_duplicates.extend(duplicates)
        
        bm_list['parent'][bm_list['key']] = unique_bookmarks
    
    print(f"\n=== Summary ===")
    print(f"Total bookmarks: {total_original}")
    print(f"Unique bookmarks: {total_unique}")
    print(f"Duplicates removed: {total_duplicates}")
    
    return plist_data, all_duplicates


def create_4sb_file(header, gzip_header, plist_data, output_path, is_archive=False, artifacts_data=None):
    """Create a new .4sb file from cleaned data."""
    file_type = "archive" if is_archive else "backup"
    print(f"\nCreating cleaned {file_type} file: {output_path}")
    
    # Serialize plist
    plist_bytes = plistlib.dumps(plist_data, fmt=plistlib.FMT_BINARY)
    
    # Create gzip compressed data - IMPORTANT: Only compress metadata, not additional data!
    gzip_buffer = io.BytesIO()
    gz = gzip.GzipFile(fileobj=gzip_buffer, mode='wb', compresslevel=9)
    gz.write(plist_bytes)
    gz.close()  # MUST close before getvalue() to ensure all data is flushed
    gzip_data = gzip_buffer.getvalue()
    
    # Update header to match new metadata size
    metadata_size = len(gzip_data)
    
    try:
        header_str = header.decode('ascii')
        
        # Find the metadata size number in the header
        # Format: <--4SBV02-->              30         1367604Backup...
        # The number before "Backup" or "Archiv" is the metadata size
        import re
        match = re.search(r'(\d{5,10})(Backup|Archiv)', header_str)
        
        if match:
            old_size_str = match.group(1)
            old_size = int(old_size_str)
            new_size_str = str(metadata_size)
            
            # Replace with right-aligned, space-padded version to maintain exact length
            padding_needed = len(old_size_str) - len(new_size_str)
            if padding_needed >= 0:
                new_size_padded = ' ' * padding_needed + new_size_str
                header_str = header_str.replace(old_size_str, new_size_padded, 1)
                header = header_str.encode('ascii')
                print(f"✓ Updated header metadata size: {old_size:,} → {metadata_size:,} bytes")
            else:
                print(f"⚠ Warning: New metadata size ({metadata_size}) is too large for header field!")
                print(f"  Keeping original header - file may not import correctly")
        else:
            print(f"⚠ Warning: Could not find metadata size in header")
            print(f"  Keeping original header - file may not import correctly")
            
    except Exception as e:
        print(f"⚠ Warning: Could not parse header: {e}")
        print(f"  Keeping original header")
    
    # Combine header + gzip data
    # Note: We do NOT append additional data (drawings, annotations) as they
    # contain references to byte offsets in the metadata which would be invalid
    # after any modifications (dedup, page2item, etc.)
    output_data = header + gzip_data
    
    # Write output file
    with open(output_path, 'wb') as f:
        f.write(output_data)
    
    print(f"✓ Created {output_path} ({len(output_data):,} bytes)")


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


def main():
    parser = argparse.ArgumentParser(
        description='Clean duplicate bookmarks from forScore .4sb backup and archive files'
    )
    parser.add_argument(
        'input_file',
        help='Input .4sb backup or archive file'
    )
    parser.add_argument(
        '-o', '--output',
        help='Output directory (default: creates subdirectory named after input file)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Analyze duplicates without creating output files'
    )
    parser.add_argument(
        '-m', '--merge-meta',
        action='store_true',
        help='Merge metadata from duplicate bookmarks (keep first occurrence and fill missing fields from others)'
    )
    parser.add_argument(
        '--no-extract-artifacts',
        action='store_true',
        help='Skip artifact extraction for archive files (artifacts will still be embedded in cleaned file)'
    )
    parser.add_argument(
        '--no-dedup',
        action='store_true',
        help='Skip bookmark deduplication (only fix Last Page=0 and preserve additional data like drawings)'
    )
    parser.add_argument(
        '--last-page-fix',
        action='store_true',
        help='Fix bookmarks where Last Page = 0 (set to First Page value)'
    )
    parser.add_argument(
        '--page2item',
        action='store_true',
        help='Convert Page Bookmarks to Item Bookmarks (sets Last Page and copies score metadata)'
    )
    
    args = parser.parse_args()
    
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        sys.exit(1)
    
    # Determine output directory
    if args.output:
        output_dir = Path(args.output)
    else:
        # Create subdirectory named after input file (without extension)
        output_dir = input_path.parent / input_path.stem
    
    # Create output directory structure
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = output_dir / 'artefacts'
    
    # Extract data
    try:
        extract_artifacts = not args.no_extract_artifacts and not args.dry_run
        return_artifacts = True  # Always return artifacts data for archive files to re-embed them
        
        extracted = extract_4sb_file(
            input_path,
            extract_artifacts=extract_artifacts,
            artifacts_dir=artifacts_dir if not args.dry_run else None,
            return_artifacts_data=return_artifacts
        )
    except Exception as e:
        print(f"Error extracting file: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Save original JSON (before cleaning)
    if not args.dry_run:
        original_json_path = output_dir / f"{input_path.stem}-original.json"
        json_data = convert_to_json_serializable({
            'header': extracted['header'].hex() if isinstance(extracted['header'], bytes) else str(extracted['header']),
            'data': extracted['data'],
            'file_type': extracted['file_type']
        })
        with open(original_json_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n✓ Original data saved to: {original_json_path}")
        # Also save original plist (binary) for reference
        original_plist_path = output_dir / f"{input_path.stem}-original.plist"
        original_plist_bytes = plistlib.dumps(extracted['data'], fmt=plistlib.FMT_BINARY)
        with open(original_plist_path, 'wb') as f:
            f.write(original_plist_bytes)
        print(f"✓ Original plist saved to: {original_plist_path}")
    
    # Clean bookmarks
    if args.no_dedup:
        print("\n⚠ Skipping bookmark deduplication (--no-dedup flag set)")
    else:
        print("\nCleaning bookmarks...")
    cleaned_data, duplicates = clean_bookmarks_in_plist(
        extracted['data'],
        merge_meta=args.merge_meta,
        no_dedup=args.no_dedup
    )
    
    # Convert Page Bookmarks to Item Bookmarks (only if --page2item is set)
    if args.page2item:
        print("\nConverting Page Bookmarks to Item Bookmarks...")
        cleaned_data, converted_count = convert_page_to_item_bookmarks(cleaned_data)
        if converted_count > 0:
            print(f"✓ Converted {converted_count} Page Bookmarks to Item Bookmarks")
            print(f"  (Set Last Page and copied score metadata: Genre, Composer, Keyword, etc.)")
        else:
            print("✓ No Page Bookmarks found to convert")
    elif args.last_page_fix:
        # Legacy --last-page-fix (only sets Last Page, no metadata)
        print("\nFixing bookmarks with Last Page = 0...")
        cleaned_data, fixed_count = fix_last_page_zero(cleaned_data)
        if fixed_count > 0:
            print(f"✓ Fixed {fixed_count} bookmarks where Last Page = 0 (set to First Page value)")
            print(f"  Note: Use --page2item to also copy score metadata")
        else:
            print("✓ No bookmarks with Last Page = 0 found")
    else:
        print("\nSkipping Page Bookmark fixes (use --page2item or --last-page-fix to enable)")
    
    # Save cleaned JSON
    if not args.dry_run:
        cleaned_json_path = output_dir / f"{input_path.stem}-cleaned.json"
        cleaned_json_data = convert_to_json_serializable({
            'header': extracted['header'].hex() if isinstance(extracted['header'], bytes) else str(extracted['header']),
            'data': cleaned_data,
            'file_type': extracted['file_type']
        })
        with open(cleaned_json_path, 'w', encoding='utf-8') as f:
            json.dump(cleaned_json_data, f, indent=2, ensure_ascii=False, default=str)
        print(f"✓ Cleaned data saved to: {cleaned_json_path}")
        # Also save cleaned plist (binary)
        cleaned_plist_path = output_dir / f"{input_path.stem}-cleaned.plist"
        cleaned_plist_bytes = plistlib.dumps(cleaned_data, fmt=plistlib.FMT_BINARY)
        with open(cleaned_plist_path, 'wb') as f:
            f.write(cleaned_plist_bytes)
        print(f"✓ Cleaned plist saved to: {cleaned_plist_path}")
    
    if args.dry_run:
        print("\n=== Dry run complete ===")
        print("Use without --dry-run to create cleaned files")
    else:
        # Create cleaned .4sb file
        is_archive = extracted['file_type']['is_archive']
        output_file = output_dir / f"{input_path.stem}-cleaned.4sb"
        
        # Check if there is additional data after metadata (annotations, drawings, etc.)
        # This can exist in both Backup (V02) and Archive (V03) files!
        artifacts_data = None
        file_size = os.path.getsize(extracted['filepath'])
        has_additional_data = file_size > extracted['metadata_end']
        
        if has_additional_data:
            additional_size = file_size - extracted['metadata_end']
            print(f"\n⚠️  File contains {additional_size:,} bytes of additional data (drawings, annotations)")
            print(f"   These will NOT be preserved - drawings require unchanged metadata structure")
            print(f"   Any modifications (dedup, page2item, etc.) will break drawing references")
        
        try:
            create_4sb_file(
                extracted['header'],
                extracted['gzip_header'],
                cleaned_data,
                output_file,
                is_archive=is_archive,
                artifacts_data=artifacts_data
            )
            print(f"\n✓ Cleaned {extracted['file_type']['version']} file created successfully!")
            print(f"  Output directory: {output_dir}")
            if has_additional_data:
                print(f"  ⚠️  Note: Drawings/annotations were NOT preserved")
                print(f"     They require unchanged metadata structure")
            print(f"  You can now restore this backup in forScore")
        except Exception as e:
            print(f"\nError creating output file: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


if __name__ == '__main__':
    main()
