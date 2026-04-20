# forScore .4sb File Format

This document describes the internal binary format of forScore backup and archive
files (`.4sb`). It is based on empirical analysis of real files produced by
forScore on iPadOS and is intended as a reference for tooling and future work.

---

## Overview

forScore produces two types of `.4sb` file, distinguished by version marker and
content:

| Type    | Filename prefix        | Version marker | Contains                                      |
|---------|------------------------|----------------|-----------------------------------------------|
| Backup  | `Backup`               | `4SBV02`       | Metadata only (plist)  +  drawing annotations |
| Archive | `Archiv` / `Archive`   | `4SBV03`       | Metadata (plist)  +  drawings  +  all PDFs and audio files |

Both share the same outer structure: an ASCII file header of variable length,
followed by a gzip-compressed metadata block, followed by a sequence of
embedded-file records.

---

## File Structure

```
┌────────────────────────────────────────────────────────────────┐
│  ASCII file header  (variable length, typically 74–75 bytes)   │
├────────────────────────────────────────────────────────────────┤
│  Metadata block  (gzip-compressed Apple Binary plist)          │
│  Size given in file header                                      │
├────────────────────────────────────────────────────────────────┤
│  Record 0:  ASCII record header  (variable length)             │
│             gzip-compressed file data  (compressed_size bytes) │
├────────────────────────────────────────────────────────────────┤
│  Record 1 …                                                    │
├────────────────────────────────────────────────────────────────┤
│  …                                                             │
└────────────────────────────────────────────────────────────────┘
```

---

## 1. File Header (variable length, ASCII)

The file starts with an ASCII header that ends immediately before the first gzip
magic byte sequence (`\x1f\x8b\x08`). The length depends on the locale of the
forScore app:

| Locale  | Filename in header              | Header length |
|---------|---------------------------------|---------------|
| German  | `Archiv 2026-04-12 16-00-52.4sb` | 74 bytes     |
| English | `Archive 2026-04-16 …4sb`        | 75 bytes     |

Example (German, 74 bytes):

```
<--4SBV03-->              30         3224329Archiv 2026-04-12 16-00-52.4sb
```

Example (English, 75 bytes):

```
<--4SBV03-->              31         1900869Archive 2026-04-16 16-38-32.4sb
```

Structure (fields separated by spaces, right-aligned within their columns):

| Field              | Example value                         | Description                                    |
|--------------------|---------------------------------------|------------------------------------------------|
| Version marker     | `<--4SBV03-->`                        | `4SBV02` = Backup, `4SBV03` = Archive          |
| Unknown number     | `30`                                  | Observed constant; meaning unknown             |
| Metadata size      | `3224329`                             | Byte length of the following metadata gzip block |
| Original filename  | `Archiv 2026-04-12 16-00-52.4sb`     | The filename as created by forScore            |

The metadata block starts immediately after the header (at the gzip magic byte)
and ends at offset `gzip_start + metadata_size`.

**Parsing — find gzip start dynamically:**

```python
with open(path, 'rb') as f:
    window = f.read(256)
gz_start = window.index(b'\x1f\x8b\x08')   # do not assume fixed offset
header_str = window[:gz_start].decode('ascii')
match = re.search(r'(\d{5,12})(Backup|Archiv)', header_str)
metadata_size = int(match.group(1))
metadata_end  = gz_start + metadata_size
```

---

## 2. Metadata Block (gzip + Apple Binary plist)

Bytes `gz_start` through `gz_start + metadata_size - 1` form a single gzip
stream containing an **Apple Binary Property List** (bplist00 format).

```python
import gzip, plistlib
with open(path, 'rb') as f:
    f.seek(gz_start)
    raw_gz = f.read(metadata_size)
raw_plist = gzip.decompress(raw_gz)
plist     = plistlib.loads(raw_plist)   # returns a dict
```

**Observed sizes (example file):**

| File    | Metadata gzip (compressed) | plist (decompressed) |
|---------|---------------------------|----------------------|
| Backup  | 3,224,239 bytes (~3.1 MB) | 9,040,061 bytes (~8.6 MB) |
| Archive | 3,224,329 bytes (~3.1 MB) | 9,040,061 bytes (~8.6 MB) |

