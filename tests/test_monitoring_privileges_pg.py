"""Actual PostgreSQL permissions in a newly owned disposable cluster only."""
import unittest
import psycopg


class MonitoringPrivilegeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.monitoring_support import MonitoringDatabase
        cls.db = MonitoringDatabase()
        cls.db.__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)

    def test_reader_can_query_metadata_but_cannot_write_or_read_raw(self):
        from backend.monitoring.privileges import probe_reader
        probe_reader(lambda: self.db.connect('monitoring_reader'))
        with self.db.connect('monitoring_reader') as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM device_registry.wells').fetchone()[0], 0)
            conn.execute('SELECT message_id FROM public.ingest_messages LIMIT 1')
            conn.execute('SELECT * FROM ingest_reliability.case_metadata LIMIT 1')
        for statement in ('SELECT raw_payload FROM public.ingest_messages LIMIT 1',
                          'SELECT raw_payload FROM ingest_reliability.facts LIMIT 1',
                          'DELETE FROM device_registry.wells',
                          'CREATE TABLE public.unsafe_table(id int)'):
            with self.subTest(statement=statement), self.db.connect('monitoring_reader') as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)

    def test_reader_rejects_excess_privileges(self):
        from backend.monitoring.privileges import probe_reader
        from backend.monitoring.errors import MonitoringError
        with self.db.connect('bootstrap') as conn:
            conn.execute('GRANT INSERT ON device_registry.wells TO review_monitoring_reader')
        try:
            with self.assertRaises(MonitoringError) as caught:
                probe_reader(lambda: self.db.connect('monitoring_reader'))
            self.assertEqual(caught.exception.code, 'monitoring.privilege_unsafe')
        finally:
            with self.db.connect('bootstrap') as conn:
                conn.execute('REVOKE INSERT ON device_registry.wells FROM review_monitoring_reader')
        probe_reader(lambda: self.db.connect('monitoring_reader'))

    def test_fixture_detects_wrong_database_identity_before_returning_connection(self):
        original = self.db.system_id
        self.db.system_id = 'not-this-cluster'
        try:
            with self.assertRaises(ValueError): self.db.connect('monitoring_reader')
        finally:
            self.db.system_id = original

    def test_probe_rejects_missing_schema_usage_and_privilege_escalation_paths(self):
        from backend.monitoring.privileges import probe_reader
        from backend.monitoring.errors import MonitoringError
        changes = (
            ('REVOKE USAGE ON SCHEMA public FROM review_monitoring_reader',
             'GRANT USAGE ON SCHEMA public TO review_monitoring_reader'),
            ('GRANT SELECT(raw_payload) ON public.ingest_messages TO review_monitoring_reader',
             'REVOKE SELECT(raw_payload) ON public.ingest_messages FROM review_monitoring_reader'),
            ('GRANT review_foundation_owner TO review_monitoring_reader',
             'REVOKE review_foundation_owner FROM review_monitoring_reader'),
        )
        for change, restore in changes:
            with self.subTest(change=change):
                with self.db.connect('bootstrap') as conn: conn.execute(change)
                try:
                    with self.assertRaises(MonitoringError) as caught:
                        probe_reader(lambda: self.db.connect('monitoring_reader'))
                    self.assertEqual(caught.exception.code, 'monitoring.privilege_unsafe')
                finally:
                    with self.db.connect('bootstrap') as conn: conn.execute(restore)
                probe_reader(lambda: self.db.connect('monitoring_reader'))
