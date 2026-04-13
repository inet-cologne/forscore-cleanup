#!/bin/bash

# Cleanup script for forScore music library
# Handles PDF scores and audio files (mp3, wav, midi)

set -euo pipefail

# Default flags
AUDIO_ONLY=false
PDF_ONLY=false
EXECUTE=false
CLEAN_FILENAME=false
REMOVE_DUPLICATES=false
DRY_RUN=true

# File extensions
AUDIO_EXTENSIONS=("mp3" "wav" "midi" "mid" "MID" "MP3" "WAV" "Mp3" "M4A" "m4a")
PDF_EXTENSIONS=("pdf" "PDF")

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print usage
usage() {
    cat << EOF
Usage: $0 [-a|-p] [-x] [-c] [-d]

Options:
    -a          Process audio files only (mp3, wav, midi, etc.)
    -p          Process PDF files only
    -x          Execute tasks (without this flag, only dry-run mode)
    -c          Clean filenames (remove prefixes like "01", "01-1")
    -d          Remove duplicates (move to "duplicated" subdirectory)
    -h          Show this help message

At least one of -a or -p must be specified.

Examples:
    $0 -a -d -x          # Remove duplicates from audio files (execute)
    $0 -p -c              # Clean PDF filenames (dry-run)
    $0 -a -c -d -x        # Clean and deduplicate audio files (execute)
EOF
    exit 1
}

# Parse command line arguments
# Check if no arguments provided
if [ $# -eq 0 ]; then
    usage
fi

while getopts "apxcdh" opt; do
    case $opt in
        a)
            AUDIO_ONLY=true
            ;;
        p)
            PDF_ONLY=true
            ;;
        x)
            EXECUTE=true
            DRY_RUN=false
            ;;
        c)
            CLEAN_FILENAME=true
            ;;
        d)
            REMOVE_DUPLICATES=true
            ;;
        h)
            usage
            ;;
        *)
            echo "Invalid option: -$OPTARG" >&2
            usage
            ;;
    esac
done

# Check that at least one of -a or -p is specified
if [ "$AUDIO_ONLY" = false ] && [ "$PDF_ONLY" = false ]; then
    echo -e "${RED}Error: At least one of -a or -p must be specified${NC}" >&2
    usage
fi

# Get script directory (root of project)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_DIR="${SCRIPT_DIR}/original"

# Create subdirectories if needed
if [ "$EXECUTE" = true ]; then
    mkdir -p "${SCRIPT_DIR}/untouched"
    mkdir -p "${SCRIPT_DIR}/duplicated"
    mkdir -p "${ORIGINAL_DIR}"
fi

# Check if original directory exists
if [ ! -d "$ORIGINAL_DIR" ]; then
    echo -e "${RED}Error: 'original' directory does not exist. Please create it and place files there.${NC}" >&2
    exit 1
fi

# Check if file extension matches audio
is_audio_file() {
    local filename="$1"
    local ext="${filename##*.}"
    for audio_ext in "${AUDIO_EXTENSIONS[@]}"; do
        if [ "$ext" = "$audio_ext" ]; then
            return 0
        fi
    done
    return 1
}

# Check if file extension matches PDF
is_pdf_file() {
    local filename="$1"
    local ext="${filename##*.}"
    for pdf_ext in "${PDF_EXTENSIONS[@]}"; do
        if [ "$ext" = "$pdf_ext" ]; then
            return 0
        fi
    done
    return 1
}

# Check if file should be processed
should_process_file() {
    local filename="$1"
    if [ "$AUDIO_ONLY" = true ] && is_audio_file "$filename"; then
        return 0
    fi
    if [ "$PDF_ONLY" = true ] && is_pdf_file "$filename"; then
        return 0
    fi
    return 1
}

# Check if file is a score-related file (audio or PDF)
is_score_related_file() {
    local filename="$1"
    if is_audio_file "$filename" || is_pdf_file "$filename"; then
        return 0
    fi
    return 1
}

