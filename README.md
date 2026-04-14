# forScore Backup Cleanup Tools

Tools for cleaning bloated forScore backup/archive files (`.4sb`) and migrating forScore data to MobileSheets.

---

> **WARNING — Early Version / Use at Your Own Risk**
>
> This is an early version. Not all features have been tested. Documentation and code may contain errors or be misleading.
>
> **Before using any of these tools:**
> - Create a full backup: export an `Archive.4sb` from forScore and store it in a safe place outside the app (e.g. Files app, local Mac folder)
> - These tools come **without any warranty** and **without support**
> - Use entirely **at your own risk**

---

## What These Tools Do

**`clean_forscore_bookmarks.py`** — removes duplicate bookmarks from a forScore `.4sb` file (Backup or Archive) and produces a cleaned copy ready to restore. Unlike v1, **draw annotations (handwritten ink strokes stored as PNG) are fully preserved** in the output file.

**`forScore2MS.py`** — imports forScore data (songs, bookmarks, setlists, audio links) directly from a `.4sb` file into a MobileSheets SQLite database. No JSON intermediate step required.

**`extract_binaries_forscore_backup.py`** — extracts all embedded files (PDFs, audio, PNG drawings, etc.) from a forScore Archive (`.4sb` V03) into a local directory.

## Background

The forScore iPad app (sheet music reader) maintains all metadata — bookmarks, setlists, annotations — within the app. When iCloud Sync is active, sync interruptions cause bookmarks to accumulate massive numbers of duplicates (up to 93% of all bookmarks can be duplicates in affected libraries). There are no built-in tools to remove them.

Creating backups within forScore puts all data in a single `.4sb` file:

| Type | Filename prefix | Version | Contents |
|------|----------------|---------|----------|
| Backup | `Backup*.4sb` | `4SBV02` | Metadata only (bookmarks, setlists, annotations) |
| Archive | `Archiv*.4sb` | `4SBV03` | Metadata + all PDFs, audio files, PNG draw annotations |

Both use the same internal structure: a fixed 74-byte ASCII header, followed by a gzip-compressed Apple Binary Property List (bplist) containing all metadata, followed by a sequence of gzip-compressed file records (V03 only).

### What is a Bookmark?

In forScore, a **bookmark** is the primary navigation unit: it defines a piece of music as a page range within a PDF file. A single large PDF (e.g. a Real Book) can contain hundreds of bookmarks, each representing one song.

Duplicates arise when the same bookmark (same PDF + title + first page) gets multiple entries with different `Identifier` UUIDs due to iCloud sync conflicts.

### Draw Annotations

Draw annotations (handwritten ink) are stored as PNG files embedded in the `.4sb` Archive, one per annotated page. The filename encodes the PDF name and page number (`<pdfname>|<page>.png`). Because deduplication only modifies bookmark *entries* in the metadata plist — never the PDF filenames — the PNG-to-page associations remain valid after cleaning.

**`clean_forscore_bookmarks.py` copies all embedded records byte-for-byte** into the output file, so draw annotations are preserved.

## Tools

### `clean_forscore_bookmarks.py`

Removes duplicate bookmarks from a `.4sb` Backup or Archive file. All embedded records (PDFs, audio, PNG draw annotations) are copied unchanged into the output.

```bash
# Analyze only — no files written
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb" --dry-run

# Create cleaned file (recommended starting point)
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb"

# Also merge metadata from duplicates into the kept entry
python3 clean_forscore_bookmarks.py "Backup 2026-01-03.4sb" --merge-meta

# Works equally on Archive files (V03, preserves PDFs + audio + draw annotations)
python3 clean_forscore_bookmarks.py "Archiv 2026-04-12 16-00-52.4sb"
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output` | Output directory (default: `<input-stem>/` next to input) |
| `--dry-run` | Analyze only, write no files |
| `-m, --merge-meta` | Merge metadata from all duplicates into the first occurrence |
| `--no-dedup` | Skip deduplication (useful combined with other flags) |
| `--last-page-fix` | Fix bookmarks where Last Page = 0 |
| `--page2item` | Convert Page Bookmarks to Item Bookmarks |

**Output** (in `<input-stem>/`):

| File | Description |
|------|-------------|
| `<name>-cleaned.4sb` | Ready-to-restore backup/archive |
| `<name>-original.json` | Extracted plist as JSON (for inspection) |
| `<name>-original.plist` | Extracted binary plist |
| `<name>-cleaned.json` | Cleaned plist as JSON |
| `<name>-cleaned.plist` | Cleaned binary plist |

**What is preserved after cleaning:**
- All bookmarks (deduplicated)
- All setlists and their contents
- Text annotations, buttons, links
- Score metadata (title, composer, genre, key, BPM, …)
- Draw annotations / ink strokes (PNG, V03 Archive only)

**What is modified:**
- Duplicate bookmark entries are removed (first occurrence kept; use `--merge-meta` to consolidate metadata)

### `forScore2MS.py`

Imports forScore data directly from a `.4sb` file into a fresh MobileSheets SQLite database.

```bash
# Import to MobileSheets database
python3 forScore2MS.py "Archiv 2026-04-12 16-00-52.4sb"

# Dry run — parse and report, write no database
python3 forScore2MS.py "Archiv 2026-04-12 16-00-52.4sb" --dry-run

# Custom output directory
python3 forScore2MS.py "Archiv 2026-04-12 16-00-52.4sb" -o /tmp/ms-import
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output` | Output directory (default: `<input-stem>-2MS/` next to input) |
| `--dry-run` | Parse and report without writing any database |
| `-v, --verbose` | Print each inserted song title |

**What is imported (Stage 1):**
- Songs — every PDF in the library with full metadata
- Bookmarks — each bookmark becomes a virtual song (page-range slice of a PDF)
- Setlists — all setlists with song membership and display order
- Audio links — linked MP3/M4A tracks

**Output:** `<input-stem>-2MS/MobileSheets.db` — a fresh MobileSheets database importable by the app.

### `extract_binaries_forscore_backup.py`

Extracts all embedded files (PDFs, audio, PNG drawings, etc.) from a forScore Archive (V03) into a local directory.

```bash
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb"

# Custom output directory
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb" -o /tmp/out

# Verbose: print each filename as it is extracted
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb" -v
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output-dir` | Base output directory (default: `<stem>/` next to input) |
| `-v, --verbose` | Print each extracted filename |

Output is written to `<input-stem>/files/`. Only accepts Archive files (V03 / `Archiv*.4sb`).

### Shell helpers

| Script | Purpose |
|--------|---------|
| `cleanup_meta.sh` | Remove duplicate rows from a forScore CSV export |
| `cleanup_scores.sh` | Deduplicate and organize PDFs and audio files in a directory |
| `copy_pdfs.sh` | Copy a list of PDF files from one directory to another |
| `example_workflow.sh` | Example end-to-end workflow |

## Restore Workflow

After cleaning a Backup or Archive:

1. On iPad: open forScore → **Settings → Backup & Restore**
2. Import the cleaned `.4sb` file via Files / AirDrop
3. Restore from that backup

## Requirements

- Python 3.7+
- No external dependencies (stdlib only: `plistlib`, `gzip`, `zlib`, `json`, `sqlite3`)