Both files had **identical plist content** — Archive adds binary files on top of
the same metadata.

### 2.1 plist Key Taxonomy

The top-level plist is a flat dictionary. Keys follow several naming conventions:

#### Score metadata keys — `<filepath>|<property>`

Each PDF file gets a set of per-file metadata keys:

| Key pattern               | Value type | Description                                      |
|---------------------------|------------|--------------------------------------------------|
| `<file>|title`            | string     | Display title                                    |
| `<file>|composer`         | string     | Composer name                                    |
| `<file>|genre`            | string     | Genre / category                                 |
| `<file>|key`              | int        | Musical key (encoded as integer)                 |
| `<file>|bpm`              | int/float  | Tempo in BPM                                     |
| `<file>|signature`        | string     | Time signature                                   |
| `<file>|added`            | date/string | Date added to library                           |
| `<file>|keywords`         | string     | Custom tags                                      |
| `<file>|note`             | string     | User notes                                       |
| `<file>|reference`        | string     | External reference / link                        |
| `<file>|version`          | int        | Internal version counter                         |
| `<file>|printNumber`      | int        | Print number                                     |
| `<file>|libraries`        | list       | Library membership                               |

#### Per-page metadata keys — `<filepath>|<page>|<property>`

Some metadata is stored per page:

| Key pattern                       | Value type | Description                        |
|-----------------------------------|------------|------------------------------------|
| `<file>|<page>|croppedLandscape`  | int        | Landscape crop flag (0/1)          |
| `<file>|<page>|zoom`              | float      | Page zoom level                    |
| `<file>|<page>|offset`            | float      | Scroll offset                      |
| `<file>|<page>|trOffset`          | float      | Transpose offset                   |
| `<file>|<page>|rect`              | data/list  | Crop rectangle                     |
| `<file>|<page>|textAnnotations`   | list       | Text annotation objects (see below)|

`croppedLandscape` is the most numerous key type (~13,700 entries in a typical file).

#### Bookmark keys — `<filepath>|bookmarks`

```
'Vol. 070 - Killer Joe.pdf|bookmarks'  →  [ { bookmark_dict }, ... ]
```

Each bookmark dict contains:

| Field                    | Type   | Description                                    |
|--------------------------|--------|------------------------------------------------|
| `Title`                  | string | Bookmark/song title                            |
| `FilePath`               | string | PDF filename this bookmark belongs to          |
| `First Page`             | int    | First page (1-based)                           |
| `Last Page`              | int    | Last page (0 = single-page bookmark)           |
| `Identifier`             | string | UUID — unique per bookmark instance            |
| `Composer`               | string | Composer                                       |
| `Genre`                  | string | Genre                                          |
| `Key`                    | int    | Musical key                                    |
| `Keyword`                | string | Tags                                           |
| `kRecoverableDestination`| int    | Internal flag (always 1)                       |

**Duplicates** arise when iCloud sync creates multiple entries for the same
bookmark (same `FilePath` + `Title` + `First Page`) with different `Identifier`
UUIDs.

#### Setlist index — `&SYS;setlists`

```
'&SYS;setlists'  →  ['&SET;Session-Eifel', '&SET;Unterricht', ...]
```

An ordered list of all setlist key names. This defines the display order of
setlists in the app.

#### Setlist content keys — `&SET;<name>`

```
'&SET;2021-Weihnachtskonzert'  →  [ { setlist_entry_dict }, ... ]
```

Each setlist entry dict contains:

| Field        | Type   | Description                                          |
|--------------|--------|------------------------------------------------------|
| `Title`      | string | Display title (may differ from bookmark title)       |
| `FilePath`   | string | PDF filename                                         |
| `Identifier` | string | UUID — **independent** from bookmark `Identifier`s   |

**Important:** Setlist `Identifier` values are not the same as bookmark
`Identifier` values. They reference the *score* object (the PDF or
bookmark-as-score), not the raw bookmark entry. Deduplicating bookmarks does
**not** invalidate setlist references.

