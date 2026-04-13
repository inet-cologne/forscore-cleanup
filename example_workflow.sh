#!/bin/bash
# 
# Beispiel-Workflow: forScore → MobileSheets Migration
# 
# Dieses Script zeigt den kompletten Prozess von der forScore Backup-Datei
# bis zur fertigen CSV-Importdatei für MobileSheets.
#

set -e  # Beende bei Fehler

echo "========================================="
echo "forScore → MobileSheets Migration"
echo "========================================="
echo ""

# Konfiguration
BACKUP_FILE="$1"
OUTPUT_DIR="mobilesheets_export"

if [ -z "$BACKUP_FILE" ]; then
    echo "Verwendung: $0 <backup.4sb>"
    echo ""
    echo "Beispiel:"
    echo "  $0 'Backup 2026-01-21.4sb'"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "Fehler: Backup-Datei nicht gefunden: $BACKUP_FILE"
    exit 1
fi

# Extrahiere Basis-Namen (ohne .4sb)
BASENAME=$(basename "$BACKUP_FILE" .4sb)
echo "Verarbeite: $BASENAME"
echo ""

# Schritt 1: Backup bereinigen
echo "Schritt 1: Backup bereinigen..."
echo "--------------------------------------"
python3 clean_forscore_bookmarks.py "$BACKUP_FILE" --page2item

if [ ! -f "$BASENAME/$BASENAME-cleaned.json" ]; then
    echo "Fehler: Bereinigte JSON-Datei wurde nicht erstellt"
    exit 1
fi

echo "✓ Backup bereinigt"
echo ""

# Schritt 2: MobileSheets CSV erstellen
echo "Schritt 2: MobileSheets CSV erstellen..."
echo "--------------------------------------"

# Erstelle Output-Verzeichnis
mkdir -p "$OUTPUT_DIR"

python3 forScore2MS.py \
    "$BASENAME/$BASENAME-cleaned.json" \
    -o "$OUTPUT_DIR/mobilesheets_import.csv" \
    --pdf-list "$OUTPUT_DIR/pdf_files.txt"

echo "✓ CSV-Datei erstellt"
echo ""

# Schritt 3: Zusammenfassung
echo "========================================="
echo "Konvertierung abgeschlossen!"
echo "========================================="
echo ""
echo "Ausgabe-Dateien:"
echo "  - $OUTPUT_DIR/mobilesheets_import.csv"
echo "  - $OUTPUT_DIR/pdf_files.txt"
echo ""
echo "Nächste Schritte:"
echo "  1. Kopieren Sie alle PDFs aus pdf_files.txt in ein Verzeichnis"
echo "  2. Übertragen Sie PDFs und CSV auf Ihr MobileSheets-Gerät"
echo "  3. Importieren Sie die CSV in MobileSheets"
echo ""
echo "Siehe README_MOBILESHEETS_IMPORT.md für detaillierte Anweisungen."
echo ""