# Move untouched files to subdirectory (only files that are neither audio nor PDF)
# Also moves PDF files containing "setlist" or "set-list" to untouched
move_untouched_files() {
    local count=0
    while IFS= read -r -d '' file; do
        local basename=$(basename "$file")
        
        # Move PDF files with "setlist", "set-list", "set_list", or "set list" in filename to untouched
        if is_pdf_file "$basename"; then
            if [[ "$basename" =~ [Ss]et[-_\ ]?[Ll]ist ]]; then
                if [ "$DRY_RUN" = true ]; then
                    echo -e "${YELLOW}[DRY RUN] Would move setlist PDF:${NC} $basename -> ../untouched/"
                else
                    mv "$file" "${SCRIPT_DIR}/untouched/"
                    echo -e "${GREEN}Moved setlist PDF:${NC} $basename -> ../untouched/"
                fi
                ((count++))
                continue
            fi
        fi
        
        # Only move files that are NOT score-related (neither audio nor PDF)
        if ! is_score_related_file "$basename"; then
            if [ "$DRY_RUN" = true ]; then
                echo -e "${YELLOW}[DRY RUN] Would move:${NC} $basename -> ../untouched/"
            else
                mv "$file" "${SCRIPT_DIR}/untouched/"
                echo -e "${GREEN}Moved:${NC} $basename -> ../untouched/"
            fi
            ((count++))
        fi
    done < <(find "$ORIGINAL_DIR" -maxdepth 1 -type f -print0)
    
    if [ "$count" -gt 0 ]; then
        if [ "$DRY_RUN" = true ]; then
            echo -e "${BLUE}Would move $count untouched file(s) to '../untouched/' subdirectory${NC}"
        else
            echo -e "${GREEN}Moved $count untouched file(s) to '../untouched/' subdirectory${NC}"
        fi
    fi
}

# Remove duplicates based on filename pattern and file size
remove_duplicates() {
    local count=0
    
    # Process files and check for duplicates
    while IFS= read -r -d '' file; do
        local basename=$(basename "$file")
        if ! should_process_file "$basename"; then
            continue
        fi
        
        local size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)
        
        # Check if filename matches pattern: "something 2.ext" or "something 3.ext" etc.
        if [[ "$basename" =~ ^(.+)\ ([0-9]+)\.([^.]+)$ ]]; then
            local base_part="${BASH_REMATCH[1]}"
            local number="${BASH_REMATCH[2]}"
            local ext="${BASH_REMATCH[3]}"
            local original_name="${base_part}.${ext}"
            
            # Check if original file exists
            local original_path="${ORIGINAL_DIR}/${original_name}"
            if [ -f "$original_path" ]; then
                local original_size=$(stat -f%z "$original_path" 2>/dev/null || stat -c%s "$original_path" 2>/dev/null)
                
                # If sizes match, it's a duplicate
                if [ "$size" = "$original_size" ]; then
                    if [ "$DRY_RUN" = true ]; then
                        echo -e "${YELLOW}[DRY RUN] Would move duplicate:${NC} $basename -> ../duplicated/"
                    else
                        mv "$file" "${SCRIPT_DIR}/duplicated/"
                        echo -e "${GREEN}Moved duplicate:${NC} $basename -> ../duplicated/"
                    fi
                    ((count++))
                else
                    if [ "$DRY_RUN" = true ]; then
                        echo -e "${BLUE}[DRY RUN] Keeping (different size):${NC} $basename (size: $size vs original: $original_size)"
                    fi
                fi
            fi
        fi
    done < <(find "$ORIGINAL_DIR" -maxdepth 1 -type f -print0)
    
    if [ "$count" -gt 0 ]; then
        if [ "$DRY_RUN" = true ]; then
            echo -e "${BLUE}Would move $count duplicate file(s) to '../duplicated/' subdirectory${NC}"
        else
            echo -e "${GREEN}Moved $count duplicate file(s) to '../duplicated/' subdirectory${NC}"
        fi
    else
        echo -e "${BLUE}No duplicates found${NC}"
    fi
}