If the same song appears twice in a setlist (intentional repeat), both entries
have different `Identifier` UUIDs with the same `FilePath` + `Title`.

#### System/app settings — `&SYS;<name>`

231 keys store app-wide settings, preferences, and state, e.g.:

| Key                           | Description                              |
|-------------------------------|------------------------------------------|
| `&SYS;setlists`               | Ordered list of setlist key names        |
| `&SYS;setlistFolders`         | Setlist folder structure                 |
| `&SYS;model`                  | iPad model identifier                    |
| `&SYS;interfaceStyle`         | Light / dark mode                        |
| `&SYS;ARCurrentVersion`       | forScore version that wrote the file     |
| `&SYS;lastCurrentFilePath`    | Last open score                          |
| `&SYS;penPresets`             | Annotation pen presets                   |
| `&SYS;customColors`           | Custom color palette                     |
| `&SYS;midiShortcuts`          | MIDI controller mappings                 |
| ... (231 total)               |                                          |

#### Track/audio link keys — `<filepath>|Tracks`

Associates a PDF with linked audio tracks:

```python
{'Song Title': 'Misty_Studio_Jam_Playalong_neu.mp3',
 'Song ID': -1,
 'Song Loop B': 0.0,
 'kRecoverableDestination': 1, ...}
```

#### Blue-point link keys — `<filepath>&BLU;<page>&BLU;bluePoints`

Page-turn trigger coordinates (blue/orange dots visible in forScore):

```python
'4 Plus One (ts1) (050).pdf&BLU;2&BLU;bluePoints':
    ['0.071277&BLU;0.732899&BLU;5&ORG;0.077660&ORG;0.106678&ORG;2']
```

Values are `&BLU;`/`&ORG;` separated coordinate strings (`x&BLU;y&BLU;page`).

#### Stamp/custom-stamp keys

```
'stamps.plist'   →  [<PNG binary data>]
'stamps2.plist'  →  [<bplist binary data>]
```

User-created annotation stamps stored as binary blobs.

---

## 3. Embedded File Records

After the metadata block, both Backup and Archive files contain a sequence of
embedded files. Each record consists of:

1. An **ASCII record header** (variable length, typically 70–90 bytes)
2. A **gzip-compressed data block** of exactly `compressed_size` bytes

Records are stored **sequentially with no gaps or padding** between them. The
file ends precisely at the end of the last record (`leftover = 0`).

### 3.1 Record Header Format

The ASCII header has a fixed **32-character numeric prefix** followed immediately
by the filename:

```
<------- 32 chars -------->
              51         5378356{%DOCUMENTS_DIR%}/1-09 Have You Met Miss Jones_.mp3
              38           41096Ain't Misbehavin' - Tenor Bb.pdf|2.png
              37           163202023-12-01_Set-List-Frechen.pdf|1.png
```

| Characters | Content                                               |
|------------|-------------------------------------------------------|
| 0–13       | Spaces (14 chars of padding)                          |
| 14–31      | Two right-aligned integers separated by spaces: `<record_index>  <compressed_size>` |
| 32–end     | Filename, optionally prefixed with `{%DOCUMENTS_DIR%}/` |

**Parsing rule:** Extract `compressed_size` as the last number within characters
0–31. The filename starts at character 32, with the optional prefix stripped:

```python
NUMERIC_PREFIX_LEN = 32
numeric_part    = header_text[:32]
compressed_size = int(re.findall(r'\d+', numeric_part)[-1])
filename        = re.sub(r'^\{%DOCUMENTS_DIR%\}/', '', header_text[32:]).strip()
```

**Critical:** Using a naive "find all numbers in the whole header" approach fails
when the filename starts with digits (e.g. a date `2023-12-01_...`), because the
leading digits get absorbed into the compressed_size. The fixed 32-char split is
required.

### 3.2 Archive vs. Backup record content

| File type | Record content                                | Filename format          |
|-----------|-----------------------------------------------|--------------------------|
| Archive   | PDF, MP3, and other binary score/audio files  | `{%DOCUMENTS_DIR%}/<name>` |
| Backup    | Drawing annotations only (PNG per page)       | `<pdfname>|<page>.png`   |

