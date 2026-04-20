# forScore Backup Cleanup Tools

This is all about dealing with the forScore sheet reader app (iPadOS/iOS) issues relating to iCloud sync problems (producing thousands of bookmark duplicates under the hood) and options to clean up your forScore library and to extract, backup and/or transfer data to another sheet reader app like MobileSheets.

**The bad news:**
Until today: no fixes, no helpful support and no interest in the issues from the developer himself!

**The good news:**
I did reverse engineering research and developed a few helpful tools.
I have 3 ready scripts and a 4th in testing to help on getting rid of the duplicates and reusing data in forScore, and to extract/prepare and transform your data for raw backup purposes and for use in other apps like MobileSheets.
They all work with an exported `Archiv*.4sb` / `Archive*.4sb` file (or `Backup*.4sb` for metadata-only operations).

---

> **WARNING — USE AT YOUR OWN RISK!**
>
> The provided scripts here are in an early state and only basically tested for now!
>
> Please consider making backups of everything and be sure about what you are doing BEFORE using any of the information or scripts mentioned here, as I am not responsible for any damage or data loss that might occur. I mainly do not have the time to help you get out of problems if they occur.
>
> This is an early version. Not all features have been tested. Documentation and code may contain errors or be misleading.

---

## The Details

### 1. `analyze_forscore_backup.py`

Shows basic stats of your data, especially the duplicate status.

```bash
python3 analyze_forscore_backup.py "Backup 2026-01-03.4sb"
```

My stats before cleaning:

```
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

As you can see, the main problem in my library are the 22,500 duplicates!

---

### 2. `extract_binaries_forscore_backup.py`

As the name says: extracts all PDF, audio and other files — including the separate drawing annotation PNG files — from the `Archiv*.4sb` file and puts them in a separate folder. These can be used to transfer everything to another app like MobileSheets.

```bash
python3 extract_binaries_forscore_backup.py "Archiv 2026-04-12 16-00-52.4sb"
```

My summary output:

```
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
  Output: /Users/me/developer/forscore/Archiv 2026-04-12 16-00-52/files
====================================================
```

Interesting: about 900 drawing annotation files!

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output-dir` | Base output directory (default: `<stem>/` next to input) |
| `-v, --verbose` | Print each extracted filename |
| `--midi-doc` | Decode MIDI presets from archive metadata and write `midi-presets.md` |

`--midi-doc` reads the MIDI preset configuration stored in the archive metadata and writes a `midi-presets.md` file parallel to the `files/` directory (e.g. `<stem>/midi-presets.md`). It decodes both Program Change and raw hex CC commands into readable form and shows a preset/command count in the terminal summary.

---

### 3. `clean_forscore_bookmarks.py`

Drum roll: it cleans up the `Archiv*.4sb` file by removing all bookmark duplicates and produces a new `*-cleaned.4sb` file.
It preserves all data including linked audio and the drawing annotations (as far as I can see at the moment).

A first test run on my 30 GB Archive file shows the following result:

```
========================================================
DONE
========================================================
  Started:          12:37:02
  Finished:         12:37:15

  Input:            28.1 GB
  Output:           28.1 GB
  Size difference:  1.9 MB (metadata cleaned)

  Records copied:   5,786
  Dupes removed:    22,496

  Output file:      /Users/me/developer/forscore/Archiv 2026-04-12 16-00-52/Archiv 2026-04-12 16-00-52-cleaned.4sb
  Debug JSON:       /Users/me/developer/forscore/Archiv 2026-04-12 16-00-52/Archiv 2026-04-12 16-00-52-cleaned.json
========================================================
```

The most important result: I transferred the cleaned file to a different device, did a fresh forScore app download, imported the cleaned `Archiv*-cleaned.4sb` file, restored it in the app — and it seems everything is where it should be, with no duplicates in the song list!

Just a quick look so far, as everything was finished today!

Some drawing annotations seem to be not exactly positioned — possibly because I had some crop/zoom issues within some of the scores. But in general it looks great, as all information seems to be preserved!

==> The few annotations failures (zoom factor) can be healed too by
importing a Backup.4sb file (not Archive.4sb) *after* you‘ve
- deleted forScore app
- reloaded forScore App from Apple Store
- imported the cleaned Archived.4sb file

==> Fortunately importing the Backup.4sb (does not contain the binaries) after a cleanup restore just updates meta data and annotations of existing songs but ignores previously removed duplicates!

```bash
# Analyze only — no files written
python3 clean_forscore_bookmarks.py "Archiv 2026-04-12 16-00-52.4sb" --dry-run

# Create cleaned file
python3 clean_forscore_bookmarks.py "Archiv 2026-04-12 16-00-52.4sb"

# Also merge metadata from duplicates into the kept entry
python3 clean_forscore_bookmarks.py "Archiv 2026-04-12 16-00-52.4sb" --merge-meta
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

---

### 4. `forScore2MS.py` *(in testing)*

This script creates a MobileSheets database file from an `Archiv*.4sb` file.
If you copy this file and the extracted files from script #2 to a fresh MobileSheets installation and switch in the app settings to the new library (the new database file), all songs and setlists with their metadata are available in MobileSheets — including audio links.

Annotations are not yet included and will be added in an upcoming version.

```bash
python3 forScore2MS.py "Archiv 2026-04-12 16-00-52.4sb"

