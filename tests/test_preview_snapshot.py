"""Snapshots retain committed business history and exclude authentication material."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from tests.preview_support import make_source, read_source_rows, find_secrets_in_served_snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = make_source(self.root)

    def test_wal_commits_preserved_with_exact_points_and_no_source_writes(self):
        from backend.preview.snapshot import capture_snapshot, build_business_snapshot, verify_manifest
        writer = sqlite3.connect(self.source)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE wells SET name='已提交未检查点' WHERE id='W1'")
        writer.commit()
        before = read_source_rows(self.source)
        raw = capture_snapshot(self.source, self.root/'private')
        business = build_business_snapshot(Path(raw['path']), self.root/'business.db')
        self.assertEqual(raw['integrity'], 'ok')
        self.assertEqual(raw['foreign_key_violations'], [])
        self.assertEqual(read_source_rows(self.source), before)
        rows = read_source_rows(Path(business['path']))
        self.assertEqual(rows['dynagraphs'], before['dynagraphs'])
        self.assertEqual(rows['production_daily'], before['production_daily'])
        self.assertEqual(rows['wells'][0]['name'], '已提交未检查点')
        self.assertEqual(find_secrets_in_served_snapshot(business), [])
        self.assertNotIn('sessions', rows)
        self.assertEqual(rows['users'][0]['username'], 'preview-actor-1')
        self.assertEqual(business['data_to'], '2026-09-12')
        self.assertEqual(verify_manifest(self.root/'business.db')['integrity'], 'ok')

    def test_target_and_unknown_schema_fail_closed(self):
        from backend.preview.snapshot import capture_snapshot, build_business_snapshot
        raw = capture_snapshot(self.source,self.root/'private')
        with self.assertRaises(FileExistsError):
            capture_snapshot(self.source,self.root/'private')
        with closing(sqlite3.connect(raw['path'])) as db, db:
            db.execute('ALTER TABLE wells ADD COLUMN api_secret TEXT')
        with self.assertRaisesRegex(ValueError,'未审核'):
            build_business_snapshot(Path(raw['path']), self.root/'business.db')
        self.assertFalse((self.root/'business.db').exists())

    def test_corrupt_references_and_tampered_snapshot_rejected(self):
        from backend.preview.snapshot import capture_snapshot, build_business_snapshot, verify_manifest
        raw = capture_snapshot(self.source,self.root/'private')
        business = build_business_snapshot(Path(raw['path']), self.root/'business.db')
        with closing(sqlite3.connect(business['path'])) as db, db:
            db.execute("UPDATE wells SET name='tampered'")
        with self.assertRaisesRegex(ValueError,'摘要'):
            verify_manifest(self.root/'business.db')
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE wells SET group_id='MISSING'")
        with self.assertRaisesRegex(ValueError,'外键'):
            capture_snapshot(self.source,self.root/'bad-private')

    def test_concurrent_append_snapshot_is_consistent(self):
        from backend.preview.snapshot import capture_snapshot
        stop = threading.Event()
        ready = threading.Event()
        def append():
            with closing(sqlite3.connect(self.source)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                ready.set()
                for i in range(150):
                    if stop.is_set(): break
                    db.execute("INSERT INTO device_events(device_id,event_type,detail) VALUES('D1','test',?)",(str(i),))
                    db.commit()
        thread = threading.Thread(target=append)
        thread.start(); ready.wait(5)
        try:
            raw = capture_snapshot(self.source,self.root/'private')
            self.assertEqual(raw['integrity'],'ok')
            self.assertEqual(raw['foreign_key_violations'],[])
        finally:
            stop.set(); thread.join(5)

    def test_sensitive_json_keys_redacted_without_changing_business_points(self):
        from backend.preview.snapshot import capture_snapshot, build_business_snapshot
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE raw_messages SET payload=?',(json.dumps({'power_kw':9.5,'nested':{'password':'TEST-SECRET-HASH'}}),))
        raw = capture_snapshot(self.source,self.root/'private')
        result = build_business_snapshot(Path(raw['path']),self.root/'business.db')
        payload = json.loads(read_source_rows(Path(result['path']))['raw_messages'][0]['payload'])
        self.assertEqual(payload['power_kw'],9.5)
        self.assertEqual(payload['nested']['password'],'[redacted]')


if __name__ == '__main__': unittest.main()
