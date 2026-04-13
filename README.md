# forScore Backup Cleanup Tools

Tools for analyzing and cleaning corrupt or bloated forScore backup files (`.4sb`).
They do not modify any app data but produce analysis reports and optionally remove duplicates by creating a cleaned copy of the .4sb file which can be used for restoring.

## Quickstart

With the following command you can analyze an existing forScore backup file and check for duplicates without any risk:

```bash
python3 analyze_forscore_backup.py "Backup 2026-01-03.4sb"
```

If there are many duplicates like in the sample below this slows down the app significantly. In this case you may use the other scripts to rescue at least your meta data, bookmarks, setlists and text annotations.

Read on the following for more information.

The summary at the end of the analysis report looks something like this:

```bash
...
...
===========================================
SUMMARY
===========================================
  Songs (PDFs):             1,280
  Setlists:                   110
  Bookmarks (total):       24,241
  Bookmarks (unique):       1,745
  Bookmarks (duplicate):   22,496  (92.8%)
===========================================
```

## Background

The forScore iPad app (sheet music reader) maintains all metadata — bookmarks, setlists, annotations within the app. If iCloud Sync is activated, due to iCloud sync interruptions, bookmarks accumulate massive amounts of duplicates (up to 84% of all bookmarks may be duplicates). There are no built-in tools to remove them.

Creating backups within forScore puts all data in a single `.4sb` file (Archive.4sb and Backup.4sb).
Archive.4sb files contain all data, that is metadata, including bookmarks, setlists, annotations, and PDFs and audio files and therefor are much bigger files.
Backup.4sb files only contain all the metadata (bookmarks, setlists, annotations) but does not contain any PDFs or audio files and are therefor very small.

To Restore all your data into a device you can restore just an Archive.4sb file or alternatively import all binary files (PDF and Audio files) manually into a device and after that restore the Backup.4sb file.

### File Formats

forScore produces two types of `.4sb` files:

| Type | Version | Contents |
|------|---------|----------|
| Backup | `4SBV02` | Metadata only (bookmarks, setlists, annotations) |
| Archive | `4SBV03` | Metadata + all PDFs and audio files embedded |

Both use the same internal structure: a 74-byte ASCII header followed by a gzip-compressed Apple Binary Property List (bplist).

### What is a Bookmark?

In forScore, a **bookmark** is the primary unit: it defines a piece of music as a page range within a PDF file. A single PDF (e.g., a Real Book) can contain hundreds of bookmarks, each representing one song.

Bookmarks are stored in the plist under keys like `<PDF-filename>|bookmarks` as a list of dicts:

```
{
  "FilePath": "Real Book (Bb) Vol.1.pdf",
  "Title": "Autumn Leaves",
  "First Page": 42,
  "Last Page": 43,
  "Identifier": "UUID-...",
  "Composer": "Joseph Kosma",
  "Genre": "Jazz Standard",
  ...
}
```

Duplicates arise when the same bookmark (same FilePath + Title + First Page) gets multiple entries with different `Identifier` UUIDs.

### Setlists and Bookmarks

Setlist entries are stored under `&SET;<name>` keys. Each entry references a score by `FilePath`, `Title`, and `Identifier`. Importantly:

- Setlist `Identifier` values are **independent** from bookmark `Identifier` values
- Setlists reference the score object (the whole-PDF or bookmark-as-score concept), not the raw bookmark entry
- **Deduplicating bookmarks does not break setlist references**

However: if the same song appears **twice in the same setlist** (a setlist-level duplicate), cleaning must preserve both entries if they are intentionally distinct.

## Tools

### `analyze_forscore_backup.py`

Extracts and analyzes a `.4sb` file without modifying it.

```bash
python3 analyze_forscore_backup.py "Backup 2026-01-03.4sb"
```

Outputs a JSON file with the extracted plist data and bookmark statistics.

### `extract_binaries_forscore_backup.py`

Extracts all embedded files (PDFs, audio, PNG drawings, etc.) from a forScore
Archive file (`.4sb` V03) into a local directory. Useful for inspecting archive
contents or recovering individual files without doing a full restore.

```bash
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb"
```

Output is written to `Archiv 2026-04-12 16-00-52/files/` next to the input file.

```bash
# Custom output base directory
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb" -o /tmp/out

# Show each filename as it is extracted
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb" -v
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output-dir` | Base output directory (default: `<stem>/` next to input file) |
| `-v, --verbose` | Print each extracted filename |

**Output directory layout:**

```
Archiv 2026-04-12 16-00-52/
└── files/
    ├── Real Book (Bb) Vol.1.pdf
    ├── My Song.mp3
    ├── artifact_00001.png   ← fallback name when gzip FNAME is absent
    └── ...
```

Filenames are taken from the `FNAME` field in each gzip block's header when
available. If absent, the file type is detected from magic bytes and a
zero-padded counter is used (`artifact_00001.pdf`, etc.).

Only accepts files whose name starts with `Archiv` and whose header contains
the V03 marker `<--4SBV03-->`. Exits with an error otherwise.

The summaary output of the script looks like this:

```bash
python3 extract_binaries_forscore_backup.py Archiv\ 2026-04-12\ 16-00-52.4sb
Started:  18:53:58
Input:    Archiv 2026-04-12 16-00-52.4sb (28.1 GB)
Output:   /Users/cwa/developer/opencode/forscore/Archiv 2026-04-12 16-00-52/files

Metadata: 3.1 MB  |  Payload: 28.1 GB

Phase 1 — Counting records...
Scan complete.  5786 records found  (0s, 189.9 GB/s)

Phase 2 — Extracting 5786 records...
[================>                  ] 2702/5786 (46%)  270.3 MB/s  ETA 46s  ok=2702 skip=0
  [18:54:59  1m 00s elapsed]  2702/5786 processed  2702 extracted  0 skipped  15.8 GB read
[===================================] 5786/5786 (100%)  done in 1m 46s  ok=5786 skip=0

====================================================
SUMMARY
====================================================
  Started:           18:53:58
  Finished:          18:55:45
  Duration:          1m 46s

  Blocks scanned:      5786
  Files extracted:     5786
  Skipped (corrupt):      0

  By file type:
    .mp3            3582
    .pdf            1281
    .png             898
    .m4a              14
    .csv               7
    .wav               2
    .mid               2
====================================================
  Output: /Users/cwa/developer/opencode/forscore/Archiv 2026-04-12 16-00-52/files
====================================================
```

### `clean_forscore_bookmarks.py`

The main script. Removes duplicate bookmarks from a `.4sb` file and creates a cleaned version.

WARNING:
drawing annotations from the cleaned file will be lost!
Therefor restoring a cleaned .4sb file will result in no more duplicates but drawing annotation will be lost!
Do not import a cleaned version of a .4sb file without creating a full backup of you data (Archive.4sb) and store this file in a safe place outside the app (export/share and save to device or iCloud-Folder).

Use at your own risk!!!

```bash
# Analyze only (dry run)
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb" --dry-run

# Create cleaned backup (recommended)
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb"

# Merge metadata from duplicates before removing them
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb" --merge-meta

# Convert Page Bookmarks to Item Bookmarks (sets Last Page, copies score metadata)
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb" --page2item
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output` | Output directory (default: subdirectory named after input file) |
| `--dry-run` | Analyze only, create no files |
| `-m, --merge-meta` | Merge metadata from duplicates into first occurrence |
| `--no-dedup` | Skip deduplication (useful with other flags only) |
| `--last-page-fix` | Fix bookmarks where Last Page = 0 |
| `--page2item` | Convert Page Bookmarks to Item Bookmarks |

**Output:**

The script creates a subdirectory (named after the input file) containing:

- `<name>-original.json` — extracted plist as JSON (for inspection)
- `<name>-original.plist` — extracted binary plist
- `<name>-cleaned.json` — cleaned plist as JSON
- `<name>-cleaned.plist` — cleaned binary plist
- `<name>-cleaned.4sb` — ready-to-restore backup file

### `cleanup_meta.sh`

Removes duplicate rows from a forScore CSV export file (exported via the forScore app's CSV export feature, separate from `.4sb` files).

```bash
./cleanup_meta.sh -i csv/2025-12-28_forScore-Export.csv
```

### `cleanup_scores.sh`

Organizes PDF and audio files in an `original/` directory: removes file duplicates (by name pattern and file size), cleans numeric prefixes from audio filenames, and moves non-score files aside.

```bash
./cleanup_scores.sh -a -d      # dry-run: show audio file duplicates
./cleanup_scores.sh -a -d -x   # execute: move audio duplicates
./cleanup_scores.sh -p -d -x   # execute: move PDF duplicates
```

### `copy_pdfs.sh`

Copies PDF files listed in a text file from a source directory to a destination directory.

```bash
./copy_pdfs.sh pdf_list.txt /path/to/source /path/to/destination
```

## Restore Workflow

After cleaning a backup:

1. On iPad: open forScore → go to **Settings → Backup & Restore**
2. Import the cleaned `.4sb` file via Files / AirDrop
3. Restore from that backup

**What is preserved:**
- All bookmarks (deduplicated)
- All setlists and their contents
- Text annotations
- Buttons and links
- Score metadata (title, composer, genre, key, BPM, ...)

**What is lost:**
- Drawn/handwritten annotations (stored as binary data with offsets into the original metadata structure; any modification invalidates those offsets)

To recover drawings: the original `.4sb` Archive file (V03) embeds them as PNG data in gzip blocks after the metadata section. Extracting and re-associating them is a potential future enhancement.

## Known Limitations

1. **Drawings are lost** after any metadata modification (dedup, page2item, etc.) because drawing data contains byte-offset references into the metadata structure.
2. The script keeps the **first occurrence** of each duplicate bookmark. Use `--merge-meta` to consolidate metadata from all duplicates before removal.
3. Only `.4sb` files with gzip + bplist format are supported (all current forScore versions use this).

## Known Bugs Fixed

### Setlist entries incorrectly treated as bookmarks (fixed)

The original `find_all_bookmarks()` function detected any list of dicts containing `FilePath` or `Title` fields as a bookmark list. Since setlist entries (`&SET;...` keys) also contain these fields, they were fed into the deduplication logic.

Result: if the same song appeared twice in one setlist (two entries with different `Identifier` UUIDs but same `FilePath` + `Title`), one entry was silently removed.

**Fix:** `find_all_bookmarks()` now exclusively identifies keys ending in `|bookmarks`, completely ignoring `&SET;` keys and all other non-bookmark lists.

## Open Questions / Future Work

- [ ] Extract and re-associate drawn annotations from Archive files (`.4sb` V03)
- [ ] Verify setlist completeness after cleaning (count items per setlist before/after)
- [ ] Handle edge case: bookmark `Identifier` collision across different PDFs
- [ ] Support for forScore Archive files (V03) in cleaning workflow

## Requirements

- Python 3.7+
- No external dependencies (uses only stdlib: `plistlib`, `gzip`, `zlib`, `json`)
