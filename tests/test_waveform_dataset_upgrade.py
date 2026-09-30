"""Non-resetting readiness probe; committed preservation uses explicit experiment evidence."""
import unittest
from tests.waveform_dataset_support import pinned_connect


class DatasetUpgradeTests(unittest.TestCase):
    def test_dataset_storage_ready_without_fixture_reset(self):
        with pinned_connect() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            self.assertEqual(conn.execute('SELECT max(version) FROM waveforms.schema_migrations').fetchone()[0], 4)
            for table in ('dataset_drafts','dataset_revisions','dataset_events','dataset_versions','dataset_members','dataset_files','dataset_manifest_chunks','dataset_exports'):
                self.assertIsNotNone(conn.execute('SELECT to_regclass(%s)', ('waveforms.' + table,)).fetchone()[0])
                self.assertTrue(conn.execute("SELECT has_table_privilege(current_user,%s,'SELECT')", ('waveforms.' + table,)).fetchone()[0])
                self.assertFalse(conn.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE')", ('waveforms.' + table,)).fetchone()[0])