# Dry run — parse and report, write no database
python3 forScore2MS.py "Archiv 2026-04-12 16-00-52.4sb" --dry-run
```

**Options:**

| Option | Description |
|--------|-------------|
| `-o, --output` | Output directory (default: `<input-stem>-2MS/` next to input) |
| `--dry-run` | Parse and report without writing any database |
| `-v, --verbose` | Print each inserted song title |
| `--midi-presets` | Import forScore MIDI presets as Smart Buttons |
| `--midi-song TITLE` | Attach Smart Buttons to an existing song with this title |
| `--midi-presets-new-song TITLE` | Create a new placeholder song and attach Smart Buttons to it |
| `--midi-columns N` | Override the number of Smart Button columns (default: auto) |
| `--midi-spacing PT` | Override the horizontal spacing between buttons in points (default: auto) |

The button grid layout is computed automatically: the widest label determines the button width (emoji count as double-width characters), which is used to derive the optimal column count and spacing so that buttons fill the available screen width without overlapping.

**What is imported:**
- Songs — every PDF with full metadata
- Bookmarks — each becomes a virtual song (page-range slice of a PDF)
- Setlists — all setlists with song membership and display order
- Audio links — linked MP3/M4A tracks
- MIDI presets — as Smart Buttons (requires `--midi-presets`)

> **Note — Emoji compatibility:**
> MobileSheets cannot render Unicode 14+ emoji (e.g. `🪈` U+1FA88 "Flute", added in 2021).
> Opening a song whose Smart Button labels contain such emoji causes the app to crash.
> `forScore2MS.py` automatically strips these characters from Smart Button labels during import.
> If you use newer emoji in forScore MIDI preset names, they will be silently removed.

**Output:** `<input-stem>-2MS/MobileSheets.db` — importable by the MobileSheets app.

> **Important — MobileSheets import steps:**
> After copying `MobileSheets.db` to your device, MobileSheets will **not** pick it up automatically.
> You must:
> 1. Close MobileSheets completely (force-quit the app).
> 2. Reopen MobileSheets.
> 3. Go to **Settings → Library** and switch to the library that contains the new database.
>
> Only after this library switch will all songs, setlists and Smart Buttons appear.

---

## Background

The forScore iPad app (sheet music reader) maintains all metadata — bookmarks, setlists, annotations — within the app. When iCloud Sync is active, sync interruptions cause bookmarks to accumulate massive numbers of duplicates (up to 93% of all bookmarks can be duplicates in affected libraries). There are no built-in tools to remove them.

Creating backups within forScore puts all data in a single `.4sb` file:

| Type | Filename prefix | Version | Contents |
|------|----------------|---------|----------|
| Backup | `Backup*.4sb` | `4SBV02` | Metadata only (bookmarks, setlists, text annotations) |
| Archive | `Archiv*.4sb` / `Archive*.4sb` | `4SBV03` | Metadata + all PDFs, audio files, PNG draw annotations |

Both use the same internal structure: a fixed 74-byte ASCII header, followed by a gzip-compressed Apple Binary Property List (bplist) containing all metadata, followed by a sequence of gzip-compressed file records (V03 only).

### What is a Bookmark?

In forScore, a **bookmark** is the primary navigation unit: it defines a piece of music as a page range within a PDF file. A single large PDF (e.g. a Real Book) can contain hundreds of bookmarks, each representing one song.

Duplicates arise when the same bookmark (same PDF + title + first page) gets multiple entries with different `Identifier` UUIDs due to iCloud sync conflicts.

### Draw Annotations

Handwritten ink strokes are stored as PNG files embedded in the `Archiv*.4sb` file, one per annotated page. The filename encodes the PDF name and page number (`<pdfname>|<page>.png`). Because deduplication only modifies bookmark *entries* in the metadata — never the PDF filenames — the PNG-to-page associations remain valid after cleaning. `clean_forscore_bookmarks.py` copies all embedded records byte-for-byte into the output file, so draw annotations are fully preserved.

## Shell Helpers

| Script | Purpose |
|--------|---------|
| `cleanup_meta.sh` | Remove duplicate rows from a forScore CSV export |
| `cleanup_scores.sh` | Deduplicate and organize PDFs and audio files in a directory |
| `copy_pdfs.sh` | Copy a list of PDF files from one directory to another |
| `example_workflow.sh` | Example end-to-end workflow |

## Restore Workflow

After cleaning an Archive:

1. On iPad: open forScore → **Settings → Backup & Restore**
2. Import the cleaned `*-cleaned.4sb` file via Files / AirDrop
3. Restore from that backup

## Requirements

- **Mac** (macOS) — the scripts have only been tested on macOS
- **Python 3.7+** — no external dependencies (stdlib only: `plistlib`, `gzip`, `zlib`, `json`, `sqlite3`)

### Installing Python 3 on a Mac

macOS does not ship with Python 3 by default. The recommended way to install it is via [Homebrew](https://brew.sh):

```bash
# 1. Install Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Install Python 3
brew install python

# 3. Verify
python3 --version
```

Alternatively, download the official installer from [python.org/downloads](https://www.python.org/downloads/).
