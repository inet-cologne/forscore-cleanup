# forScore Bookmark Duplicate Cleanup

## Problem

The forScore app creates massive amounts of bookmark duplicates due to iCloud sync interruptions. There are no built-in tools to remove these duplicates.

## Solution

This project contains tools for analyzing and cleaning forScore backup files (.4sb).

## File Format

The .4sb files have the following structure:
- **Header**: `<--4SBV02-->` + metadata (74 bytes)
- **Gzip-compressed data**: Binary Property List (bplist) format

## Tools

### 1. `analyze_forscore_backup.py`

Analyzes a .4sb file and extracts the data.

**Usage:**
```bash
python3 analyze_forscore_backup.py "Backup 2026-01-03 14-09-11.4sb"
```

**Output:**
- Extracts data from the .4sb file
- Creates a JSON file with the extracted data
- Shows statistics about bookmarks

### 2. `clean_forscore_bookmarks.py`

Removes duplicates from a .4sb file and creates a cleaned version.

**Usage:**
```bash
# Dry-run (analyze only, don't create file)
python3 clean_forscore_bookmarks.py "Backup 2026-01-03 14-09-11.4sb" --dry-run

# Create cleaned backup file
python3 clean_forscore_bookmarks.py "Backup 2026-01-03 14-09-11.4sb" -o "Backup-cleaned.4sb"

# With duplicate information
python3 clean_forscore_bookmarks.py "Backup 2026-01-03 14-09-11.4sb" --save-duplicates duplicates.json

# Merge metadata from duplicates
python3 clean_forscore_bookmarks.py "Backup 2026-01-03 14-09-11.4sb" -m
```

**Options:**
- `-o, --output`: Output file (default: input file with `-cleaned` suffix)
- `--dry-run`: Analyze only, don't create file
- `--save-duplicates`: Save duplicate information to JSON file
- `--no-dedup`: Skip deduplication (only for use with other options)
- `-m, --merge-meta`: Merge metadata from duplicate bookmarks (keep first occurrence and fill missing fields from others)
- `--last-page-fix`: Fix "Last Page = 0" → set to "First Page"
- `--page2item`: Convert Page Bookmarks to Item Bookmarks (sets Last Page and copies score metadata)

**Important Note on Drawings/Annotations:**
Drawings and handwritten annotations (Layer 1) CANNOT be preserved when metadata is modified. The drawing data contains references to byte positions in the metadata that become invalid with any modification (deduplication, page2item conversion, etc.). **Text annotations, buttons, and links are preserved.**

## Duplicate Detection

Duplicates are identified by:
- **FilePath** (PDF filename)
- **First Page** (page number)
- **Title** (bookmark title)

Bookmarks with the same FilePath, First Page, and Title are considered duplicates, even if they have different `Identifier` (UUIDs).

## Example Results

When analyzing the file `Backup 2026-01-03 14-09-11.4sb`:
- **Total bookmarks**: 26,169
- **Unique bookmarks**: 4,135
- **Removed duplicates**: 22,034 (84%)

## Restoring in forScore

1. Create a backup of your current forScore data
2. Run the cleanup script
3. Import the cleaned .4sb file into forScore using the backup restore function

**IMPORTANT**: Test the cleaned backup file in a test environment first before using it in your main library!

## Technical Details

### Bookmark Structure

A bookmark in the forScore data structure contains:
- `FilePath`: Name of the PDF file
- `First Page`: First page of the bookmark
- `Last Page`: Last page of the bookmark
- `Title`: Title of the bookmark
- `Composer`: Composer
- `Genre`: Genre
- `Key`: Key signature
- `Keyword`: Keywords
- `Identifier`: Unique UUID (may differ for duplicates)
- `kRecoverableDestination`: Flag for recoverable destination

### File Format Details

The .4sb file uses:
- **Header**: ASCII text with version information
- **Compression**: Gzip (Deflate algorithm)
- **Data format**: Binary Property List (Apple plist format)

## Known Limitations

1. The script keeps the first occurrence of a duplicate. If different duplicates have different metadata, the metadata of the first occurrence is retained.

2. The cleaned file should be tested before using it in forScore.

3. The script only works with "Backup" files (metadata only), not "Archive" files (with PDFs).

## Troubleshooting

### "Gzip magic number not found"
- The file may be corrupted or not a valid .4sb format

### "Unsupported format"
- The file does not contain a Binary Property List
- Possibly an older or newer version of the format

### Import fails in forScore
- Make sure the original backup file works
- Check if the cleaned file was created correctly
- Contact forScore support if you encounter problems

