#!/bin/bash

# Script to remove duplicate rows from forScore CSV export
# Checks all columns for duplicates and creates a cleaned copy

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# File paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Print usage (before logging setup so help doesn't get logged)
usage() {
    cat << EOF
Usage: $0 -i <input.csv> [-h]

Options:
    -i FILE     Input CSV file to process (required)
    -h          Show this help message

Description:
    This script removes duplicate rows from the forScore CSV export file.
    It processes the specified input CSV file and creates a cleaned version
    without duplicates in the same directory with "-cleaned" suffix.
    
    The script performs several verification steps:
    1. Verifies all unique rows from original are present in cleaned file
    2. Checks if CSV entries have corresponding files in original folder
    3. Checks if PDF files in original folder have entries in CSV
    
    All output is logged to cleanup_meta.log

Examples:
    $0 -i csv/2025-12-28_forScore-Export.csv
    # Creates: csv/2025-12-28_forScore-Export-cleaned.csv
    
    $0 -h           # Show this help message
EOF
    exit 0
}

# Parse command line arguments
INPUT_FILE=""
while getopts "i:h" opt; do
    case $opt in
        i)
            INPUT_FILE="$OPTARG"
            ;;
        h)
            usage
            ;;
        *)
            echo -e "${RED}Invalid option: -$OPTARG${NC}" >&2
            usage
            ;;
    esac
done

# Check if input file was provided
if [ -z "$INPUT_FILE" ]; then
    echo -e "${RED}Error: Input file is required. Use -i to specify the input CSV file.${NC}" >&2
    echo ""
    usage
fi

