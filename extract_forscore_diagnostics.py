#!/usr/bin/env python3
"""Extrahiert eine forScore-Diagnosedatei (.4sl) in ein lesbares Verzeichnis."""
import argparse, datetime as dt, gzip, json, plistlib, shutil, sqlite3, sys, tempfile
from pathlib import Path

COCOA = dt.datetime(2001, 1, 1)
HELP_EPILOG = """\
Ausgabe: <Ordner der Eingabedatei>/jjjj-mm-tt_hh-mm_<Dateiname ohne .4sl>/
  (Zeitstempel = Änderungsdatum der Eingabedatei)
Inhalt:
  <name>.sqlite    Datenbank (WAL bereits eingearbeitet)
  <name>.sql       SQL-Dump (ohne Indizes)
  <name>.tables/   je Tabelle eine CSV-Datei (nur nicht-leere Tabellen)
  prefs.json       App-Einstellungen (&SYS;/&SYNC;)
  summary.txt      Zusammenfassung (Sync-Stand, Zeilenzahlen, Setlists, Items)
"""

def die(msg):
    print(f"Fehler: {msg}", file=sys.stderr); sys.exit(1)

def jsonable(o):
    if isinstance(o, plistlib.UID): return f"UID({o.data})"
    if isinstance(o, bytes): return f"<{len(o)} bytes> " + o[:32].hex()
    if isinstance(o, dt.datetime): return o.isoformat()
    return str(o)

def decode_zone_token(b):
    try:
        p = plistlib.loads(b)
        return {"CKServerChangeToken_hex": p["$objects"][2].hex()}
    except Exception:
        return None

def cocoa(v):
    return (COCOA + dt.timedelta(seconds=v)).strftime("%Y-%m-%d %H:%M:%S") if v else ""

def main():
    ap = argparse.ArgumentParser(
        prog="extract_forscore_diagnostics",
        description="Extrahiert forScore-Diagnosedateien (diagnostics.4sl / previous.4sl).",
        epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("datei", help="Pfad zur .4sl-Datei")
    ap.add_argument("-o", "--out", help="Zielverzeichnis für den Ausgabeordner (Standard: Ordner der Eingabedatei)")
    if len(sys.argv) == 1:
        ap.print_help(); sys.exit(0)
    a = ap.parse_args()

    src = Path(a.datei)
    if not src.is_file(): die(f"Datei nicht gefunden: {src}")
    try:
        raw = gzip.decompress(src.read_bytes())
        top = plistlib.loads(raw)
        objs = top["$objects"]
    except Exception as e:
        die(f"Keine gültige .4sl-Datei ({e})")
    blobs = [o for o in objs if isinstance(o, bytes)]
    db = next((b for b in blobs if b.startswith(b"SQLite format 3")), None)
    plists = [b for b in blobs if b.startswith(b"bplist00")]
    wal = next((b for b in blobs if b[:4] in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")), None)
    if db is None: die("Keine SQLite-Datenbank in der Datei gefunden")

    stem = src.name[:-4] if src.suffix == ".4sl" else src.stem
    ts = dt.datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d_%H-%M")
    out = Path(a.out or src.parent) / f"{ts}_{stem}"
    if out.exists(): die(f"Ausgabeverzeichnis existiert bereits: {out}")
    out.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "d.sqlite"
        tmp.write_bytes(db)
        if wal: (Path(td) / "d.sqlite-wal").write_bytes(wal)
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest = sqlite3.connect(out / f"{stem}.sqlite")
        con.backup(dest); dest.execute("PRAGMA journal_mode=DELETE"); dest.close()
        con.row_factory = sqlite3.Row

        with open(out / f"{stem}.sql", "w", encoding="utf-8") as f:
            for line in con.iterdump():
                if not line.startswith("CREATE INDEX"): f.write(line + "\n")

        tdir = out / f"{stem}.tables"; tdir.mkdir()
        import csv
        counts = {}
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        for t in tables:
            cur = con.execute(f'select * from "{t}"'); rows = cur.fetchall()
            counts[t] = len(rows)
            if not rows: continue
            with open(tdir / f"{t}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(rows[0].keys())
                for r in rows:
                    w.writerow([f"<{len(v)} bytes>" if isinstance(v, bytes) else v for v in tuple(r)])

        prefs = {}
        for b in plists:
            try:
                p = plistlib.loads(b)
                if isinstance(p, dict) and any(k.startswith("&SYS;") for k in p): prefs = p
            except Exception: pass
        if prefs:
            tok = prefs.get("&SYNC;zoneToken")
            if isinstance(tok, bytes): prefs["&SYNC;zoneToken"] = decode_zone_token(tok) or tok
            (out / "prefs.json").write_text(json.dumps(prefs, indent=2, ensure_ascii=False, default=jsonable, sort_keys=True), encoding="utf-8")

        L = [f"Quelle: {src.name}", f"Erstellt: {ts}", f"WAL enthalten: {'ja' if wal else 'nein'}", ""]
        for k in ("&SYNC;lastSyncDate", "&SYNC;syncEnabled", "&SYNC;lastSyncErrorCode",
                  "&SYS;ARUseCount", "FSAppUpdateCheckerPreference"):
            if k in prefs: L.append(f"{k}: {prefs[k]}")
        L += ["", "Zeilen je Tabelle (nicht leer):"]
        L += [f"  {t}: {n}" for t, n in counts.items() if n and not t.startswith("Z_")]
        def q(sql): return con.execute(sql).fetchall()
        L += ["", "Setlists:"] + [f"  [{r['ZMENUINDEX']}] {r['ZTITLE']}" for r in q("select * from ZSETLIST order by ZMENUINDEX")]
        L += ["", "Items:"] + [f"  {r['ZTITLE'] or r['ZPATH']} (hinzugefügt {cocoa(r['ZADDED'])})" for r in q("select * from ZITEM")]
        L += ["", "Tracks:"] + [f"  {r['ZTITLE']} ({r['ZLENGTH']:.0f}s)" for r in q("select * from ZTRACK")]
        L += ["", "Text-Annotationen:"] + [f"  Seite-PK {r['ZPAGE']}: {r['ZTEXT']!r}" for r in q("select * from ZTEXTANNOTATION")]
        L += ["", "Tabs:"] + [f"  {r['ZTITLE']} ({r['ZCATEGORY']}), Änderungen: {r['Z_OPT']}" for r in q("select * from ZTAB")]
        (out / "summary.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
        con.close()

    print(f"Extrahiert nach: {out}")

if __name__ == "__main__":
    main()
