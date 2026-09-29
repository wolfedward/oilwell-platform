"""Owned local sender files/processes and loopback broker; no center database."""
from dataclasses import replace
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.waveforms.contracts import ACK_SCHEMA, WaveReceipt
from backend.waveforms.errors import WaveformError
from backend.waveforms.files import scan_file
from tests.waveform_vectors import golden_capture
from tests.test_waveform_config import config_sections, toml_text

ROOT = Path(__file__).resolve().parents[1]
BUDGET = 16 * 1024 * 1024


def local_sections(path, *, broker=None):
    sections = config_sections()
    sections = {key.replace('gateways.GW', 'gateways.GW-001'): value for key, value in sections.items()
                if not key.startswith('postgres_') and not key.endswith(('mqtt_receive', 'mqtt_publish'))}
    sections['sender_spool'] = {'path': path.name, 'max_bytes': BUDGET}
    if broker:
        sections['gateways.GW-001.mqtt_sender'].update(port=broker.port, username='waveform-GW-001-sender')
    return sections


def await_condition(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.025)
    raise AssertionError('owned sender test condition timed out')


def stop_process(process):
    if process.poll() is None: process.kill()
    process.communicate(timeout=10)


class SenderSpoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.chunks = golden_capture()
        cls.content = b'\n'.join((cls.manifest, *cls.chunks))
        cls.scan = scan_file(BytesIO(cls.content))
        cls.key = cls.scan.manifest.key

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wp04-sender-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'wave-sender.db'

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.sender_spool'),
                             'persistent local waveform spool is required')
        from backend.waveforms import sender_spool
        return sender_spool

    def spool(self, budget=BUDGET):
        result = self.module().WaveSenderSpool.initialize(self.path, max_bytes=budget)
        self.addCleanup(result.close)
        result.bind('GW-001', 'oil')
        return result

    def receipt(self, index=None, **changes):
        chunk = json.loads(self.chunks[index - 1]) if index else None
        value = WaveReceipt(ACK_SCHEMA, uuid4(), self.key.gateway_id, self.key.device_id,
            self.key.capture_id, 'capture' if index is None else 'message', self.scan.manifest_sha256,
            None if index is None else self.scan.lines[index].sha256,
            chunk['stream_id'] if chunk else None, chunk['chunk_index'] if chunk else None,
            'complete' if index is None else 'stored', 'accepted', '2026-09-29T00:00:00.000000Z')
        return replace(value, **changes).to_bytes()

    @property
    def topic(self):
        return 'oil/v2/GW-001/PUMP-001/waveform-ack'

    def test_broker_ack_keeps_capture(self):
        spool = self.spool()
        identifier = spool.enqueue(BytesIO(self.content))
        messages = spool.prepare_batch(limit=16)
        for message in messages:
            self.assertTrue(spool.record_broker_ack(message, 0))
        self.assertEqual(spool.status()['retained_capture_bytes'], len(self.content))
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        self.assertEqual(spool.enqueue(BytesIO(self.content)), identifier)
        self.assertTrue(spool.prepare_batch(limit=16))

    def test_stored_chunk_does_not_release_file(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        for index in reversed(range(len(self.scan.lines))):
            self.assertTrue(spool.apply_receipt(self.topic, self.receipt(index)))
        self.assertEqual(spool.status()['retained_capture_bytes'], len(self.content))
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        replay = spool.prepare_batch(limit=16)
        self.assertEqual([message.payload for message in replay], [self.manifest])

    def test_capture_receipt_requires_manifest_digest(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        self.assertFalse(spool.apply_receipt('other/v2/GW-001/PUMP-001/waveform-ack', self.receipt()))
        self.assertFalse(spool.apply_receipt(self.topic, self.receipt(manifest_sha256='0' * 64)))
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        self.assertTrue(spool.apply_receipt(self.topic, self.receipt()))
        self.assertEqual(spool.capture_state(self.key), 'archived')
        self.assertEqual(spool.status()['retained_capture_bytes'], len(self.content))
        self.assertEqual(spool.prepare_batch(limit=16), ())

    def test_old_scalar_spool_untouched(self):
        self.path = self.root / 'gateway-v2.db'
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute('CREATE TABLE uplink_spool(payload BLOB)')
            db.execute('INSERT INTO uplink_spool VALUES(?)', (b'old-scalar',))
        for suffix in ('-journal', '-wal', '-shm'):
            Path(str(self.path) + suffix).write_bytes(b'foreign-sidecar')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaises(WaveformError): self.spool()
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_restart_source_removed_and_exact_file_identity(self):
        spool = self.spool()
        original = self.root / 'original.owpwave'
        original.write_bytes(self.content)
        with original.open('rb') as source:
            identifier = spool.enqueue(source)
        original.unlink()
        spool.close()
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil') as reopened:
            self.assertEqual(reopened.enqueue(BytesIO(self.content)), identifier)
            self.assertEqual([m.payload for m in reopened.prepare_batch()], [self.manifest, *self.chunks])
            for changed in (self.content + b'\n', b'\n'.join((self.manifest, *reversed(self.chunks)))):
                with self.assertRaises(WaveformError): reopened.enqueue(BytesIO(changed))

    def test_capacity_archives_still_charged_and_admission_atomic(self):
        charge = len(self.content) + 1024 * (2 + len(self.chunks))
        spool = self.spool(charge - 1)
        with self.assertRaises(WaveformError): spool.enqueue(BytesIO(self.content))
        self.assertEqual(spool.status()['captures'], 0)
        spool.close()
        self.path = self.root / 'exact.db'
        spool = self.spool(charge)
        spool.enqueue(BytesIO(self.content))
        self.assertEqual(spool.status()['charged_bytes'], charge)
        spool.apply_receipt(self.topic, self.receipt())
        self.assertEqual(spool.status()['charged_bytes'], charge)

    def test_clock_rollback_does_not_age_matching_receipts_or_stall_retry(self):
        spool = self.spool()
        with patch('time.time', return_value=3000): spool.enqueue(BytesIO(self.content))
        with patch('time.time', return_value=1):
            self.assertTrue(spool.prepare_batch())
            self.assertTrue(spool.apply_receipt(self.topic, self.receipt(recorded_at='2001-01-01T00:00:00.000000Z')))
        self.assertEqual(spool.capture_state(self.key), 'archived')

    def test_conflicting_receipts_and_bounded_diagnostics(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        receipt = self.receipt(1)
        self.assertTrue(spool.apply_receipt(self.topic, receipt))
        self.assertTrue(spool.apply_receipt(self.topic, receipt))
        self.assertFalse(spool.apply_receipt(self.topic, self.receipt(1, status='quarantined', reason_code='layout_invalid')))
        for _ in range(140): self.assertFalse(spool.apply_receipt(self.topic, b'not a receipt'))
        self.assertEqual(spool.status()['diagnostic_events'], 128)
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')

    def test_persisted_corruption_cannot_be_sent(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        spool.close()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute('UPDATE sender_capture SET content=?', (self.content[:-1] + b'!',))
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil') as reopened:
            self.assertEqual(reopened.prepare_batch(), ())
            self.assertEqual(reopened.capture_state(self.key), 'local_corrupt')
            self.assertFalse(reopened.apply_receipt(self.topic, self.receipt()))
            self.assertEqual(reopened.status()['retained_capture_bytes'], len(self.content))

    def test_busy_aliases_binding_and_budget_refused(self):
        spool = self.spool()
        for alias in (self.path, self.root / '.' / self.path.name.upper()):
            with self.assertRaises(WaveformError) as caught:
                self.module().WaveSenderSpool.initialize(alias, max_bytes=BUDGET)
            self.assertEqual(caught.exception.code, 'waveform.sender_busy')
        spool.close()
        for kwargs in ({'max_bytes': BUDGET + 1, 'gateway_id': 'GW-001', 'topic_prefix': 'oil'},
                       {'max_bytes': BUDGET, 'gateway_id': 'OTHER', 'topic_prefix': 'oil'},
                       {'max_bytes': BUDGET, 'gateway_id': 'GW-001', 'topic_prefix': 'else'}):
            with self.assertRaises(WaveformError): self.module().WaveSenderSpool.open(self.path, **kwargs)
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil'):
            pass

    def test_hardlink_and_missing_or_copied_identity_refused(self):
        spool = self.spool()
        spool.close()
        before = self.path.read_bytes()
        hardlink = self.root / 'alias.db'
        os.link(self.path, hardlink)
        with self.assertRaises(WaveformError): self.spool()
        hardlink.unlink()
        marker = Path(str(self.path) + '.identity')
        saved = marker.read_bytes()
        marker.unlink()
        with self.assertRaises(WaveformError): self.spool()
        self.assertEqual(self.path.read_bytes(), before)
        marker.write_bytes(saved)
        self.path = self.root / 'copy.db'
        self.path.write_bytes(before)
        Path(str(self.path) + '.identity').write_bytes(saved)
        with self.assertRaises(WaveformError): self.spool()

    def test_sender_cli_requires_explicit_init_and_is_database_independent(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.sender'), 'local sender CLI is required')
        config = self.root / 'config.toml'
        config.write_text(toml_text(local_sections(self.path)), encoding='utf-8')
        script = """import sys
from unittest.mock import patch
class DenyServerImports:
 def find_spec(self, fullname, *args):
  if fullname.startswith('psycopg') or fullname in ('backend.waveforms.consumer','backend.waveforms.publisher'):
   raise AssertionError('local sender imported server machinery')
sys.meta_path.insert(0,DenyServerImports())
with patch('socket.create_connection',side_effect=AssertionError('unexpected network')), patch('threading.Thread.start',side_effect=AssertionError('unexpected thread')):
 from backend.waveforms.sender import main
 raise SystemExit(main(sys.argv[1:]))
"""
        def cli(*args):
            return subprocess.run([sys.executable, '-c', script, *args, '--config', str(config)],
                env=os.environ | {'TEST_MQTT_SENDER': 'local-only'}, capture_output=True, text=True, timeout=15)
        failed = cli('status')
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertFalse(self.path.exists())
        self.assertEqual(cli('init').returncode, 0)
        original = self.root / 'input.owpwave'
        original.write_bytes(self.content)
        queued = cli('enqueue', '--input', str(original))
        self.assertEqual(queued.returncode, 0, queued.stderr + queued.stdout)
        status = cli('status')
        self.assertEqual(json.loads(status.stdout)['retained_capture_bytes'], len(self.content))

    def test_real_broker_acceptance_and_capture_receipt(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.sender'))
        from backend.waveforms import sender
        self.assertTrue(hasattr(sender, 'WaveSender'), 'sender MQTT lifecycle is required')
        from backend.waveforms.config import load_waveform_config
        from tests.waveform_stack import Broker
        from tests.ingest_broker import Peer
        broker = Broker(self.root, ['GW-001'])
        self.addCleanup(broker.close)
        observer = Peer(broker, 'waveform-GW-001-receive')
        broker.clients.append(observer)
        observer.subscribe('oil/v2/GW-001/+/waveform')
        publisher = Peer(broker, 'waveform-GW-001-publish')
        broker.clients.append(publisher)
        config_path = self.root / 'config.toml'
        config_path.write_text(toml_text(local_sections(self.path, broker=broker)), encoding='utf-8')
        with patch.dict(os.environ, {'TEST_MQTT_SENDER': broker.password}):
            config = load_waveform_config(config_path, component='sender')
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        worker = sender.WaveSender(spool, config.gateways[0])
        self.addCleanup(worker.close)
        self.assertEqual(worker.run_once(), len(self.chunks) + 1)
        received = [observer.messages.get(timeout=5) for _ in range(len(self.chunks) + 1)]
        self.assertEqual([m.payload for m in received], [self.manifest, *self.chunks])
        self.assertTrue(all(m.qos == 1 and not m.retain for m in received))
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        # Persistent offline session delivers a matching delayed receipt after reconnect.
        publisher.publish(self.topic, self.receipt())
        worker.run_once()
        self.assertEqual(spool.capture_state(self.key), 'archived')
        self.assertEqual(spool.status()['retained_capture_bytes'], len(self.content))

    def test_strong_kill_hot_journal_recovers_and_lock_is_released(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        spool.close()
        ready = self.root / 'ready'
        script = """import sys,time
from pathlib import Path
from backend.waveforms.sender_spool import WaveSenderSpool
s=WaveSenderSpool.open(sys.argv[1],max_bytes=16777216,gateway_id='GW-001',topic_prefix='oil')
s._db.execute('PRAGMA cache_size=10')
s._db.execute('BEGIN IMMEDIATE')
s._db.execute('UPDATE sender_capture SET content=zeroblob(length(content))')
Path(sys.argv[2]).write_text('ready')
time.sleep(120)
"""
        process = subprocess.Popen([sys.executable, '-c', script, str(self.path), str(ready)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(stop_process, process)
        await_condition(lambda: ready.exists() or process.poll() is not None)
        self.assertIsNone(process.poll())
        journal = Path(str(self.path) + '-journal')
        self.assertTrue(journal.exists())
        self.assertGreater(journal.stat().st_size, len(self.content))
        with self.assertRaises(WaveformError) as caught: self.spool()
        self.assertEqual(caught.exception.code, 'waveform.sender_busy')
        process.kill()
        process.communicate(timeout=10)
        self.assertNotEqual(journal.read_bytes()[:8], b'\0' * 8)
        marker = Path(str(self.path) + '.identity')
        marker_data = marker.read_bytes()
        marker.unlink()
        before = (self.path.read_bytes(), journal.read_bytes())
        with self.assertRaises(WaveformError): self.spool()
        self.assertEqual((self.path.read_bytes(), journal.read_bytes()), before)
        marker.write_bytes(marker_data)
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil') as reopened:
            self.assertEqual([m.payload for m in reopened.prepare_batch()], [self.manifest, *self.chunks])

    def test_real_sqlite_full_never_reports_success_or_erases_old_capture(self):
        spool = self.spool()
        identifier = spool.enqueue(BytesIO(self.content))
        page_count = spool._db.execute('PRAGMA page_count').fetchone()[0]
        spool._db.execute('PRAGMA max_page_count=' + str(page_count))
        manifest = json.loads(self.manifest)
        manifest['capture_id'] = str(uuid4())
        raw = json.dumps(manifest, separators=(',', ':')).encode()
        chunks = []
        for payload in self.chunks:
            body = json.loads(payload)
            body.update(capture_id=manifest['capture_id'], manifest_sha256=sha256(raw).hexdigest())
            chunks.append(json.dumps(body, separators=(',', ':')).encode())
        with self.assertRaises(WaveformError): spool.enqueue(BytesIO(b'\n'.join((raw, *chunks))))
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil') as reopened:
            self.assertEqual(reopened.status()['captures'], 1)
            self.assertEqual(reopened.enqueue(BytesIO(self.content)), identifier)

    def test_empty_existing_file_and_wal_are_never_adopted(self):
        self.path.write_bytes(b'')
        with self.assertRaises(WaveformError): self.spool()
        self.assertEqual(self.path.read_bytes(), b'')
        self.path = self.root / 'owned.db'
        spool = self.spool()
        spool.close()
        wal = Path(str(self.path) + '-wal')
        wal.write_bytes(b'do-not-remove')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaises(WaveformError): self.spool()
        for name, content in before.items(): self.assertEqual((self.root / name).read_bytes(), content)

    def test_connect_failure_releases_owner(self):
        spool = self.spool()
        spool.close()
        with patch('backend.waveforms.sender_spool.sqlite3.connect', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(WaveformError): self.spool()
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil'):
            pass

    def test_threads_enqueue_receipt_status_and_close_serialize(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        errors = []
        barrier = threading.Barrier(4)
        def operation(fn):
            barrier.wait()
            try: fn()
            except WaveformError as error:
                if error.code != 'waveform.sender_closed': errors.append(error)
            except BaseException as error: errors.append(error)
        threads = [threading.Thread(target=operation, args=(fn,)) for fn in (
            lambda: spool.enqueue(BytesIO(self.content)), lambda: spool.apply_receipt(self.topic, self.receipt()),
            spool.status, spool.close)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])

    def test_failed_owner_claim_closes_its_handle(self):
        self.module()
        from backend.waveforms import sender_paths
        original = sender_paths.identity
        def reject_lock(path):
            if path.name.endswith('.owner'): raise sender_paths.failure('ownership_invalid')
            return original(path)
        owner = sender_paths.Ownership.__new__(sender_paths.Ownership)
        self.addCleanup(owner.close)
        with patch.object(sender_paths, 'identity', side_effect=reject_lock):
            with self.assertRaises(WaveformError): owner.__init__(self.path)
        self.assertTrue(owner.handle is None or owner.handle.closed, 'failed claim leaked owned lock handle')
        spool = self.spool()
        self.assertEqual(spool.status()['captures'], 0)

    def test_commit_uncertainty_never_returns_success(self):
        spool = self.spool()
        original = spool._db
        class CommitUncertain:
            def __getattr__(self, name): return getattr(original, name)
            def commit(self):
                original.commit()
                raise sqlite3.OperationalError('injected uncertainty after commit')
        spool._db = CommitUncertain()
        with self.assertRaises(WaveformError): spool.enqueue(BytesIO(self.content))
        with self.module().WaveSenderSpool.open(self.path, max_bytes=BUDGET, gateway_id='GW-001', topic_prefix='oil') as reopened:
            self.assertEqual(reopened.status()['captures'], 1)
            first = reopened.enqueue(BytesIO(self.content))
            self.assertEqual(reopened.enqueue(BytesIO(self.content)), first)

    def test_junction_alias_resolves_to_same_owner(self):
        spool = self.spool()
        alias = self.root / 'directory-alias'
        # mklink /J creates an owned local directory junction without symlink privilege.
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(self.root)],
            capture_output=True, timeout=10)
        self.assertEqual(created.returncode, 0, created.stderr)
        try:
            with self.assertRaises(WaveformError) as caught:
                self.module().WaveSenderSpool.initialize(alias / self.path.name, max_bytes=BUDGET)
            self.assertEqual(caught.exception.code, 'waveform.sender_busy')
            spool.close()
            with self.module().WaveSenderSpool.open(alias / self.path.name, max_bytes=BUDGET,
                    gateway_id='GW-001', topic_prefix='oil') as reopened:
                self.assertEqual(reopened.status()['captures'], 0)
        finally:
            alias.rmdir()  # Remove only this junction, never recurse into its target.

    def test_network_device_and_alternate_stream_paths_refused(self):
        for path in ('//localhost/C$/temp/wave.db', r'\\?\C:\temp\wave.db', str(self.path) + ':stream'):
            with self.assertRaises(WaveformError) as caught:
                self.module().WaveSenderSpool.initialize(path, max_bytes=BUDGET)
            self.assertEqual(caught.exception.code, 'waveform.sender_path_unsupported')

    def test_cli_run_busy_kill_restart_and_broker_restart(self):
        from tests.waveform_stack import Broker
        from tests.ingest_broker import Peer
        broker = Broker(self.root, ['GW-001'])
        self.addCleanup(broker.close)
        observer = Peer(broker, 'waveform-GW-001-receive')
        broker.clients.append(observer)
        observer.subscribe('oil/v2/GW-001/+/waveform')
        config = self.root / 'config.toml'
        config.write_text(toml_text(local_sections(self.path, broker=broker)), encoding='utf-8')
        source = self.root / 'input.owpwave'
        source.write_bytes(self.content)
        environment = os.environ | {'TEST_MQTT_SENDER': broker.password}
        command = [sys.executable, '-m', 'backend.waveforms.sender']
        def cli(*args):
            return subprocess.run(command + list(args) + ['--config', str(config)],
                capture_output=True, text=True, timeout=15, env=environment)
        self.assertEqual(cli('init').returncode, 0)
        self.assertEqual(cli('enqueue', '--input', str(source)).returncode, 0)
        source.unlink()
        process = subprocess.Popen(command + ['run', '--config', str(config)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(stop_process, process)
        self.assertEqual(observer.messages.get(timeout=10).payload, self.manifest)
        for verb in ('init', 'status'):
            busy = cli(verb)
            self.assertEqual(busy.returncode, 3, busy.stdout + busy.stderr)
            self.assertEqual(json.loads(busy.stdout)['code'], 'waveform.sender_busy')
        process.kill()
        process.communicate(timeout=10)
        self.assertEqual(json.loads(cli('status').stdout)['retained_capture_bytes'], len(self.content))
        broker.close()
        broker.start()
        observer = Peer(broker, 'waveform-GW-001-receive')
        broker.clients.append(observer)
        observer.subscribe('oil/v2/GW-001/+/waveform')
        process = subprocess.Popen(command + ['run', '--config', str(config)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(stop_process, process)
        received = [observer.messages.get(timeout=10).payload for _ in range(len(self.chunks) + 1)]
        self.assertCountEqual(received, [self.manifest, *self.chunks])
        process.kill()
        process.communicate(timeout=10)
        self.assertEqual(cli('status').returncode, 0)

    def mqtt_fixture(self):
        from backend.waveforms import sender
        from backend.waveforms.config import load_waveform_config
        from tests.waveform_stack import Broker
        broker = Broker(self.root, ['GW-001'])
        self.addCleanup(broker.close)
        config = self.root / 'config.toml'
        config.write_text(toml_text(local_sections(self.path, broker=broker)), encoding='utf-8')
        with patch.dict(os.environ, {'TEST_MQTT_SENDER': broker.password}):
            gateway = load_waveform_config(config, component='sender').gateways[0]
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        return sender, broker, gateway, spool

    def test_real_no_subscribers_puback_and_acl_denial_keep_pending(self):
        sender, broker, gateway, spool = self.mqtt_fixture()
        worker = sender.WaveSender(spool, gateway)
        self.addCleanup(worker.close)
        self.assertEqual(worker.run_once(), len(self.scan.lines))
        self.assertEqual(spool._db.execute('SELECT sum(broker_accepted) FROM sender_message').fetchone()[0], len(self.scan.lines))
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        denied = replace(gateway, mqtt=replace(gateway.mqtt, username='waveform-GW-001-receive'))
        worker.close()
        worker = sender.WaveSender(spool, denied)
        self.addCleanup(worker.close)
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(spool.status()['retained_capture_bytes'], len(self.content))

    def test_real_delayed_wrong_digest_and_retained_receipts_do_not_archive(self):
        sender, broker, gateway, spool = self.mqtt_fixture()
        from tests.ingest_broker import Peer
        publisher = Peer(broker, 'waveform-GW-001-publish')
        broker.clients.append(publisher)
        worker = sender.WaveSender(spool, gateway)
        self.addCleanup(worker.close)
        worker.run_once()
        publisher.publish(self.topic, self.receipt(), retain=True)
        publisher.publish(self.topic, self.receipt(manifest_sha256='0' * 64))
        worker.run_once()
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        self.assertGreaterEqual(spool.status()['diagnostic_events'], 1)
        publisher.publish(self.topic, self.receipt())
        worker.run_once()
        self.assertEqual(spool.capture_state(self.key), 'archived')

    def test_old_client_callbacks_are_fenced_and_sockets_close(self):
        from types import SimpleNamespace
        sender, broker, gateway, spool = self.mqtt_fixture()
        clients, saved, sockets = [], [], []
        def factory(gateway):
            client = sender._client(gateway)
            loop = client.loop
            first = True
            def drive(*args, **kwargs):
                nonlocal first
                if first:
                    first = False
                    if saved:
                        old_client, old_message, old_publish = saved[0]
                        old_message(old_client, None, SimpleNamespace(qos=1, retain=False, topic=self.topic,
                            payload=self.receipt(), mid=1))
                        old_publish(old_client, None, 1, SimpleNamespace(value=0), None)
                    saved.append((client, client.on_message, client.on_publish))
                    sockets.append(client.socket())
                return loop(*args, **kwargs)
            client.loop = drive
            clients.append(client)
            return client
        worker = sender.WaveSender(spool, gateway, client_factory=factory)
        self.addCleanup(worker.close)
        worker.run_once()
        worker.run_once()
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')
        self.assertTrue(all(sock.fileno() == -1 for sock in sockets))
        self.assertTrue(all(c.socket() is None and c._sockpairR is None and c._sockpairW is None for c in clients))

    def test_failed_disposal_prevents_any_replacement(self):
        sender, broker, gateway, spool = self.mqtt_fixture()
        clients = []
        def factory(gateway):
            client = sender._client(gateway)
            clients.append(client)
            original = client._reset_sockets
            self.addCleanup(original)
            client._reset_sockets = lambda *args, **kwargs: None
            client.disconnect = lambda: 0
            return client
        worker = sender.WaveSender(spool, gateway, client_factory=factory)
        self.addCleanup(worker.close)
        with self.assertRaises(WaveformError) as caught: worker.run_once()
        self.assertEqual(caught.exception.code, 'waveform.sender_shutdown_incomplete')
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(len(clients), 1)

    def test_init_interruption_does_not_adopt_unmarked_file(self):
        module = self.module()
        with patch('backend.waveforms.sender_paths.Ownership.establish', side_effect=OSError('injected init interruption')):
            with self.assertRaises(WaveformError): module.WaveSenderSpool.initialize(self.path, max_bytes=BUDGET)
        before = self.path.read_bytes()
        with self.assertRaises(WaveformError): self.spool()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(Path(str(self.path) + '.identity').exists())

    def test_unknown_receipt_and_foreign_capture_cannot_change_queue(self):
        spool = self.spool()
        self.assertFalse(spool.apply_receipt(self.topic, self.receipt()))
        spool.enqueue(BytesIO(self.content))
        for changes in ({'gateway_id': 'OTHER'}, {'capture_id': uuid4()}, {'device_id': 'OTHER'}):
            self.assertFalse(spool.apply_receipt(self.topic, self.receipt(**changes)))
        self.assertFalse(spool.apply_receipt(self.topic, self.receipt(1, payload_sha256='0' * 64)))
        self.assertEqual(spool.capture_state(self.key), 'awaiting_capture_receipt')

    def test_capture_invalid_and_quarantine_preserve_failed_bytes(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        self.assertTrue(spool.apply_receipt(self.topic, self.receipt(1, status='quarantined', reason_code='layout_invalid')))
        self.assertEqual(spool.capture_state(self.key), 'failed')
        self.assertFalse(spool.apply_receipt(self.topic, self.receipt()))
        self.assertTrue(spool.apply_receipt(self.topic, self.receipt(status='invalid', reason_code='layout_invalid')))
        self.assertEqual(spool.prepare_batch(), ())
        self.assertEqual(spool.status()['retained_capture_bytes'], len(self.content))

    def test_durability_readback_schema_accounting_and_metadata_corruption(self):
        spool = self.spool()
        self.assertEqual(spool._db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(spool._db.execute('PRAGMA synchronous').fetchone()[0], 3)
        spool.enqueue(BytesIO(self.content))
        spool.close()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute('UPDATE sender_capture SET charge=charge+1')
        before = self.path.read_bytes()
        with self.assertRaises(WaveformError) as caught: self.spool()
        self.assertEqual(caught.exception.code, 'waveform.sender_accounting_invalid')
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute('UPDATE sender_capture SET charge=charge-1')
            db.execute('ALTER TABLE sender_capture ADD COLUMN unowned TEXT')
        before = self.path.read_bytes()
        with self.assertRaises(WaveformError) as caught: self.spool()
        self.assertEqual(caught.exception.code, 'waveform.sender_schema_invalid')
        self.assertEqual(self.path.read_bytes(), before)

    def test_local_message_offset_corruption_cannot_be_sent(self):
        spool = self.spool()
        spool.enqueue(BytesIO(self.content))
        spool._db.execute('UPDATE sender_message SET offset=offset+1 WHERE ordinal=1')
        self.assertEqual(spool.prepare_batch(), ())
        self.assertEqual(spool.capture_state(self.key), 'local_corrupt')

    def test_source_is_snapshotted_once_and_wrong_gateway_is_rejected(self):
        spool = self.spool()
        content = self.content
        class ChangingSource(BytesIO):
            def read(self, size=-1):
                result = super().read(size)
                self.seek(0)
                self.write(b'corrupt-after-read')
                return result
        spool.enqueue(ChangingSource(content))
        self.assertEqual([m.payload for m in spool.prepare_batch()], [self.manifest, *self.chunks])
        spool.close()
        self.path = self.root / 'another.db'
        with self.module().WaveSenderSpool.initialize(self.path, max_bytes=BUDGET) as other:
            other.bind('OTHER', 'oil')
            with self.assertRaises(WaveformError): other.enqueue(BytesIO(content))
            self.assertEqual(other.status()['captures'], 0)


if __name__ == '__main__':
    unittest.main()