# Clean filename by removing numeric prefixes
clean_filename() {
    local count=0
    
    while IFS= read -r -d '' file; do
        local basename=$(basename "$file")
        if ! should_process_file "$basename"; then
            continue
        fi
        
        local dirname=$(dirname "$file")
        local new_name="$basename"
        local changed=false
        
        # Only clean audio files: remove prefixes like "01 ", "01-1 ", "01. ", etc.
        # PDF files should NOT be renamed
        if is_audio_file "$basename"; then
            # Pattern: starts with digits, optionally followed by dash and more digits, then space or dot
            if [[ "$basename" =~ ^([0-9]+(-[0-9]+)?[. ]+)(.+)$ ]]; then
                new_name="${BASH_REMATCH[3]}"
                changed=true
            fi
        fi
        
        if [ "$changed" = true ]; then
            local new_path="${ORIGINAL_DIR}/${new_name}"
            local file_size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)
            local final_name="$new_name"
            local final_path="$new_path"
            
            # Check if target already exists
            if [ -f "$new_path" ] && [ "$new_path" != "$file" ]; then
                local target_size=$(stat -f%z "$new_path" 2>/dev/null || stat -c%s "$new_path" 2>/dev/null)
                
                # If sizes match, it's a duplicate - move to duplicated directory
                if [ "$file_size" = "$target_size" ]; then
                    if [ "$DRY_RUN" = true ]; then
                        echo -e "${YELLOW}[DRY RUN] Would move duplicate (after rename):${NC} $basename -> ../duplicated/"
                    else
                        mkdir -p "${SCRIPT_DIR}/duplicated"
                        mv "$file" "${SCRIPT_DIR}/duplicated/"
                        echo -e "${GREEN}Moved duplicate (after rename):${NC} $basename -> ../duplicated/"
                    fi
                    ((count++))
                else
                    # Target exists but different size - find next available number suffix
                    local base_part="${new_name%.*}"
                    local ext="${new_name##*.}"
                    local suffix_num=2
                    
                    # Find the next available number suffix
                    while [ -f "${ORIGINAL_DIR}/${base_part} ${suffix_num}.${ext}" ]; do
                        ((suffix_num++))
                    done
                    
                    final_name="${base_part} ${suffix_num}.${ext}"
                    final_path="${ORIGINAL_DIR}/${final_name}"
                    
                    if [ "$DRY_RUN" = true ]; then
                        echo -e "${YELLOW}[DRY RUN] Would rename (with suffix):${NC} $basename -> $final_name"
                    else
                        mv "$file" "$final_path"
                        echo -e "${GREEN}Renamed (with suffix):${NC} $basename -> $final_name"
                    fi
                    ((count++))
                fi
            else
                # Target doesn't exist - rename normally
                if [ "$DRY_RUN" = true ]; then
                    echo -e "${YELLOW}[DRY RUN] Would rename:${NC} $basename -> $new_name"
                else
                    mv "$file" "$new_path"
                    echo -e "${GREEN}Renamed:${NC} $basename -> $new_name"
                fi
                ((count++))
            fi
        fi
    done < <(find "$ORIGINAL_DIR" -maxdepth 1 -type f -print0)
    
    if [ "$count" -gt 0 ]; then
        if [ "$DRY_RUN" = true ]; then
            echo -e "${BLUE}Would rename $count file(s)${NC}"
        else
            echo -e "${GREEN}Renamed $count file(s)${NC}"
        fi
    else
        echo -e "${BLUE}No files need renaming${NC}"
    fi
}

# Main execution
main() {
    echo -e "${BLUE}=== forScore Cleanup Script ===${NC}"
    if [ "$DRY_RUN" = true ]; then
        echo -e "${YELLOW}DRY RUN MODE - No files will be modified${NC}"
    else
        echo -e "${GREEN}EXECUTE MODE - Files will be modified${NC}"
    fi
    echo ""
    
    # Show what will be processed
    if [ "$AUDIO_ONLY" = true ]; then
        echo -e "${BLUE}Processing: Audio files${NC}"
    fi
    if [ "$PDF_ONLY" = true ]; then
        echo -e "${BLUE}Processing: PDF files${NC}"
    fi
    echo ""
    
    # Step 1: Move untouched files
    echo -e "${BLUE}--- Step 1: Moving untouched files ---${NC}"
    move_untouched_files
    echo ""
    
    # Step 2: Remove duplicates (if requested)
    if [ "$REMOVE_DUPLICATES" = true ]; then
        echo -e "${BLUE}--- Step 2: Removing duplicates ---${NC}"
        remove_duplicates
        echo ""
    fi
    
    # Step 3: Clean filenames (if requested)
    if [ "$CLEAN_FILENAME" = true ]; then
        echo -e "${BLUE}--- Step 3: Cleaning filenames ---${NC}"
        clean_filename
        echo ""
    fi
    
    echo -e "${GREEN}=== Cleanup complete ===${NC}"
}

# Run main function
main

