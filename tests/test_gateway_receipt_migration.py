"""Explicit offline queue upgrade and exact-byte retention in owned temp files."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import importlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.gateway.spool import DurableSpool
from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_vectors import observation


class SpoolMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ingest-spool-v2-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'queue.db'
        self.backup = Path(temporary.name) / 'queue-before-v2.db'
        self.module = importlib.import_module('backend.gateway.reliable_spool')
        self.migrate = importlib.import_module('backend.gateway.spool_migrate')
        self.limits = self.module.SpoolLimits(100, 1024 * 1024, 1024 * 1024)

    def old_rows(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute('SELECT message_id,topic,payload,qos,state,attempts,next_attempt_at,'
                                      'claimed_at,created_at,acked_at,last_error FROM uplink_spool ORDER BY message_id').fetchall()

    def old(self, **kwargs):
        spool = DurableSpool.__new__(DurableSpool)
        self.addCleanup(spool.close)
        DurableSpool.__init__(spool, self.path, **kwargs)
        return spool

    def initialize(self, limits=None):
        self.migrate.initialize_spool(self.path, limits=limits or self.limits)

    def open(self, limits=None):
        spool = self.module.ReliableSpool(self.path, limits=limits or self.limits)
        self.addCleanup(spool.close)
        return spool

    def test_migration_keeps_broker_confirmation_meaning(self):
        obs = observation()
        old = self.old()
        old.enqueue('M-1', obs.topic, obs.payload, now=10)
        old.claim(now=10, limit=1)
        old.ack('M-1', now=11)
        old.close()
        before = self.old_rows()
        result = self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(result['version'], 2)
        row = self.open().get('M-1')
        self.assertIsNotNone(row)
        self.assertEqual((row['confirmation_mode'], row['state']), ('broker', 'legacy_broker_confirmed'))
        self.assertEqual(row['payload'], obs.payload)
        self.assertEqual(row['legacy_state'], 'acked')
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(row['receipt_id'])
        with closing(sqlite3.connect(self.backup)) as backup, backup:
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(backup.execute('SELECT * FROM uplink_spool ORDER BY message_id').fetchall(), before)

    def test_legacy_pending_inflight_dead_and_arbitrary_bytes_are_preserved(self):
        old = self.old(max_attempts=1)
        for identifier, payload in [('A', b'\x00\xffraw'), ('B', b'{ "x": 1 }'), ('C', b'pending')]:
            old.enqueue(identifier, 'legacy/topic', payload, now=100)
        old.claim(now=100, limit=1)
        old.fail('A', 'old field failure', now=101)
        old.claim(now=101, limit=1)
        old.close()
        before = {row[0]: row for row in self.old_rows()}
        self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        spool = self.open()
        for identifier, state, legacy in [('A', 'dead', 'dead'), ('B', 'pending', 'inflight'), ('C', 'pending', 'pending')]:
            row = spool.get(identifier)
            self.assertIsNotNone(row)
            self.assertEqual((row['state'], row['legacy_state'], row['confirmation_mode']), (state, legacy, 'broker'))
            self.assertEqual((row['payload'], row['attempts'], row['next_attempt_at']),
                             (before[identifier][2], before[identifier][5], before[identifier][6]))
        self.assertEqual(spool.get('A')['legacy_error'], 'old field failure')
        self.assertIsNone(spool.get('B')['lease_token'])

    def test_runtime_never_creates_or_upgrades_a_database(self):
        with self.assertRaises(ReliabilityError):
            self.open()
        self.assertFalse(self.path.exists())
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        before = self.old_rows()
        with self.assertRaises(ReliabilityError):
            self.open()
        self.assertEqual(self.old_rows(), before)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)

    def test_existing_backup_is_never_overwritten(self):
        self.old().close()
        self.backup.write_bytes(b'irreplaceable backup')
        with self.assertRaises(ReliabilityError):
            self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.backup.read_bytes(), b'irreplaceable backup')
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)

    def test_initialize_refuses_existing_database_instead_of_erasing_it(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        with self.assertRaises(ReliabilityError):
            self.initialize()
        self.assertEqual(len(self.old_rows()), 1)

    def test_migration_budget_failure_preserves_original_and_backup(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'12345')
        old.close()
        before = self.old_rows()
        with self.assertRaises(ReliabilityError):
            self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=replace(self.limits, max_payload_bytes=4))
        self.assertEqual(self.old_rows(), before)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)

    def test_old_constructor_and_unmodified_old_sql_cannot_write_v2(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        with self.assertRaises((ReliabilityError, sqlite3.Error, ValueError)):
            self.old()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT type FROM sqlite_master WHERE name='uplink_spool'").fetchone()[0], 'view')
            for statement in ("INSERT INTO uplink_spool(message_id,topic,payload,qos,state,next_attempt_at,created_at) VALUES('X','t',X'00',1,'pending',0,0)",
                              "UPDATE uplink_spool SET state='acked'", "DELETE FROM uplink_spool",
                              'CREATE INDEX IF NOT EXISTS uplink_spool_ready_idx ON uplink_spool(state,next_attempt_at,created_at)'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                    connection.execute(statement)
        self.assertEqual(self.open().get('M-1')['payload'], b'raw')

    def test_exact_reenqueue_is_idempotent_but_topic_payload_qos_and_mode_conflict(self):
        self.initialize()
        spool = self.open()
        self.assertTrue(spool.enqueue('M-1', 'oilfield/v1/GW/D/telemetry', b'{"a":1}', confirmation_mode='application', now=10))
        self.assertFalse(spool.enqueue('M-1', 'oilfield/v1/GW/D/telemetry', b'{"a":1}', confirmation_mode='application', now=20))
        for topic, payload, mode, qos in [('other/topic', b'{"a":1}', 'application', 1),
                                         ('oilfield/v1/GW/D/telemetry', b'{ "a": 1 }', 'application', 1),
                                         ('oilfield/v1/GW/D/telemetry', b'{"a":1}', 'broker', 1),
                                         ('oilfield/v1/GW/D/telemetry', b'{"a":1}', 'application', 2)]:
            with self.subTest(topic=topic, payload=payload, mode=mode, qos=qos), self.assertRaises(ReliabilityError):
                spool.enqueue('M-1', topic, payload, confirmation_mode=mode, qos=qos)
        self.assertEqual(spool.get('M-1')['created_at'], 10)
        self.assertEqual(spool.get('M-1')['payload'], b'{"a":1}')
        self.assertEqual(sum(spool.counts().values()), 1)

    def test_message_and_byte_budgets_are_atomic_under_thread_contention(self):
        for limits in (replace(self.limits, max_messages=1), replace(self.limits, max_payload_bytes=5)):
            with self.subTest(limits=limits):
                other = self.path.with_name(str(limits.max_messages) + '-' + str(limits.max_payload_bytes) + '.db')
                self.migrate.initialize_spool(other, limits=limits)
                spool = self.module.ReliableSpool(other, limits=limits)
                self.addCleanup(spool.close)
                def enqueue(index):
                    try:
                        return spool.enqueue('M-' + str(index), 'topic', b'12345', confirmation_mode='broker')
                    except ReliabilityError:
                        return False
                with ThreadPoolExecutor(max_workers=4) as executor:
                    result = list(executor.map(enqueue, range(8)))
                self.assertEqual(sum(result), 1)
                self.assertEqual(sum(spool.counts().values()), 1)
                with closing(sqlite3.connect(other)) as connection, connection:
                    self.assertEqual(connection.execute('SELECT used_messages,used_payload_bytes FROM spool_capacity').fetchone(), (1, 5))

    def test_open_does_not_silently_change_persisted_budgets(self):
        self.initialize()
        with self.assertRaises(ReliabilityError):
            self.open(replace(self.limits, max_messages=101))
        self.assertEqual(sum(self.open().counts().values()), 0)

    def test_new_spool_requires_wal_full_and_retains_records_across_restart(self):
        self.initialize()
        spool = self.open()
        spool.enqueue('M-1', 'topic', b'\x00\xffexact', confirmation_mode='broker')
        self.assertEqual(spool._connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(spool._connection.execute('PRAGMA synchronous').fetchone()[0], 2)
        spool.close()
        row = self.open().get('M-1')
        self.assertEqual(row['payload'], b'\x00\xffexact')
        self.assertEqual(row['state'], 'pending')

    def test_two_runtime_processes_cannot_open_the_same_queue(self):
        self.initialize()
        spool = self.open()
        code = ('from backend.gateway.reliable_spool import ReliableSpool,SpoolLimits; '
                'import sys; ReliableSpool(sys.argv[1],limits=SpoolLimits(100,1048576,1048576))')
        result = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(spool.counts().values()), 0)
        spool.close()
        self.assertEqual(sum(self.open().counts().values()), 0)

    def test_limit_types_bounds_and_unsafe_enqueue_inputs_are_rejected(self):
        for values in ((True, 5, 5), (0, 5, 5), (1, -1, 5), (1, 5, 0), (1, 2**63, 5), (1.5, 5, 5)):
            with self.subTest(values=values), self.assertRaises((ValueError, ReliabilityError)):
                self.module.SpoolLimits(*values)
        self.initialize()
        spool = self.open()
        for kwargs in ({'message_id': ''}, {'payload': 'text'}, {'topic': 't/+'}, {'topic': 'bad\x00topic'},
                       {'qos': True}, {'confirmation_mode': 'auto'}, {'now': float('nan')}, {'now': float('inf')},
                       {'payload': b'x' * (1024 * 1024 + 1)}):
            arguments = {'message_id': 'M-1', 'topic': 'topic', 'payload': b'raw', 'confirmation_mode': 'application'} | kwargs
            with self.subTest(fields=tuple(kwargs)), self.assertRaises(ReliabilityError):
                spool.enqueue(**arguments)
        self.assertEqual(sum(spool.counts().values()), 0)

    def test_backup_includes_committed_wal_after_an_unclean_process_exit(self):
        code = ('from backend.gateway.spool import DurableSpool; import os,sys; '
                's=DurableSpool(sys.argv[1]); s._connection.execute("PRAGMA wal_autocheckpoint=0"); '
                's.enqueue("WAL", "topic", b"committed-only-in-wal"); os._exit(0)')
        result = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertGreater(Path(str(self.path) + '-wal').stat().st_size, 0)
        self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.open().get('WAL')['payload'], b'committed-only-in-wal')
        with closing(sqlite3.connect(self.backup)) as backup, backup:
            self.assertEqual(backup.execute("SELECT payload FROM uplink_spool WHERE message_id='WAL'").fetchone()[0], b'committed-only-in-wal')

    def test_ddl_failure_rolls_back_copy_and_keeps_consistent_old_backup(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        before = self.old_rows()
        original_connect = sqlite3.connect
        class DeniedDrop(sqlite3.Connection):
            def execute(self, statement, parameters=()):
                if statement.strip().upper().startswith('DROP TABLE UPLINK_SPOOL'):
                    raise sqlite3.OperationalError('injected drop failure')
                return super().execute(statement, parameters)
        with patch.object(self.migrate.sqlite3, 'connect', side_effect=lambda *args, **kwargs: original_connect(*args, factory=DeniedDrop, **kwargs)):
            with self.assertRaises(ReliabilityError):
                self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.old_rows(), before)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)
        with closing(sqlite3.connect(self.backup)) as backup, backup:
            self.assertEqual(backup.execute('SELECT * FROM uplink_spool ORDER BY message_id').fetchall(), before)

    def test_insufficient_disk_refuses_new_rows_without_overwriting_old_rows(self):
        from collections import namedtuple
        self.initialize()
        spool = self.open()
        spool.enqueue('M-1', 'topic', b'raw', confirmation_mode='broker')
        Usage = namedtuple('Usage', 'total used free')
        with patch.object(self.module.shutil, 'disk_usage', return_value=Usage(100, 100, 0)):
            with self.assertRaises(ReliabilityError):
                spool.enqueue('M-2', 'topic', b'new', confirmation_mode='broker')
            self.assertFalse(spool.enqueue('M-1', 'topic', b'raw', confirmation_mode='broker'))
        self.assertEqual(sum(spool.counts().values()), 1)
        self.assertEqual(spool.get('M-1')['payload'], b'raw')

    def test_unknown_version_or_missing_schema_is_not_repaired_by_runtime(self):
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('PRAGMA user_version=3')
        with self.assertRaises(ReliabilityError):
            self.open()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)
            connection.execute('PRAGMA user_version=2')
            connection.execute('DROP TABLE spool_security_events')
        with self.assertRaises(ReliabilityError):
            self.open()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='spool_security_events'").fetchone())

    def test_failed_enqueue_commit_rolls_back_both_budget_and_original_bytes(self):
        self.initialize()
        original_connect = sqlite3.connect
        class DeniedCommit(sqlite3.Connection):
            reject = False
            def commit(self):
                if self.reject:
                    raise sqlite3.OperationalError('injected commit failure')
                return super().commit()
        with patch.object(self.module.sqlite3, 'connect', side_effect=lambda *args, **kwargs: original_connect(*args, factory=DeniedCommit, **kwargs)):
            spool = self.open()
        spool._connection.reject = True
        with self.assertRaises(ReliabilityError):
            spool.enqueue('M-1', 'topic', b'raw', confirmation_mode='broker')
        self.assertEqual(sum(spool.counts().values()), 0)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT used_messages,used_payload_bytes FROM spool_capacity').fetchone(), (0, 0))

    def test_cli_initialization_requires_explicit_flag_and_never_overwrites(self):
        command = [sys.executable, '-m', 'backend.gateway.spool_migrate', '--database', str(self.path),
                   '--max-messages', '100', '--max-payload-bytes', '1048576', '--min-free-bytes', '1048576']
        refused = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(self.path.exists())
        initialized = subprocess.run(command + ['--initialize'], capture_output=True, text=True, timeout=10)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertEqual(self.migrate.read_spool_version(self.path), 2)
        again = subprocess.run(command + ['--initialize'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(again.returncode, 0)

    def test_read_version_does_not_create_missing_file_or_promote_empty_database(self):
        self.assertEqual(self.migrate.read_spool_version(self.path), 0)
        self.assertFalse(self.path.exists())
        with closing(sqlite3.connect(self.path)):
            pass
        with self.assertRaises(ReliabilityError):
            self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.migrate.read_spool_version(self.path), 0)

    def test_runtime_closed_access_and_extreme_clock_are_controlled_failures(self):
        self.initialize()
        spool = self.open()
        with self.assertRaises(ReliabilityError):
            spool.enqueue('M-1', 'topic', b'raw', confirmation_mode='broker', now=10**10000)
        spool.close()
        for operation in (lambda: spool.get('M-1'), spool.counts,
                          lambda: spool.enqueue('M-1', 'topic', b'raw', confirmation_mode='broker')):
            with self.assertRaises(ReliabilityError):
                operation()

    def test_queue_identity_is_protected_from_in_place_rewrites(self):
        self.initialize()
        spool = self.open()
        spool.enqueue('M-1', 'topic', b'raw', confirmation_mode='broker')
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for statement in ("UPDATE uplink_queue_v2 SET payload=X'00'", "UPDATE uplink_queue_v2 SET topic='different'",
                              "UPDATE uplink_queue_v2 SET confirmation_mode='application'", 'DELETE FROM uplink_queue_v2'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                    connection.execute(statement)
        self.assertEqual(spool.get('M-1')['payload'], b'raw')

    def test_running_legacy_queue_blocks_migration_until_it_is_closed(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        with self.assertRaises(ReliabilityError):
            self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)
        self.assertFalse(self.backup.exists())
        old.close()
        self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.open().get('M-1')['payload'], b'raw')

    def test_old_constructor_refuses_unknown_future_version_without_creating_table(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('PRAGMA user_version=42')
        with self.assertRaises(ReliabilityError):
            self.old()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='uplink_spool'").fetchone())

    def test_backup_cannot_use_a_source_database_sidecar_path(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        with self.assertRaises(ReliabilityError):
            self.migrate.migrate_spool(self.path, backup_path=Path(str(self.path) + '-journal'), limits=self.limits)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)

    def test_initialize_preserves_orphan_wal_instead_of_creating_a_new_database_over_it(self):
        orphan = Path(str(self.path) + '-wal')
        orphan.write_bytes(b'needs-operator-recovery')
        with self.assertRaises(ReliabilityError):
            self.initialize()
        self.assertFalse(self.path.exists())
        self.assertEqual(orphan.read_bytes(), b'needs-operator-recovery')

    def test_backup_byte_mismatch_stops_before_source_schema_change(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        original_connect = sqlite3.connect
        class CorruptBackup(sqlite3.Connection):
            def backup(self, target, **kwargs):
                super().backup(target, **kwargs)
                target.execute("UPDATE uplink_spool SET payload=X'00' WHERE message_id='M-1'")
                target.commit()
        with patch.object(self.migrate.sqlite3, 'connect', side_effect=lambda *args, **kwargs: original_connect(*args, factory=CorruptBackup, **kwargs)):
            with self.assertRaises(ReliabilityError):
                self.migrate.migrate_spool(self.path, backup_path=self.backup, limits=self.limits)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)
        self.assertEqual(self.old_rows()[0][2], b'raw')

    def test_process_death_after_old_table_drop_recovers_original_and_complete_backup(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'crash-survivor')
        old.close()
        code = '''import os, sqlite3, sys
from backend.gateway import spool_migrate as m
from backend.gateway.reliable_spool import SpoolLimits
original = sqlite3.connect
class Interrupted(sqlite3.Connection):
    def execute(self, statement, parameters=()):
        result = super().execute(statement, parameters)
        if statement == 'DROP TABLE uplink_spool':
            os._exit(19)
        return result
m.sqlite3.connect = lambda *args, **kwargs: original(*args, factory=Interrupted, **kwargs)
m.migrate_spool(sys.argv[1], backup_path=sys.argv[2], limits=SpoolLimits(100,1048576,1048576))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.path), str(self.backup)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)
        self.assertEqual(self.old_rows()[0][2], b'crash-survivor')
        with closing(sqlite3.connect(self.backup)) as backup:
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(backup.execute('SELECT payload FROM uplink_spool').fetchone()[0], b'crash-survivor')
        self.migrate.migrate_spool(self.path, backup_path=self.backup.with_name('second-attempt.db'), limits=self.limits)
        self.assertEqual(self.open().get('M-1')['payload'], b'crash-survivor')

    def test_uncooperative_active_sqlite_writer_blocks_migration_before_backup(self):
        old = self.old()
        old.enqueue('M-1', 'topic', b'raw')
        old.close()
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            try:
                result = subprocess.run([sys.executable, '-m', 'backend.gateway.spool_migrate', '--database', str(self.path),
                                         '--backup', str(self.backup), '--max-messages', '100', '--max-payload-bytes', '1048576',
                                         '--min-free-bytes', '1048576'], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.backup.exists())
            finally:
                writer.rollback()
        self.assertEqual(self.migrate.read_spool_version(self.path), 1)
        self.assertEqual(self.old_rows()[0][2], b'raw')

    def test_security_event_identity_keeps_distinct_topic_and_payload_digests(self):
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for digest in ('a' * 64, 'b' * 64):
                failure = None
                try:
                    connection.execute('INSERT INTO spool_security_events VALUES(?,1,100,100,?,?)',
                                       ('receipt.invalid', 'c' * 64, digest))
                except sqlite3.Error as error:
                    failure = type(error).__name__
                self.assertIsNone(failure, 'Different invalid receipt digests must not collapse into the same event key')
            self.assertEqual(connection.execute('SELECT count(*) FROM spool_security_events').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT security_overflow_count FROM spool_capacity').fetchone()[0], 0)
