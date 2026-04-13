# cleanup_meta.sh - CSV Metadata Cleanup Script

## Overview

The `cleanup_meta.sh` script removes duplicate rows from the forScore CSV export file and performs comprehensive verification to ensure data integrity.

## Features

- **Duplicate Removal**: Removes duplicate rows based on all column values
- **Data Verification**: Verifies that all unique rows from original are preserved
- **File Verification**: Checks correspondence between CSV entries and actual files
- **Comprehensive Logging**: All output is logged to `cleanup_meta.log`

## Quick Start

```bash
# Run the cleanup script with input file
./cleanup_meta.sh -i csv/2025-12-28_forScore-Export.csv

# Show help message
./cleanup_meta.sh -h
```

## Usage

```bash
./cleanup_meta.sh -i <input.csv> [-h]
```

### Options

| Option | Description |
|--------|-------------|
| `-i FILE` | Input CSV file to process (required) |
| `-h` | Show help message |

**Note**: The input file (`-i`) is required. The cleaned file will be created in the same directory as the input file with "-cleaned" suffix.

## Examples

### Example 1: Basic Usage

```bash
./cleanup_meta.sh -i csv/2025-12-28_forScore-Export.csv
```

This will:
- Process `csv/2025-12-28_forScore-Export.csv`
- Remove duplicate rows
- Create `csv/2025-12-28_forScore-Export-cleaned.csv` in the same directory
- Perform all verification steps
- Log everything to `cleanup_meta.log`

### Example 2: Different Input File

```bash
./cleanup_meta.sh -i /path/to/my-export.csv
```

This will create `/path/to/my-export-cleaned.csv` in the same directory.

### Example 3: Show Help

```bash
./cleanup_meta.sh -h
```

## What the Script Does

### Step 1: Duplicate Removal

- Reads the input CSV file (`csv/2025-12-28_forScore-Export.csv`)
- Identifies duplicate rows (where all column values match)
- Keeps only the first occurrence of each unique row
- Creates cleaned CSV file (`csv/2025-12-28_forScore-Export-cleaned.csv`)

### Step 2: Data Integrity Verification

Verifies that all unique rows from the original CSV are present in the cleaned file. This ensures no data was accidentally lost during deduplication.

### Step 3: File Verification (Step 1)

Checks if every filename in the "Dateiname" column (column A) of the cleaned CSV has a corresponding file in the `original/` directory.

**Output Example:**
```
✓ All 3029 CSV entries have corresponding files in original folder
```

Or if files are missing:
```
⚠ 5 out of 3029 CSV entries do not have corresponding files in original folder
Missing files:
  - example1.pdf
  - example2.pdf
```

### Step 4: File Verification (Step 2)

Checks if every PDF file in the `original/` directory has a corresponding entry in the cleaned CSV's "Dateiname" column.

**Output Example:**
```
✓ All 1250 PDF files from original folder have entries in CSV
```

Or if PDFs are missing from CSV:
```
⚠ 10 out of 1250 PDF files from original folder do not have entries in CSV
PDF files missing from CSV:
  - file1.pdf
  - file2.pdf
```

## Input and Output Files

### Input File
- **Specification**: Provided via `-i` option
- **Format**: CSV file exported from forScore
- **Required**: Must exist and be specified with `-i` option

### Output Files

1. **Cleaned CSV**
   - **Location**: Same directory as input file, with "-cleaned" suffix
   - **Naming**: If input is `file.csv`, output is `file-cleaned.csv`
   - **Content**: Original CSV with duplicate rows removed
   - **Format**: Same CSV structure as input

2. **Log File**
   - **Location**: `cleanup_meta.log`
   - **Content**: Complete log of script execution
   - **Format**: Timestamped log with all output

## CSV Structure

The script expects a CSV file with the following columns:
- `Dateiname` (column A) - Filename
- `Titel` - Title
- `Erste Seite (Lesezeichen)` - First page bookmark
- `Letzte Seite (Lesezeichen)` - Last page bookmark
- `Komponisten` - Composers
- `Genres` - Genres
- `Tags` - Tags
- `Etikette` - Labels
- `ID` - ID
- `Bewertung` - Rating
- `Niveau` - Level
- `Minuten` - Minutes
- `Seconds` - Seconds
- `keysf` - Key signature (flat)
- `keymi` - Key signature (minor)

## Verification Details

### Verification Step 1: CSV → Files

- Extracts all filenames from the "Dateiname" column
- Checks if each file exists in `original/` directory
- Reports missing files

### Verification Step 2: Files → CSV

- Finds all PDF files in `original/` directory
- Checks if each PDF has an entry in the CSV
- Reports PDFs missing from CSV

## Logging

All script output is automatically logged to `cleanup_meta.log` with:
- Timestamp header
- Complete execution log
- All verification results
- Statistics and summaries

The log file is overwritten each time the script runs, so previous runs are not preserved.

## Error Handling

The script will exit with an error if:
- Input CSV file doesn't exist
- Verification fails (unique rows missing from cleaned file)
- Python3 is not available (required for CSV parsing)

## Requirements

- **Bash**: Standard bash shell
- **Python3**: Required for proper CSV parsing (handles quoted fields)
- **Standard Unix tools**: `head`, `tail`, `awk`, `sort`, `comm`, `find`, `wc`

## Help

To see the help message:

```bash
./cleanup_meta.sh -h
```

## Tips

1. **Review the Log**: Always check `cleanup_meta.log` after running the script for detailed information
2. **Verify Results**: Check the verification steps output to ensure data integrity
3. **Backup First**: Consider backing up your CSV file before running the script
4. **Check Missing Files**: If verification shows missing files, investigate why they're missing

## Troubleshooting

### Issue: "Input file is required"
- **Solution**: Use `-i` option to specify the input CSV file: `./cleanup_meta.sh -i path/to/file.csv`

### Issue: "Input file not found"
- **Solution**: Check that the file path specified with `-i` is correct and the file exists

### Issue: "Original directory not found"
- **Solution**: The script will skip file verification steps but will still clean the CSV

### Issue: Python3 not found
- **Solution**: Install Python3 or ensure it's in your PATH

### Issue: Verification shows missing files
- **Investigation**: Check if filenames in CSV match actual filenames exactly (case-sensitive, special characters)



