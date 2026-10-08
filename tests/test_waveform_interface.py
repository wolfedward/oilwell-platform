"""Public waveform validator agrees with the center on original bytes."""

import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from backend.waveforms.errors import ContractError
from backend.waveforms.files import scan_file
from tests.test_waveform_browser import vectors


ROOT = Path(__file__).resolve().parents[1]
KIT = Path(os.environ.get("OILWELL_WAVEFORM_PUBLIC_ROOT", ROOT / "assets/open-interface"))
SIBLINGS = ("validate_waveform.py", "waveform_errors.py", "waveform_contracts.py",
            "waveform_wire.py", "waveform_samples.py", "waveform_files.py")


def outcome(raw):
    try:
        result = scan_file(BytesIO(raw))
        return {"ok": True, "sha256": result.sha256,
                "manifest_sha256": result.manifest_sha256,
                "size_bytes": result.size_bytes, "final_lf": result.final_lf,
                "lines": len(result.lines), "capture_id": str(result.manifest.key.capture_id)}
    except ContractError as error:
        return {"ok": False, "reason_code": error.code}


class WaveformInterfaceTests(unittest.TestCase):
    def test_public_validator_matches_center(self):
        shared = vectors()
        self.assertEqual(len(shared), 40)
        by_name = {row["name"]: row for row in shared}
        self.assertTrue(outcome(base64.b64decode(by_name["count-minus-zero"]["base64"]))["ok"])
        self.assertEqual(outcome(base64.b64decode(by_name["fraction-count"]["base64"]))["reason_code"], "contract_invalid")
        self.assertEqual(outcome(base64.b64decode(by_name["duplicate-key"]["base64"]))["reason_code"], "json_duplicate_key")
        for name in ('positive-infinity', 'negative-infinity', 'nan-sample'):
            self.assertFalse(by_name[name]['accepted'])
            self.assertEqual(by_name[name]['reason_code'], 'layout_invalid')
            self.assertTrue(by_name[name+'-i32']['accepted'])
        with tempfile.TemporaryDirectory(prefix="standalone-waveform-") as folder:
            isolated = Path(folder)
            for name in SIBLINGS:
                shutil.copyfile(KIT / name, isolated / name)
            for row in shared:
                name, raw = row["name"], base64.b64decode(row["base64"])
                with self.subTest(name=name):
                    path = isolated / f"{name}.owpwave"
                    path.write_bytes(raw)
                    completed = subprocess.run([sys.executable, "-I", str(isolated / "validate_waveform.py"),
                                                "--input", str(path)], cwd=isolated, capture_output=True,
                                               timeout=30,
                                               env={key: os.environ.get(key, "") for key in
                                                    ("PATH", "SYSTEMROOT", "WINDIR")})
                    expected = outcome(raw)
                    self.assertEqual(row["accepted"], expected["ok"])
                    if expected["ok"]:
                        self.assertEqual(row["hash"], expected["sha256"])
                        self.assertEqual(row["lines"], expected["lines"])
                        self.assertEqual(row["final_lf"], expected["final_lf"])
                    self.assertEqual(completed.returncode, 0 if expected["ok"] else 2, completed.stderr)
                    self.assertEqual(json.loads(completed.stdout), expected)

    def test_public_example_is_small_synthetic_and_exact(self):
        raw = (KIT / "waveform-example.owpwave").read_bytes()
        self.assertLessEqual(len(raw), 32 * 1024)
        result = scan_file(BytesIO(raw))
        self.assertEqual(result.sha256, sha256(raw).hexdigest())
        self.assertEqual(result.manifest.body["source_kind"], "synthetic")
        self.assertTrue(all(channel.measurement_source == "synthetic"
                            for stream in result.manifest.streams for channel in stream.channels))


if __name__ == "__main__":
    unittest.main()
