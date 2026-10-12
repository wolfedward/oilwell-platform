import sqlite3
import tempfile
from pathlib import Path
import unittest
from tests.preview_support import make_source


class DatasetTests(unittest.TestCase):
    def test_read_helpers_and_connection_cannot_write_or_attach(self):
        from backend.preview.dataset import PreviewDataset
        with tempfile.TemporaryDirectory() as temp:
            path = make_source(Path(temp))
            dataset = PreviewDataset(path)
            self.assertEqual(dataset.scalar('SELECT count(*) FROM wells'),2)
            self.assertEqual(dataset.query_one('SELECT name FROM wells WHERE id=?',('W1',))['name'],'一号井')
            for sql in ["INSERT INTO units(id,name) VALUES('x','x')", "UPDATE wells SET name='x'",'DELETE FROM wells',
                        "ATTACH DATABASE ':memory:' AS other",'PRAGMA query_only=OFF','PRAGMA journal_mode=WAL']:
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    dataset.query_all(sql)
            self.assertFalse((path.parent/'attached.db').exists())