# Convert input file to absolute path if it's relative
if [[ "$INPUT_FILE" != /* ]]; then
    # Relative path - make it relative to current working directory
    INPUT_FILE="$(cd "$(dirname "$INPUT_FILE")" && pwd)/$(basename "$INPUT_FILE")"
fi

# Check if input file exists
if [ ! -f "$INPUT_FILE" ]; then
    echo -e "${RED}Error: Input file not found: $INPUT_FILE${NC}" >&2
    exit 1
fi

# Derive output file path: same directory, add "-cleaned" before .csv extension
INPUT_DIR="$(dirname "$INPUT_FILE")"
INPUT_BASENAME="$(basename "$INPUT_FILE")"
INPUT_NAME="${INPUT_BASENAME%.*}"
INPUT_EXT="${INPUT_BASENAME##*.}"

# Handle case where file has no extension
if [ "$INPUT_EXT" = "$INPUT_BASENAME" ]; then
    OUTPUT_FILE="${INPUT_DIR}/${INPUT_NAME}-cleaned"
else
    OUTPUT_FILE="${INPUT_DIR}/${INPUT_NAME}-cleaned.${INPUT_EXT}"
fi

LOG_FILE="${SCRIPT_DIR}/cleanup_meta.log"
ORIGINAL_DIR="${SCRIPT_DIR}/original"

# Setup logging - redirect all output to both terminal and log file
# Clear previous log and add timestamp header
{
    echo "=========================================="
    echo "forScore CSV Cleanup Script"
    echo "Started: $(date)"
    echo "=========================================="
    echo ""
} > "$LOG_FILE"

# Redirect all output to both terminal and log file
exec > >(tee -a "$LOG_FILE")
exec 2>&1

echo -e "${BLUE}=== forScore CSV Duplicate Removal ===${NC}"
echo -e "${BLUE}Input file:${NC} $INPUT_FILE"
echo -e "${BLUE}Output file:${NC} $OUTPUT_FILE"
echo ""

# Count total lines (including header)
TOTAL_LINES=$(wc -l < "$INPUT_FILE" | tr -d ' ')
DATA_LINES=$((TOTAL_LINES - 1))

echo -e "${BLUE}Processing $DATA_LINES data rows...${NC}"

# Extract and write header first
head -n 1 "$INPUT_FILE" > "$OUTPUT_FILE"

# Process data rows: remove duplicates while preserving order
# Using awk to track seen rows and only output first occurrence
# This handles CSV properly by comparing entire lines
tail -n +2 "$INPUT_FILE" | awk '{if (!seen[$0]++) print}' >> "$OUTPUT_FILE"

# Verify output file was created
if [ ! -f "$OUTPUT_FILE" ]; then
    echo -e "${RED}Error: Failed to create output file${NC}" >&2
    exit 1
fi

# Count lines in output
OUTPUT_LINES=$(wc -l < "$OUTPUT_FILE" | tr -d ' ')
OUTPUT_DATA_LINES=$((OUTPUT_LINES - 1))
DUPLICATES_REMOVED=$((DATA_LINES - OUTPUT_DATA_LINES))

echo ""
echo -e "${BLUE}=== Verification ===${NC}"

# Verify that every unique row from original exists in cleaned file
# Create temporary sorted files for comparison
TEMP_ORIGINAL=$(mktemp)
TEMP_CLEANED=$(mktemp)

# Extract unique rows from original (excluding header) and sort
tail -n +2 "$INPUT_FILE" | sort -u > "$TEMP_ORIGINAL"

# Extract rows from cleaned file (excluding header) and sort
tail -n +2 "$OUTPUT_FILE" | sort > "$TEMP_CLEANED"

# Count unique rows in original
UNIQUE_ORIGINAL=$(wc -l < "$TEMP_ORIGINAL" | tr -d ' ')
CLEANED_ROWS=$(wc -l < "$TEMP_CLEANED" | tr -d ' ')

echo -e "${BLUE}Validating $UNIQUE_ORIGINAL unique entries from original against $CLEANED_ROWS entries in cleaned file...${NC}"

# Check if all original unique rows are in cleaned file
MISSING_ROWS=$(comm -23 "$TEMP_ORIGINAL" "$TEMP_CLEANED" | wc -l | tr -d ' ')

# Clean up temp files
rm -f "$TEMP_ORIGINAL" "$TEMP_CLEANED"

if [ "$MISSING_ROWS" -eq 0 ]; then
    echo -e "${GREEN}✓ Verification passed: All $UNIQUE_ORIGINAL unique rows from original are present in cleaned file${NC}"
else
    echo -e "${RED}✗ Verification failed: $MISSING_ROWS unique row(s) from original are missing in cleaned file${NC}" >&2
    echo -e "${RED}This should not happen - please check the deduplication process${NC}" >&2
    exit 1
fi

echo ""
echo -e "${BLUE}=== File Verification Step 1: CSV entries → Original folder ===${NC}"

# Check if original directory exists
if [ ! -d "$ORIGINAL_DIR" ]; then
    echo -e "${YELLOW}Warning: Original directory not found: $ORIGINAL_DIR${NC}"
    echo -e "${YELLOW}Skipping file verification steps${NC}"
else
    # Extract Dateiname column (first column) from cleaned CSV (excluding header)
    # Use Python to properly parse CSV and handle quoted fields
    TEMP_CSV_FILES=$(mktemp)
    tail -n +2 "$OUTPUT_FILE" | python3 -c "
import csv
import sys
for row in csv.reader(sys.stdin):
    if row:
        print(row[0])
" | sort > "$TEMP_CSV_FILES"
    
    CSV_FILENAME_COUNT=$(wc -l < "$TEMP_CSV_FILES" | tr -d ' ')
    echo -e "${BLUE}Checking $CSV_FILENAME_COUNT filenames from CSV against original folder...${NC}"
    
    MISSING_FILES=0
    MISSING_FILES_LIST=$(mktemp)
    
    while IFS= read -r filename; do
        # Remove any leading/trailing whitespace and quotes
        filename=$(echo "$filename" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        
        if [ ! -f "${ORIGINAL_DIR}/${filename}" ]; then
            echo "$filename" >> "$MISSING_FILES_LIST"
            ((MISSING_FILES++))
        fi
    done < "$TEMP_CSV_FILES"
    
    if [ "$MISSING_FILES" -eq 0 ]; then
        echo -e "${GREEN}✓ All $CSV_FILENAME_COUNT CSV entries have corresponding files in original folder${NC}"
    else
        echo -e "${YELLOW}⚠ $MISSING_FILES out of $CSV_FILENAME_COUNT CSV entries do not have corresponding files in original folder${NC}"
        echo -e "${YELLOW}Missing files:${NC}"
        while IFS= read -r missing_file; do
            echo -e "${YELLOW}  - $missing_file${NC}"
        done < "$MISSING_FILES_LIST"
    fi
    
    rm -f "$TEMP_CSV_FILES" "$MISSING_FILES_LIST"
    
    echo ""
    echo -e "${BLUE}=== File Verification Step 2: Original folder PDFs → CSV entries ===${NC}"
    
    # Find all PDF files in original folder
    TEMP_PDF_FILES=$(mktemp)
    find "$ORIGINAL_DIR" -maxdepth 1 -type f -iname "*.pdf" -exec basename {} \; | sort > "$TEMP_PDF_FILES"
    
    PDF_COUNT=$(wc -l < "$TEMP_PDF_FILES" | tr -d ' ')
    echo -e "${BLUE}Checking $PDF_COUNT PDF files from original folder against CSV entries...${NC}"
    
    # Create a sorted list of CSV filenames for comparison
    TEMP_CSV_FILENAMES=$(mktemp)
    tail -n +2 "$OUTPUT_FILE" | python3 -c "
import csv
import sys
for row in csv.reader(sys.stdin):
    if row:
        print(row[0].strip())
" | sort > "$TEMP_CSV_FILENAMES"
    
    MISSING_IN_CSV=0
    MISSING_IN_CSV_LIST=$(mktemp)
    
    while IFS= read -r pdf_file; do
        # Check if PDF filename exists in CSV
        if ! grep -Fxq "$pdf_file" "$TEMP_CSV_FILENAMES"; then
            echo "$pdf_file" >> "$MISSING_IN_CSV_LIST"
            ((MISSING_IN_CSV++))
        fi
    done < "$TEMP_PDF_FILES"
    
    if [ "$MISSING_IN_CSV" -eq 0 ]; then
        echo -e "${GREEN}✓ All $PDF_COUNT PDF files from original folder have entries in CSV${NC}"
    else
        echo -e "${YELLOW}⚠ $MISSING_IN_CSV out of $PDF_COUNT PDF files from original folder do not have entries in CSV${NC}"
        echo -e "${YELLOW}PDF files missing from CSV:${NC}"
        while IFS= read -r missing_pdf; do
            echo -e "${YELLOW}  - $missing_pdf${NC}"
        done < "$MISSING_IN_CSV_LIST"
    fi
    
    rm -f "$TEMP_PDF_FILES" "$TEMP_CSV_FILENAMES" "$MISSING_IN_CSV_LIST"
fi

echo ""
echo -e "${GREEN}=== Cleanup complete ===${NC}"
echo -e "${GREEN}Original rows:${NC} $DATA_LINES"
echo -e "${GREEN}Unique rows in original:${NC} $UNIQUE_ORIGINAL"
echo -e "${GREEN}Cleaned rows:${NC} $OUTPUT_DATA_LINES"
echo -e "${GREEN}Duplicates removed:${NC} $DUPLICATES_REMOVED"
echo -e "${GREEN}Output saved to:${NC} $OUTPUT_FILE"
echo -e "${GREEN}Log file saved to:${NC} $LOG_FILE"

