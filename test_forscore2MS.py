import io
import sqlite3
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import forScore2MS as converter


class PageCountImportTests(unittest.TestCase):
    def test_page_ranges_and_source_counts(self):
        db = converter.MobileSheetsDB(':memory:')
        try:
            def song(title, first_page, last_page):
                return {
                    'title': title, 'filepath': 'Book.pdf',
                    'first_page': first_page, 'last_page': last_page,
                }

            db.insert_song(song('Book', 1, -1), page_count=330)
            db.insert_song(song('Tune', 26, 28), page_count=330)
            db.insert_song(song('Single bookmark', 36, 36), page_count=330)
            db.insert_song({**song('One page', 1, -1), 'filepath': 'One.pdf'}, page_count=1)
            db.insert_song({**song('Unknown', 1, -1), 'filepath': 'Missing.pdf'})

            rows = db.cursor.execute(
                'SELECT s.Title, s.LastPage, f.PageOrder, f.SourceFilePageCount '
                'FROM Songs s JOIN Files f ON f.SongId = s.Id ORDER BY s.Id'
            ).fetchall()
            self.assertEqual(rows, [
                ('Book', 330, '1-330', 330),
                ('Tune', 0, '26,27,28', 330),
                ('Single bookmark', 0, '36', 330),
                ('One page', 1, '1', 1),
                ('Unknown', 0, '', 0),
            ])
        finally:
            db.close()

    def test_import_counts_each_pdf_once_and_keeps_missing_entries(self):
        plist = {
            'Book.pdf|bookmarks': [
                {'Title': 'Tune', 'First Page': 2, 'Last Page': 3},
            ],
            'Missing.pdf|title': 'Missing',
        }
        with tempfile.TemporaryDirectory() as output_dir:
            with (patch.object(converter, 'validate_input'),
                  patch.object(converter, 'read_plist_from_4sb', return_value=plist),
                  patch.object(converter, 'get_pdf_page_count',
                               side_effect=lambda path: 4 if path.name == 'Book.pdf' else 0) as count,
                  patch('sys.argv', ['forScore2MS.py', 'archive.4sb', '-o', output_dir,
                                     '--pdf-dir', output_dir]),
                  redirect_stdout(io.StringIO()) as output):
                converter.main()

            with sqlite3.connect(Path(output_dir) / 'mobilesheets.db') as connection:
                rows = connection.execute(
                    'SELECT s.Title, f.PageOrder, f.SourceFilePageCount, s.LastPage '
                    'FROM Songs s JOIN Files f ON f.SongId = s.Id ORDER BY s.Title'
                ).fetchall()
            self.assertEqual(rows, [
                ('Book', '1-4', 4, 4),
                ('Missing', '', 0, 0),
                ('Tune', '2,3', 4, 0),
            ])
            self.assertEqual(count.call_count, 2)
            self.assertIn('Missing.pdf (file missing)', output.getvalue())

    def test_default_pdf_directory_is_used(self):
        with tempfile.TemporaryDirectory() as output_dir:
            archive = Path(output_dir) / 'Archiv 2026.4sb'
            database_dir = Path(output_dir) / 'result'
            with (patch.object(converter, 'validate_input'),
                  patch.object(converter, 'read_plist_from_4sb',
                               return_value={'Book.pdf|title': 'Book'}),
                  patch.object(converter, 'get_pdf_page_count', return_value=7) as count,
                  patch('sys.argv', ['forScore2MS.py', str(archive), '-o', str(database_dir)]),
                  redirect_stdout(io.StringIO())):
                converter.main()

            count.assert_called_once_with(archive.resolve().with_suffix('') / 'files' / 'Book.pdf')
            with sqlite3.connect(database_dir / 'mobilesheets.db') as connection:
                row = connection.execute(
                    'SELECT PageOrder, SourceFilePageCount FROM Files'
                ).fetchone()
            self.assertEqual(row, ('1-7', 7))

    def test_missing_default_pdf_directory_warns_and_preserves_songs(self):
        with tempfile.TemporaryDirectory() as output_dir:
            archive = Path(output_dir) / 'archive.4sb'
            with (patch.object(converter, 'validate_input'),
                  patch.object(converter, 'read_plist_from_4sb',
                               return_value={'Book.pdf|title': 'Book'}),
                  patch('sys.argv', ['forScore2MS.py', str(archive), '-o', output_dir]),
                  redirect_stdout(io.StringIO()) as output):
                converter.main()

            with sqlite3.connect(Path(output_dir) / 'mobilesheets.db') as connection:
                row = connection.execute('SELECT Path, PageOrder FROM Files').fetchone()
            self.assertEqual(row, ('Book.pdf', ''))
            self.assertIn('PDF directory not found:', output.getvalue())
            self.assertIn('Book.pdf (file missing)', output.getvalue())


