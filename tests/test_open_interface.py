"""Manufacturer kit examples must work with the shipping telemetry receiver."""

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree
from zipfile import ZipFile

from backend.center.ingest import CentralIngestService


KIT = Path(__file__).resolve().parents[1] / "assets" / "open-interface"
TOPIC = "oilfield/v1/GW-EXAMPLE/D-EXAMPLE/telemetry"


class CaptureRepository:
    def store(self, batch, **_kwargs):
        self.batch = batch
        return True


class ResourceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, _tag, attrs):
        for key, value in attrs:
            if key in ("href", "src") and value and not value.startswith(("/", "#", "https://")):
                self.paths.add(value)


class OpenInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((KIT / "validate_telemetry.py").is_file(), "downloadable validator is missing")
        self.validate = runpy.run_path(str(KIT / "validate_telemetry.py"))["validate"]
        self.payload = (KIT / "telemetry-example.json").read_bytes()

    def test_published_example_is_accepted_by_validator_and_real_receiver(self):
        result = self.validate(TOPIC, self.payload)
        self.assertEqual(result["sample_count"], 3)
        repo = CaptureRepository()
        stored = CentralIngestService(repo, clock=lambda: datetime.now(timezone.utc)).ingest(TOPIC, self.payload)
        self.assertEqual(stored.sample_count, 3)
        self.assertEqual(repo.batch.samples[0].value, 5.8)

    def test_validator_rejects_mismatched_topic_unknown_fields_and_bad_sequence(self):
        invalid = []
        body = json.loads(self.payload)
        body["unrecognized"] = 1
        invalid.append(json.dumps(body).encode())
        body = json.loads(self.payload)
        body["sequence"]["last"] = 2
        invalid.append(json.dumps(body).encode())
        body = json.loads(self.payload)
        body["samples"][0]["sequence"] = True
        invalid.append(json.dumps(body).encode())
        body = json.loads(self.payload)
        body["samples"][0]["unit"] = {"wrong": "type"}
        invalid.append(json.dumps(body).encode())
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.validate(TOPIC, payload)
        with self.assertRaises(ValueError):
            self.validate(TOPIC.replace("GW-EXAMPLE", "GW-OTHER"), self.payload)

    def test_validator_rejects_duplicate_keys_nonfinite_naive_time_and_oversize(self):
        for payload in (b'{"schema":1,"schema":2}', self.payload.replace(b'5.8', b'NaN'),
                        self.payload.replace(b'00:00Z', b'00:00'), b'x' * 1_048_577):
            with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                self.validate(TOPIC, payload)

    def test_high_sequence_is_preserved_and_postgres_overflow_is_rejected(self):
        body = json.loads(self.payload)
        for sample in body["samples"]:
            sample["sequence"] = 9007199254740993
        body["sequence"] = {"first": 9007199254740993, "last": 9007199254740993}
        result = self.validate(TOPIC, json.dumps(body).encode())
        self.assertEqual(result["sequence_last"], "9007199254740993")
        body["sequence"]["last"] = 9223372036854775808
        with self.assertRaises(ValueError):
            self.validate(TOPIC, json.dumps(body).encode())

    def test_unit_rejects_characters_postgres_jsonb_cannot_store(self):
        for unit in ("\x00", "kW\ud800", "\udfff"):
            body = json.loads(self.payload)
            body["samples"][0]["unit"] = unit
            with self.subTest(unit=repr(unit)), self.assertRaisesRegex(ValueError, "unit"):
                self.validate(TOPIC, json.dumps(body).encode())
        body["samples"][0]["unit"] = "立方米/日"
        self.assertTrue(self.validate(TOPIC, json.dumps(body).encode())["valid"])

    def test_validator_checks_numeric_tokens_before_float_underflow(self):
        for token in (b'1e-100000', b'-1e-100000', b'0e-100000', b'0e1000000', b'1e309'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.validate(TOPIC, self.payload.replace(b'5.8', token))
        for token in (b'1.25e-300', b'1e308', b'0.0'):
            with self.subTest(token=token):
                self.assertTrue(self.validate(TOPIC, self.payload.replace(b'5.8', token))["valid"])

    def test_drawings_are_editable_svg_with_explicit_reference_status(self):
        for name in ("hardware-overview.svg", "terminal-interfaces.svg", "edge-reference-state.svg"):
            root = ElementTree.parse(KIT / name).getroot()
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertIn("参考设计", "".join(root.itertext()))
            self.assertIn("未经", "".join(root.itertext()))

    def test_portal_resources_exist_and_download_archive_matches_sources(self):
        links = ResourceLinks()
        links.feed((KIT / "index.html").read_text(encoding="utf-8"))
        for path in links.paths:
            self.assertTrue((KIT / path).is_file(), path)
        expected = {"index.html", "portal.css", "INTERFACE.md", "HARDWARE.md", "ROADMAP.md",
                    "LICENSE.txt", "telemetry.schema.json", "telemetry-example.json",
                    "validate_telemetry.py", "hardware-overview.svg", "terminal-interfaces.svg",
                    "ingest-ack.schema.json", "ingest-ack-example.json", "validate_ingest_ack.py",
                    "WAVEFORM.md", "waveform.schema.json", "waveform-ack.schema.json",
                    "validate_waveform.py", "waveform-example.owpwave", "waveform_errors.py",
                    "waveform_contracts.py", "waveform_wire.py", "waveform_samples.py", "waveform_files.py",
                    "EDGE-CONTROL-REFERENCE.md", "edge-reference-state.svg", "edge_protection_reference.py",
                    "protection_replay.py", "protection_contracts.py", "check_edge_reference.py"}
        with ZipFile(KIT / "open-interface-r1.zip") as archive:
            self.assertEqual(set(archive.namelist()), expected)
            for path in expected:
                self.assertEqual(archive.read(path), (KIT / path).read_bytes(), path)

    def test_public_edge_reference_runs_without_platform_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('edge_protection_reference.py', 'protection_replay.py', 'protection_contracts.py'):
                self.assertTrue((KIT / name).is_file(), name)
                raw = (KIT / name).read_bytes()
                self.assertNotIn(b'from backend.', raw)
                (root / name).write_bytes(raw)
            command = [sys.executable, '-B', '-c', 'from edge_protection_reference import EdgeProtectionReference; from protection_replay import ProtectionReplay; print("ok")']
            result = subprocess.run(command, cwd=root, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), b'ok')

    def test_downloadable_cli_has_documented_exit_codes(self):
        command = [sys.executable, str(KIT / "validate_telemetry.py"), "--topic", TOPIC, "--file"]
        success = subprocess.run(command + [str(KIT / "telemetry-example.json")], capture_output=True, timeout=10)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertTrue(json.loads(success.stdout)["valid"])
        with tempfile.TemporaryDirectory() as folder:
            invalid = Path(folder) / "bad.json"
            invalid.write_bytes(self.payload.replace(b'5.8', b'1e-100000'))
            failure = subprocess.run(command + [str(invalid)], capture_output=True, timeout=10)
        self.assertEqual(failure.returncode, 2, failure.stderr)
        self.assertFalse(json.loads(failure.stdout)["valid"])

    def test_public_edge_conformance_vectors_run_without_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('edge_protection_reference.py', 'protection_replay.py', 'protection_contracts.py', 'check_edge_reference.py'):
                self.assertTrue((KIT / name).is_file(), name)
                (root / name).write_bytes((KIT / name).read_bytes())
            result = subprocess.run([sys.executable, '-B', str(root / 'check_edge_reference.py')],
                                    cwd=root, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['status'], 'passed')
            self.assertGreaterEqual(len(report['checks']), 5)
            self.assertFalse(report['hardware_execution'])


if __name__ == "__main__":
    unittest.main()
