"""Regression for GRANT/catalog-lock versus ANALYZE/TRUNCATE inversion."""
import os
import threading
import time
import unittest
from unittest.mock import patch

import psycopg


class FixtureLifecycleTests(unittest.TestCase):
    def exercise(self, fixture_type, module):
        fixture = fixture_type()
        original = module.grant_roles
        workers, connections, errors = [], [], []

        def grant_while_analyzing(connection, **kwargs):
            settings = fixture.settings['migrator']
            analyzer = psycopg.connect(**settings)
            connections.append(analyzer)
            self.assertEqual(analyzer.execute('SELECT current_database()').fetchone()[0], settings['dbname'])
            analyzer.execute("SET statement_timeout='6s'")
            schema = 'device_registry' if settings['dbname'] == 'oilwell_registry_test' else 'ingest_reliability'
            table = psycopg.sql.Identifier(schema, 'operation_receipts')
            analyzer.execute(psycopg.sql.SQL('LOCK {} IN SHARE UPDATE EXCLUSIVE MODE').format(table))
            original(connection, **kwargs)
            connection.execute("SET statement_timeout='6s'")

            def analyze():
                try:
                    analyzer.execute(psycopg.sql.SQL('ANALYZE {}').format(table))
                    analyzer.commit()
                except Exception as error:
                    errors.append(error)
                    analyzer.rollback()
            worker = threading.Thread(target=analyze)
            workers.append(worker)
            worker.start()
            with psycopg.connect(**settings, autocommit=True) as watcher:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    row = watcher.execute('SELECT wait_event FROM pg_stat_activity WHERE pid=%s',
                                          (analyzer.info.backend_pid,)).fetchone()
                    if row == ('transactionid',):
                        return
                    time.sleep(.005)
                self.fail('Controlled ANALYZE did not reach the catalog transaction wait')

        try:
            with patch.object(module, 'grant_roles', side_effect=grant_while_analyzing):
                fixture.setUp()
        finally:
            fixture.doCleanups()
            for worker in workers:
                worker.join(8)
                self.assertFalse(worker.is_alive(), 'ANALYZE must not outlive test cleanup')
            for connection in connections:
                connection.close()
        self.assertEqual(errors, [], 'Fixture must commit grants before requesting TRUNCATE locks')

    @unittest.skipUnless(os.environ.get('OILWELL_REGISTRY_TEST_CONFIG'), 'Requires isolated registry DB')
    def test_registry_reset_does_not_deadlock_with_analyze(self):
        from tests.registry_support import RegistryDatabaseCase
        from backend.registry import migrate
        self.exercise(RegistryDatabaseCase, migrate)

    def test_ingest_reset_does_not_deadlock_with_analyze(self):
        from tests.ingest_support import IngestDatabaseCase
        from backend.ingest_reliability import migrate
        self.exercise(IngestDatabaseCase, migrate)
