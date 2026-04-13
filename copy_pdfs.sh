#!/bin/bash
#
# PDF-Kopier-Script für MobileSheets-Import
#
# Dieses Script hilft beim Kopieren aller benötigten PDF-Dateien
# aus einem Quellverzeichnis in das MobileSheets-Import-Verzeichnis.
#

set -e

echo "========================================="
echo "PDF-Dateien für MobileSheets kopieren"
echo "========================================="
echo ""

# Parameter
PDF_LIST="${1:-mobilesheets_export/pdf_files.txt}"
SOURCE_DIR="${2:-original}"
TARGET_DIR="${3:-mobilesheets_export/pdfs}"

if [ ! -f "$PDF_LIST" ]; then
    echo "Fehler: PDF-Liste nicht gefunden: $PDF_LIST"
    echo ""
    echo "Verwendung: $0 [pdf_list] [source_dir] [target_dir]"
    echo ""
    echo "Beispiel:"
    echo "  $0 mobilesheets_export/pdf_files.txt original mobilesheets_export/pdfs"
    exit 1
fi

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Fehler: Quellverzeichnis nicht gefunden: $SOURCE_DIR"
    echo ""
    echo "Bitte geben Sie das Verzeichnis an, in dem sich Ihre PDF-Dateien befinden."
    exit 1
fi

# Erstelle Zielverzeichnis
mkdir -p "$TARGET_DIR"

echo "PDF-Liste:        $PDF_LIST"
echo "Quellverzeichnis: $SOURCE_DIR"
echo "Zielverzeichnis:  $TARGET_DIR"
echo ""

# Zähle Dateien
TOTAL=$(grep -c "^[^#]" "$PDF_LIST" || true)
COPIED=0
MISSING=0
MISSING_FILES=()

echo "Kopiere $TOTAL PDF-Dateien..."
echo ""

# Lese PDF-Liste und kopiere Dateien
while IFS= read -r pdf_file; do
    # Überspringe Kommentare und Leerzeilen
    [[ "$pdf_file" =~ ^#.*$ ]] && continue
    [[ -z "$pdf_file" ]] && continue
    
    # Suche nach der Datei im Quellverzeichnis
    SOURCE_FILE="$SOURCE_DIR/$pdf_file"
    
    if [ -f "$SOURCE_FILE" ]; then
        cp "$SOURCE_FILE" "$TARGET_DIR/"
        COPIED=$((COPIED + 1))
        
        # Zeige Fortschritt alle 10 Dateien
        if [ $((COPIED % 10)) -eq 0 ]; then
            echo "  $COPIED/$TOTAL Dateien kopiert..."
        fi
    else
        MISSING=$((MISSING + 1))
        MISSING_FILES+=("$pdf_file")
        echo "  ⚠ Nicht gefunden: $pdf_file"
    fi
done < "$PDF_LIST"

echo ""
echo "========================================="
echo "Zusammenfassung"
echo "========================================="
echo ""
echo "Erfolgreich kopiert: $COPIED"
echo "Fehlende Dateien:    $MISSING"
echo ""

if [ $MISSING -gt 0 ]; then
    echo "⚠ Warnung: $MISSING Dateien wurden nicht gefunden!"
    echo ""
    echo "Fehlende Dateien:"
    for file in "${MISSING_FILES[@]}"; do
        echo "  - $file"
    done
    echo ""
    echo "Hinweise:"
    echo "  - Prüfen Sie den Pfad zum Quellverzeichnis"
    echo "  - Achten Sie auf Groß-/Kleinschreibung bei Dateinamen"
    echo "  - Manche Dateien könnten umbenannt worden sein"
    echo ""
    
    # Erstelle Liste fehlender Dateien
    MISSING_LIST="$TARGET_DIR/missing_files.txt"
    printf "%s\n" "${MISSING_FILES[@]}" > "$MISSING_LIST"
    echo "Liste fehlender Dateien gespeichert: $MISSING_LIST"
    echo ""
fi

if [ $COPIED -gt 0 ]; then
    echo "✓ PDFs erfolgreich nach $TARGET_DIR kopiert"
    echo ""
    echo "Nächste Schritte:"
    echo "  1. Überprüfen Sie das Zielverzeichnis: $TARGET_DIR"
    echo "  2. Kopieren Sie den Ordner auf Ihr MobileSheets-Gerät"
    echo "  3. Importieren Sie die CSV in MobileSheets"
    echo ""
fi
