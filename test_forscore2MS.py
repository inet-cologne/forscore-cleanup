import io
import sqlite3
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


if __name__ == '__main__':
    unittest.main()