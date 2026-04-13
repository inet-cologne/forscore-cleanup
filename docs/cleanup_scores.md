# cleanup_scores.sh - Music Library Cleanup Script

## Overview

The `cleanup_scores.sh` script helps organize and clean up your forScore music library by processing PDF scores and audio files (MP3, WAV, MIDI, etc.) in the `original` directory.

## Features

- **File Type Selection**: Process audio files only, PDF files only, or both
- **Dry Run Mode**: Preview changes without modifying files (default)
- **Filename Cleaning**: Remove numeric prefixes from audio filenames (e.g., "01 ", "01-1 ")
- **Duplicate Detection**: Identify and move duplicate files based on filename patterns and file size
- **Setlist Handling**: Automatically moves setlist PDFs to the `untouched` directory

## Quick Start

```bash
# Preview what would be done (dry-run mode)
./cleanup_scores.sh -a -d

# Actually execute the cleanup
./cleanup_scores.sh -a -d -x

# Clean PDF filenames (dry-run)
./cleanup_scores.sh -p -c

# Clean and deduplicate audio files (execute)
./cleanup_scores.sh -a -c -d -x
```

## Usage

```bash
./cleanup_scores.sh [-a|-p] [-x] [-c] [-d] [-h]
```

### Options

| Option | Description |
|--------|-------------|
| `-a` | Process audio files only (mp3, wav, midi, etc.) |
| `-p` | Process PDF files only |
| `-x` | Execute tasks (without this flag, only dry-run mode) |
| `-c` | Clean filenames (remove prefixes like "01", "01-1") |
| `-d` | Remove duplicates (move to "duplicated" subdirectory) |
| `-h` | Show help message |

**Note**: At least one of `-a` or `-p` must be specified.

## Examples

### Example 1: Remove Duplicate Audio Files

```bash
# Preview duplicates (dry-run)
./cleanup_scores.sh -a -d

# Actually remove duplicates
./cleanup_scores.sh -a -d -x
```

This will:
- Find audio files with patterns like "filename 2.mp3" or "filename 3.mp3"
- Compare them with the original "filename.mp3"
- If file sizes match, move duplicates to `duplicated/` directory

### Example 2: Clean Audio Filenames

```bash
# Preview filename changes (dry-run)
./cleanup_scores.sh -a -c

# Execute filename cleaning
./cleanup_scores.sh -a -c -x
```

This will:
- Remove numeric prefixes from audio filenames (e.g., "01 Song.mp3" → "Song.mp3")
- Handle conflicts by adding number suffixes if needed

### Example 3: Process PDF Files Only

```bash
# Preview PDF duplicate removal
./cleanup_scores.sh -p -d

# Execute PDF cleanup
./cleanup_scores.sh -p -d -x
```

### Example 4: Combined Operations

```bash
# Clean filenames and remove duplicates for audio files
./cleanup_scores.sh -a -c -d -x
```

## Directory Structure

The script expects the following directory structure:

```
project-root/
├── cleanup_scores.sh
├── original/          # Source files to process
├── untouched/         # Files moved here (setlists, non-score files)
└── duplicated/        # Duplicate files moved here
```

## How It Works

### Duplicate Detection

The script identifies duplicates by:
1. Looking for files matching pattern: `filename N.ext` where N is a number
2. Checking if original `filename.ext` exists
3. Comparing file sizes
4. If sizes match, the numbered file is considered a duplicate

### Filename Cleaning

For audio files:
- Removes prefixes like "01 ", "01-1 ", "01. " from filenames
- Preserves the rest of the filename
- PDF files are NOT renamed

### Setlist Handling

PDF files containing "setlist", "set-list", "set_list", or "set list" in their filename are automatically moved to the `untouched/` directory.

## Safety Features

- **Dry Run by Default**: The script runs in preview mode unless `-x` is specified
- **No Data Loss**: Files are moved, not deleted
- **Clear Output**: Color-coded messages show what will happen or what was done

## Error Handling

The script will exit with an error if:
- The `original` directory doesn't exist
- No file type option (`-a` or `-p`) is specified
- Invalid options are provided

## Help

To see the help message:

```bash
./cleanup_scores.sh -h
```

Or run without any options (will show help):

```bash
./cleanup_scores.sh
```