class LibraryCollectionImportTests(unittest.TestCase):
    def test_library_memberships_become_collections_for_all_pdf_entries(self):
        plist = {
            'Book.pdf|bookmarks': [
                {'Title': 'Tune'},
            ],
            'Book.pdf|libraries': 'MSF-Big-Band, Jazzlight',
            'Solo.pdf|title': 'Solo score',
            'Solo.pdf|libraries': 'Übungsmaterial',
        }
        with tempfile.TemporaryDirectory() as output_dir:
            with (patch.object(converter, 'validate_input'),
                  patch.object(converter, 'read_plist_from_4sb', return_value=plist),
                  patch.object(converter, 'get_pdf_page_count', return_value=0),
                  patch('sys.argv', ['forScore2MS.py', 'archive.4sb', '-o', output_dir,
                                     '--pdf-dir', output_dir]),
                  redirect_stdout(io.StringIO())):
                converter.main()

            with sqlite3.connect(Path(output_dir) / 'mobilesheets.db') as connection:
                rows = connection.execute(
                    'SELECT c.Name, s.Title FROM Collections c '
                    'JOIN CollectionSong cs ON cs.CollectionId = c.Id '
                    'JOIN Songs s ON s.Id = cs.SongId ORDER BY c.Name, s.Title'
                ).fetchall()

        self.assertEqual(rows, [
            ('Jazzlight', 'Book'),
            ('Jazzlight', 'Tune'),
            ('MSF-Big-Band', 'Book'),
            ('MSF-Big-Band', 'Tune'),
            ('Übungsmaterial', 'Solo score'),
        ])


class TextAnnotationImportTests(unittest.TestCase):
    def test_database_schema_matches_mobilesheets_version_63(self):
        db = converter.MobileSheetsDB(':memory:')
        try:
            version = db.cursor.execute('PRAGMA user_version').fetchone()[0]
            textbox_columns = {
                row[1] for row in db.cursor.execute('PRAGMA table_info(TextboxAnnotations)')
            }
            link_columns = {
                row[1] for row in db.cursor.execute('PRAGMA table_info(Links)')
            }
        finally:
            db.close()

        self.assertEqual(version, 63)
        self.assertIn('LineSpacing', textbox_columns)
        self.assertIn('Zoom', link_columns)
        self.assertIn('ZoomEnd', link_columns)

    def test_pdf_page_size_prefers_pdf_points_over_spotlight_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / 'score.pdf'
            pdf_path.touch()
            with patch.object(
                    converter.subprocess, 'run',
                    return_value=type('Result', (), {
                        'returncode': 0,
                        'stdout': 'Page size: 595 x 842 pts (A4)\n',
                    })()) as run:
                self.assertEqual(converter.get_pdf_page_size(pdf_path), (595, 842))
            run.assert_called_once()

    def test_extracts_visible_text_annotations_and_writes_mobile_sheets_rows(self):
        plist = {
            'Score.pdf|1|textAnnotations': [
                {
                    'text': 'Thema komplett nach Solo!\nSolo: ts, tp, p', 'origin.x': 0.1,
                    'origin.y': 0.2, 'size.x': 200, 'size.y': 35,
                    'fontSize': 16, 'fontColor': 'Red', 'layerVisible': 1,
                },
                {'text': 'hidden', 'layerVisible': 0},
                {'text': '', 'layerVisible': 1},
            ],
        }
        annotations = converter.extract_text_annotations(plist)
        self.assertEqual(len(annotations['Score.pdf']), 1)

        db = converter.MobileSheetsDB(':memory:')
        try:
            song_id = db.insert_song({
                'title': 'Score', 'filepath': 'Score.pdf',
                'first_page': 1, 'last_page': -1,
            }, page_count=2)
            db.add_text_annotation(song_id, 0, annotations['Score.pdf'][0][1], (528, 759))
            base = db.cursor.execute(
                'SELECT SongId, Page, Type, Opacity, SourcePageWidth, SourcePageHeight '
                'FROM AnnotationsBase'
            ).fetchone()
            textbox = db.cursor.execute(
                'SELECT TextColor, Text, FontSize, TextAlign, LineSpacing, '
                'typeof(LineSpacing) '
                'FROM TextboxAnnotations'
            ).fetchone()
            layers = db.cursor.execute(
                'SELECT SongId, Page, LayerIndex, Name, Visible FROM Layers'
            ).fetchall()
            points, count = db.cursor.execute(
                'SELECT Points, Count FROM AnnotationPoints'
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(base, (song_id, 0, 0, 255, 528, 759))
        self.assertEqual(
            textbox,
            (0xFF0000, 'Thema komplett nach Solo!\nSolo: ts, tp, p', 16, 0, 1.0, 'real'),
        )
        self.assertEqual(layers, [(song_id, 0, 0, 'Ebene 1', 1)])
        self.assertEqual(count, 6)
        for actual, expected in zip(
                struct.unpack('<6d', points),
            (52.8, 151.8, 252.8, 186.8, 57.8, 167.8)):
            self.assertAlmostEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()