**Archive** embeds all PDFs and audio files referenced in the library. Example:
```
1-09 Have You Met Miss Jones_.mp3
2022-12-17_Set-List-Frechen.pdf
```

**Backup** embeds only the drawn/handwritten annotations as PNG images, one per
annotated page. Example:
```
Ain't Misbehavin' - Tenor Bb.pdf|2.png     ← page 2 of that PDF
Morning Dance (ts1) (052).pdf|1.png
```

**Observed record counts (example):**

| File    | Records | File size |
|---------|---------|-----------|
| Archive | 5,786   | 28.1 GB   |
| Backup  | 898     | 40.5 MB   |

### 3.3 Decompression

Each gzip block decompresses to the raw file content. No multi-stream reading:

```python
with open(path, 'rb') as f:
    f.seek(gzip_start)
    compressed = f.read(compressed_size)
content = gzip.decompress(compressed)
```

**Do not use `gzip.GzipFile.read()` on a seek position** — Python 3.13+
transparently reads concatenated gzip streams, so it will attempt to decompress
the entire rest of the file.

---

## 4. Drawing Annotations and Why They Are Lost on Metadata Changes

Drawing annotations (ink strokes drawn with Apple Pencil or finger) are stored
differently from text annotations:

- In **Backup files**: stored as PNG files in the record section, named
  `<pdfname>|<page>.png`
- In **Archive files**: same PNG files are also included in the record section

The plist metadata contains **byte-offset references** that point into the
metadata binary structure. Any modification to the plist (adding, removing, or
reordering keys/values) changes the binary layout of the serialized plist, which
invalidates all stored offsets.

This is why `clean_forscore_bookmarks.py` warns that drawing annotations will be
lost: deduplicating bookmarks necessarily modifies the plist and thus invalidates
the offsets.

Text annotations (`|textAnnotations` keys) are stored entirely within the plist
as structured data (coordinate + text + font + color) and are **not** affected by
other plist changes — they survive the cleanup.

---

## 5. Restore Behavior

| Restore source  | Restores PDFs/audio? | Restores drawings? | Restores metadata? |
|-----------------|----------------------|--------------------|--------------------|
| Archive (.4sb)  | Yes (re-imports all) | Yes                | Yes                |
| Backup (.4sb)   | No (files must be present on device already) | Yes | Yes |

Restoring a **cleaned Backup** file preserves bookmarks, setlists, text
annotations, buttons, and score metadata — but loses drawings (because the
cleaned plist has different byte offsets from the original).

---

## 6. Summary of Format Constants

| Constant                  | Value  | Description                                  |
|---------------------------|--------|----------------------------------------------|
| File header size          | 74–75 bytes | Variable; ends at first gzip magic (`\x1f\x8b\x08`) |
| Version marker (Backup)   | `4SBV02` | gzip+plist only, small file               |
| Version marker (Archive)  | `4SBV03` | gzip+plist + all PDFs/audio               |
| Record header prefix size | 32     | Fixed-width numeric prefix in record headers |
| Metadata format           | gzip + Apple Binary plist (bplist00) |                     |
| Record data format        | gzip (one file per block)            |                     |
| Path placeholder          | `{%DOCUMENTS_DIR%}/` | Replaced by actual Documents dir on device |

---

## 7. Open Questions / Future Work

- What exactly is the first number (`30` in observed files) in the file header?
  Is it a format sub-version, a count, or something else?
- What is the `record_index` field in the record header (first number in the
  32-char prefix)? It appears to increment but is not always sequential — its
  exact role is unclear.
- The `&BLU;`/`&ORG;` blue-point coordinate format is not fully documented.
- Drawing offsets: if the exact binary offset mechanism in the plist were
  understood, it might be possible to update them after a metadata change,
  preserving drawings through a cleanup.
- The `kRecoverableDestination` flag (always `1`) appears in bookmarks, text
  annotations, and track entries — its semantics are unknown.
