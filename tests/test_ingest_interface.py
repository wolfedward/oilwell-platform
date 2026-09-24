"""Public tools must agree with exact-byte center and durable gateway behavior."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
import queue
from zipfile import ZipFile

from backend.gateway.reliable_spool import ReliableSpool, SpoolLimits
from backend.gateway.spool_migrate import initialize_spool
from backend.ingest_reliability.contracts import Receipt
from tests.ingest_vectors import observation, receipt_for
from tests.ingest_broker import Broker, raw_publish_response

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'assets/open-interface'
PUBLIC_FILES = {'index.html', 'portal.css', 'INTERFACE.md', 'HARDWARE.md', 'ROADMAP.md',
                'LICENSE.txt', 'telemetry.schema.json', 'telemetry-example.json',
                'validate_telemetry.py', 'hardware-overview.svg', 'terminal-interfaces.svg',
                'ingest-ack.schema.json', 'ingest-ack-example.json', 'validate_ingest_ack.py'}


class IngestInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((KIT / 'validate_ingest_ack.py').is_file(), 'public receipt validator missing')
        self.validate = runpy.run_path(str(KIT / 'validate_ingest_ack.py'))['validate']

    def agreement(self, topic, payload, obs, expected):
        try:
            center = Receipt.from_bytes(payload).matches(topic, obs)
        except ValueError:
            center = False
        self.assertEqual(center, expected, 'center disagrees with literal expectation')
        if expected:
            report = self.validate(topic, payload, telemetry_topic=obs.topic, telemetry_payload=obs.payload)
            self.assertEqual(report['message_id'], 'M-1')
            self.assertEqual(report['payload_sha256'], sha256(obs.payload).hexdigest())
            self.assertEqual(report['schema'], 'OWP-INGEST-ACK/1.0')
        else:
            with self.assertRaises(ValueError):
                self.validate(topic, payload, telemetry_topic=obs.topic, telemetry_payload=obs.payload)
        with tempfile.TemporaryDirectory(prefix='public-receipt-') as folder:
            path = Path(folder) / 'queue.db'
            limits = SpoolLimits(10, 4 * 1024 * 1024, 1024 * 1024)
            initialize_spool(path, limits=limits)
            spool = ReliableSpool(path, limits=limits)
            try:
                spool.enqueue('M-1', obs.topic, obs.payload, confirmation_mode='application')
                self.assertEqual(spool.apply_receipt(topic, payload), 'applied' if expected else 'ignored')
                self.assertEqual(spool.get('M-1')['state'], json.loads(payload)['status'] if expected else 'pending')
            finally:
                spool.close()

    def test_same_strict_receipt_boundary_vectors_reach_all_three_implementations(self):
        obs = observation()
        body = asdict(receipt_for(obs))
        topic = 'oilfield/v2/GW/D/ingest-ack'
        raw = json.dumps(body).encode()
        self.agreement(topic, raw, obs, True)
        self.agreement(topic, raw + b' ' * (4096 - len(raw)), obs, True)
        self.agreement(topic, raw + b' ' * (4097 - len(raw)), obs, False)
        invalid = [dict(body, unknown='x'), {k: v for k, v in body.items() if k != 'status'}]
        changes = [dict(status=value) for value in (True, 1, None, [], {}, 'duplicate')]
        changes += [dict(schema='OWP-INGEST-ACK/2.0'), dict(receipt_id='invalid'),
                    dict(receipt_id='00000000000040008000000000000001'),
                    dict(gateway_id='GW/OTHER'), dict(device_id='+'), dict(message_id=''),
                    dict(message_id='M' * 129), dict(message_id='M\n'),
                    dict(payload_sha256='a' * 63), dict(payload_sha256='A' * 64),
                    dict(payload_sha256='g' * 64), dict(reason_code='json_invalid'),
                    dict(status='quarantined'), dict(status='quarantined', reason_code='internal_exception'),
                    dict(recorded_at='2026-09-24T00:00:00+00:00'),
                    dict(recorded_at='2026-09-24T00:00:00.000000+08:00'),
                    dict(recorded_at='2026-02-30T00:00:00.000000Z'),
                    dict(recorded_at='2026-09-24 00:00:00.000000Z'),
                    dict(recorded_at='2026-09-24T00:00:00.0000000Z')]
        invalid += [dict(body, **change) for change in changes]
        encoded = [json.dumps(item).encode() for item in invalid]
        encoded += [raw.replace(b'"status":', b'"status":"stored","status":'),
                    b'\xff', b'[]', b'null', b'\xef\xbb\xbf' + raw,
                    b'[' * 2000 + b'0' + b']' * 2000]
        for index, payload in enumerate(encoded):
            with self.subTest(vector=index):
                self.agreement(topic, payload, obs, False)
        for reason in ('topic_invalid', 'payload_too_large', 'utf8_invalid', 'json_invalid',
                       'json_duplicate_key', 'contract_invalid', 'identity_mismatch',
                       'idempotency_conflict', 'qos_not_supported', 'retained_publish'):
            with self.subTest(reason=reason):
                self.agreement(topic, json.dumps(dict(body, status='quarantined', reason_code=reason)).encode(), obs, True)

    def test_exact_bytes_and_namespace_identity_are_not_normalized(self):
        obs = observation()
        receipt = receipt_for(obs)
        topic = 'oilfield/v2/GW/D/ingest-ack'
        self.agreement(topic, receipt.to_bytes(), replace(obs, payload=obs.payload + b' '), False)
        for wrong in ('oilfield/v2/OTHER/D/ingest-ack', 'other/v2/GW/D/ingest-ack',
                      'oilfield/v1/GW/D/ingest-ack', 'oilfield/v2/GW/D/telemetry',
                      'oilfield/v2/GW/D/ingest-ack/extra', ''):
            with self.subTest(topic=wrong):
                self.agreement(wrong, receipt.to_bytes(), obs, False)
        for change in ({'gateway_id': 'OTHER'}, {'device_id': 'OTHER'}, {'message_id': 'M-2'}):
            other = replace(receipt, **change)
            self.agreement(other.topic('oilfield'), other.to_bytes(), obs, False)
        self.agreement(topic, replace(receipt, recorded_at='2026-09-24T00:00:00.000000Z').to_bytes(), obs, True)

    def test_quarantine_correlation_requires_whole_unambiguous_json_not_valid_samples(self):
        obs = observation()
        body = json.loads(obs.payload)
        body['samples'] = []
        body['extra'] = 'contract violation'
        rejected = replace(obs, payload=json.dumps(body).encode())
        receipt = receipt_for(rejected, status='quarantined', reason_code='contract_invalid')
        self.agreement(receipt.topic('oilfield'), receipt.to_bytes(), rejected, True)
        for payload in (rejected.payload[:-1], rejected.payload + b'{}', b'\xff',
                        rejected.payload.replace(b'"samples":', b'"samples":[],"samples":'),
                        rejected.payload.replace(b'"samples": []', b'"samples": [NaN]'),
                        rejected.payload.replace(b'"samples": []', b'"samples": [1e-100000]')):
            with self.subTest(payload=payload[:60]):
                malformed = replace(obs, payload=payload)
                bound = replace(receipt, payload_sha256=sha256(payload).hexdigest())
                self.agreement(bound.topic('oilfield'), bound.to_bytes(), malformed, False)

    def test_validator_bounds_inputs_before_parsing_and_rejects_non_bytes(self):
        obs, receipt = observation(), receipt_for(observation())
        for telemetry in (obs.payload + b' ' * 2_097_152, bytearray(obs.payload), None):
            with self.subTest(type=type(telemetry)), self.assertRaises(ValueError):
                self.validate(receipt.topic('oilfield'), receipt.to_bytes(), telemetry_topic=obs.topic, telemetry_payload=telemetry)
        for payload in (bytearray(receipt.to_bytes()), None):
            with self.assertRaises(ValueError):
                self.validate(receipt.topic('oilfield'), payload, telemetry_topic=obs.topic, telemetry_payload=obs.payload)

    def test_public_example_matches_v1_file_and_cli_works_without_server_imports(self):
        ack = KIT / 'ingest-ack-example.json'
        topic = 'oilfield/v2/GW-EXAMPLE/D-EXAMPLE/ingest-ack'
        telemetry_topic = 'oilfield/v1/GW-EXAMPLE/D-EXAMPLE/telemetry'
        result = self.validate(topic, ack.read_bytes(), telemetry_topic=telemetry_topic,
                               telemetry_payload=(KIT / 'telemetry-example.json').read_bytes())
        self.assertEqual(result['status'], 'stored')
        with tempfile.TemporaryDirectory(prefix='standalone-ack-') as folder:
            root = Path(folder)
            for name in ('validate_ingest_ack.py', 'ingest-ack-example.json', 'telemetry-example.json'):
                (root / name).write_bytes((KIT / name).read_bytes())
            command = [sys.executable, '-I', str(root / 'validate_ingest_ack.py'), '--topic', topic,
                       '--file', str(root / 'ingest-ack-example.json'), '--telemetry-topic', telemetry_topic,
                       '--telemetry-file', str(root / 'telemetry-example.json')]
            done = subprocess.run(command, cwd=root, capture_output=True, timeout=10)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)['status'], 'stored')
            with (root / 'telemetry-example.json').open('ab') as stream:
                stream.write(b' ')
            bad = subprocess.run(command, cwd=root, capture_output=True, timeout=10)
            self.assertEqual(bad.returncode, 2, bad.stderr)
            self.assertFalse(json.loads(bad.stdout)['valid'])


class IngestKitBuildTests(unittest.TestCase):
    def test_packaging_is_fixed_whitelist_exact_bytes_and_repeatable(self):
        builder = ROOT / 'tools/build_open_interface_kit.py'
        self.assertTrue(builder.is_file(), 'kit builder missing')
        build = runpy.run_path(str(builder))['build']
        with tempfile.TemporaryDirectory(prefix='public-kit-') as folder:
            root = Path(folder)
            for name in PUBLIC_FILES:
                (root / name).write_bytes((KIT / name).read_bytes())
            (root / 'credentials.local.toml').write_text('not-public')
            (root / 'service.log').write_text('not-public')
            report = build(root)
            destination = root / 'open-interface-r1.zip'
            original = destination.read_bytes()
            self.assertEqual(report['sha256'], sha256(original).hexdigest())
            self.assertEqual(set(report['files']), PUBLIC_FILES)
            with ZipFile(destination) as archive:
                self.assertEqual(len(archive.namelist()), 14)
                self.assertEqual(set(archive.namelist()), PUBLIC_FILES)
                for name in PUBLIC_FILES:
                    self.assertEqual(archive.read(name), (root / name).read_bytes())
            build(root)
            self.assertEqual(destination.read_bytes(), original)
            (root / 'validate_ingest_ack.py').unlink()
            with self.assertRaises((OSError, ValueError)):
                build(root)
            self.assertEqual(destination.read_bytes(), original, 'failed build must preserve last archive')

    def test_schema_declares_closed_string_contract_and_outcomes(self):
        schema = json.loads((KIT / 'ingest-ack.schema.json').read_bytes())
        names = set(asdict(receipt_for(observation())))
        self.assertEqual(set(schema['required']), names)
        self.assertEqual(set(schema['properties']), names)
        self.assertIs(schema['additionalProperties'], False)
        for value in schema['properties'].values():
            self.assertEqual(value['type'], 'string')
        self.assertEqual(schema['oneOf'][0]['properties']['status']['const'], 'stored')
        self.assertEqual(schema['oneOf'][0]['properties']['reason_code']['const'], 'accepted')
        self.assertEqual(schema['oneOf'][1]['properties']['status']['const'], 'quarantined')
        self.assertEqual(len(schema['oneOf'][1]['properties']['reason_code']['enum']), 10)


class IngestBrokerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker()
        self.addCleanup(self.broker.close)

    def test_gateway_reads_own_receipts_only_even_with_broad_subscription(self):
        gateway = self.broker.client('gateway-GW-001')
        publisher = self.broker.client('platform-receipt-publisher')
        gateway.subscribe('oilfield/v2/+/+/ingest-ack')
        self.assertLess(publisher.publish('oilfield/v2/GW-002/D/ingest-ack', b'other-gateway'), 128)
        self.assertLess(publisher.publish('oilfield/v2/GW-001/D/ingest-ack', b'own-gateway'), 128)
        self.assertEqual(gateway.messages.get(timeout=5).payload, b'own-gateway')
        with self.assertRaises(queue.Empty):
            gateway.messages.get(timeout=.2)

    def test_gateway_cannot_forge_receipts_or_publish_another_gateway(self):
        gateway = self.broker.client('gateway-GW-001')
        for topic in ('oilfield/v2/GW-001/D/ingest-ack', 'oilfield/v2/GW-002/D/ingest-ack',
                      'oilfield/v1/GW-002/D/telemetry'):
            with self.subTest(topic=topic):
                self.assertGreaterEqual(gateway.publish(topic, b'forbidden'), 128)
        self.assertLess(gateway.publish('oilfield/v1/GW-001/D/telemetry', b'own-telemetry'), 128)

    def test_publisher_cannot_write_telemetry_and_center_is_read_only(self):
        publisher = self.broker.client('platform-receipt-publisher')
        center = self.broker.client('platform-ingestor')
        self.assertGreaterEqual(publisher.publish('oilfield/v1/GW-001/D/telemetry', b'forbidden'), 128)
        for topic in ('oilfield/v1/GW-001/D/telemetry', 'oilfield/v2/GW-001/D/ingest-ack'):
            with self.subTest(topic=topic):
                self.assertGreaterEqual(center.publish(topic, b'forbidden'), 128)

    def test_template_allows_exact_one_mib_and_preserves_retain_for_classification(self):
        from backend.ingest_reliability.wire import classify
        center = self.broker.client('platform-ingestor')
        gateway = self.broker.client('gateway-GW-001')
        self.assertEqual(center.properties.MaximumPacketSize, 2_097_152)
        self.assertEqual(center.subscribe('oilfield/v1/+/+/telemetry'), [1])
        obs = observation(gateway='GW-001')
        payload = obs.payload + b' ' * (1_048_576 - len(obs.payload))
        self.assertEqual(gateway.publish(obs.topic, payload), 0)
        message = center.messages.get(timeout=5)
        self.assertEqual(message.payload, payload)
        self.assertIsNone(classify(replace(obs, payload=message.payload), prefix='oilfield').reason_code)
        self.assertEqual(gateway.publish(obs.topic, payload + b' '), 0)
        too_large = center.messages.get(timeout=5)
        self.assertEqual(classify(replace(obs, payload=too_large.payload), prefix='oilfield').reason_code, 'payload_too_large')
        self.assertEqual(gateway.publish(obs.topic, obs.payload, retain=True), 0)
        retained = center.messages.get(timeout=5)
        self.assertTrue(retained.retain, 'RAP must expose retained input to quarantine policy')
        self.assertEqual(classify(replace(obs, retain=retained.retain), prefix='oilfield').reason_code, 'retained_publish')

    def test_whole_packet_limit_includes_topic_and_protocol_overhead(self):
        center = self.broker.client('platform-ingestor')
        gateway = self.broker.client('gateway-GW-001')
        center.subscribe('oilfield/v1/+/+/telemetry')
        info = gateway.client.publish('oilfield/v1/GW-001/D/telemetry', b'x' * 2_097_152, qos=1)
        self.assertEqual(info.rc, 0)
        self.assertTrue(gateway.disconnected.wait(5), 'oversized full packet must disconnect')
        with self.assertRaises(queue.Empty):
            center.messages.get(timeout=.2)

    def test_payload_limit_can_complete_qos_handshake_without_forwarding(self):
        # A smaller isolated payload limit demonstrates why PUBACK is not storage.
        broker = Broker(message_size_limit=1_048_576)
        self.addCleanup(broker.close)
        center = broker.client('platform-ingestor')
        import paho.mqtt.client as mqtt
        gateway = broker.client('gateway-GW-001', protocol=mqtt.MQTTv311)
        center.subscribe('oilfield/v1/+/+/telemetry')
        self.assertEqual(gateway.publish('oilfield/v1/GW-001/D/telemetry', b'x' * 1_048_577), 0)
        # Mosquitto 2.1.2 emits PUBACK 0x95 here; Paho 2.1.0 refuses that
        # packet/reason combination. Capture the wire rather than masking it.
        header, response = raw_publish_response(broker, 'oilfield/v1/GW-001/D/telemetry', b'x' * 1_048_577)
        self.assertEqual(header, 0x40)
        self.assertEqual(response[:2], b'\x00\x01')
        self.assertGreaterEqual(response[2], 128)
        with self.assertRaises(queue.Empty):
            center.messages.get(timeout=.2)
