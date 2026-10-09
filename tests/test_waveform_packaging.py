"""Deterministic exact public membership without private inputs."""

from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from zipfile import ZipFile
from tests.waveform_vectors import golden_capture


ROOT = Path(__file__).resolve().parents[1]
KIT = Path(os.environ.get("OILWELL_WAVEFORM_PUBLIC_ROOT", ROOT / "assets/open-interface"))
LEGACY = {"index.html", "portal.css", "INTERFACE.md", "HARDWARE.md", "ROADMAP.md",
          "LICENSE.txt", "telemetry.schema.json", "telemetry-example.json",
          "validate_telemetry.py", "hardware-overview.svg", "terminal-interfaces.svg",
          "ingest-ack.schema.json", "ingest-ack-example.json", "validate_ingest_ack.py"}
NEW = {"WAVEFORM.md", "waveform.schema.json", "waveform-ack.schema.json",
       "validate_waveform.py", "waveform-example.owpwave", "waveform_errors.py",
       "waveform_contracts.py", "waveform_wire.py", "waveform_samples.py", "waveform_files.py"}
EXPECTED = LEGACY | NEW
MODULES = {"waveform_errors.py": "errors.py", "waveform_contracts.py": "contracts.py",
           "waveform_wire.py": "wire.py", "waveform_samples.py": "samples.py",
           "waveform_files.py": "files.py"}


class WaveformPackagingTests(unittest.TestCase):
    def test_pure_sources_keep_public_bytes_after_real_git_checkout_filters(self):
        with tempfile.TemporaryDirectory(prefix="waveform-git-filter-") as folder:
            repository = Path(folder) / "repository"
            repository.mkdir()
            mapped = repository / "backend/waveforms"
            mapped.mkdir(parents=True)
            attributes = ROOT / "backend/waveforms/.gitattributes"
            if attributes.is_file():
                shutil.copyfile(attributes, mapped / ".gitattributes")
            expected = {}
            for public, original in MODULES.items():
                source = ROOT / "backend/waveforms" / original
                expected[original] = source.read_bytes()
                shutil.copyfile(source, mapped / original)
            (mapped / "unrelated.py").write_bytes(b"alpha\nbeta\n")

            def git(*args):
                result = subprocess.run(["git", "-C", str(repository), *args],
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            git("init", "-q")
            git("config", "core.autocrlf", "true")
            git("add", "--all")
            checkout = Path(folder) / "checkout"
            checkout.mkdir()
            git("checkout-index", "--all", "--force", f"--prefix={checkout.as_posix()}/")
            for original, raw in expected.items():
                with self.subTest(original=original):
                    self.assertEqual((checkout / "backend/waveforms" / original).read_bytes(), raw)
            self.assertEqual((checkout / "backend/waveforms/unrelated.py").read_bytes(),
                             b"alpha\r\nbeta\r\n")

    def test_public_zip_exact_whitelist(self):
        build = runpy.run_path(str(ROOT / "tools/build_open_interface_kit.py"))["build"]
        with tempfile.TemporaryDirectory(prefix="waveform-public-") as folder:
            root = Path(folder)
            for name in EXPECTED:
                (root / name).write_bytes((KIT / name).read_bytes())
            report = build(root)
            archive_path = root / "open-interface-r1.zip"
            first = archive_path.read_bytes()
            self.assertEqual(set(report["files"]), EXPECTED)
            self.assertEqual(report["sha256"], sha256(first).hexdigest())
            with ZipFile(archive_path) as archive:
                self.assertEqual(len(archive.namelist()), 24)
                self.assertEqual(set(archive.namelist()), EXPECTED)
                for name in EXPECTED:
                    self.assertEqual(archive.read(name), (root / name).read_bytes(), name)
            self.assertEqual(build(root)["sha256"], sha256(first).hexdigest())
            self.assertEqual(archive_path.read_bytes(), first)

    def test_reproducible_kit_no_private_inputs(self):
        build = runpy.run_path(str(ROOT / "tools/build_open_interface_kit.py"))["build"]
        with tempfile.TemporaryDirectory(prefix="waveform-private-") as folder:
            root = Path(folder)
            for name in EXPECTED:
                (root / name).write_bytes((KIT / name).read_bytes())
            for name in ("credentials.local.toml", "data.db", "service.log", "secret.key"):
                (root / name).write_bytes(b"PRIVATE_CANARY")
            first = build(root)["sha256"]
            self.assertEqual(first, build(root)["sha256"])
            with ZipFile(root / "open-interface-r1.zip") as archive:
                self.assertEqual(set(archive.namelist()), EXPECTED)
                self.assertNotIn(b"PRIVATE_CANARY", (root / "open-interface-r1.zip").read_bytes())
            (root / "waveform.schema.json").unlink()
            with self.assertRaises((OSError, ValueError)):
                build(root)
            self.assertEqual(sha256((root / "open-interface-r1.zip").read_bytes()).hexdigest(), first)

    def test_public_modules_match_only_reviewed_source_rewrites(self):
        rewrites = {b"from .errors import ": b"from waveform_errors import ",
                    b"from .contracts import ": b"from waveform_contracts import ",
                    b"from .wire import ": b"from waveform_wire import ",
                    b"from .samples import ": b"from waveform_samples import "}
        for public, original in MODULES.items():
            with self.subTest(public=public):
                source = (ROOT / "backend/waveforms" / original).read_bytes()
                converted = source
                for before, after in rewrites.items():
                    converted = converted.replace(before, after)
                self.assertEqual((KIT / public).read_bytes(), converted)

    def test_waveform_schemas_are_closed_structural_contracts(self):
        manifest = json.loads((KIT / "waveform.schema.json").read_bytes())
        receipt = json.loads((KIT / "waveform-ack.schema.json").read_bytes())
        for schema in (manifest["$defs"][name] for name in
                       ("manifest", "chunk", "channel", "source", "stream", "event", "cycle")):
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(receipt["additionalProperties"])
        self.assertEqual(set(receipt["required"]), set(receipt["properties"]))
        manifest_raw, chunks = golden_capture()
        self.assertEqual(set(json.loads(manifest_raw)), set(manifest["$defs"]["manifest"]["properties"]))
        self.assertEqual(set(json.loads(chunks[0])), set(manifest["$defs"]["chunk"]["properties"]))
        self.assertEqual(manifest["$defs"]["uuid"]["maxLength"], 36)
        self.assertEqual(manifest["$defs"]["sha256"]["maxLength"], 64)
        self.assertEqual(manifest["$defs"]["utc"]["maxLength"], 27)
        self.assertEqual(manifest["$defs"]["manifest"]["properties"]["schema"]["type"], "string")
        self.assertEqual(manifest["$defs"]["chunk"]["properties"]["kind"]["type"], "string")
        self.assertEqual(manifest["$defs"]["manifest"]["properties"]["kind"]["const"], "manifest")
        for name in ("streams", "cycle", "producer"):
            self.assertIn(name, manifest["$defs"]["manifest"]["properties"])
        self.assertEqual(receipt["properties"]["schema"]["const"], "OWP-WAVE-ACK/1.0")
        self.assertEqual(receipt["$defs"]["uuid"]["maxLength"], 36)


if __name__ == "__main__":
    unittest.main()
