"""Exact-byte offline capture tests using the synthetic wire fixture."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from backend.waveforms.errors import ContractError
from backend.waveforms.files import iter_file_frames, scan_file
from tests.waveform_vectors import golden_capture


class WaveformFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.chunks = golden_capture()
        cls.contents = b"\n".join((cls.manifest, *cls.chunks))

    def assert_rejected(self, contents, code):
        with self.assertRaises(ContractError) as caught:
            scan_file(BytesIO(contents))
        self.assertEqual(caught.exception.code, code)

    def test_file_lines_are_exact(self):
        source = BytesIO(self.contents)
        result = scan_file(source)
        self.assertEqual(result.sha256, sha256(self.contents).hexdigest())
        self.assertEqual(result.manifest_sha256, sha256(self.manifest).hexdigest())
        self.assertEqual(result.size_bytes, len(self.contents))
        self.assertFalse(result.final_lf)
        self.assertEqual(len(result.lines), 1 + len(self.chunks))
        self.assertEqual((result.lines[0].offset, result.lines[0].length, result.lines[0].sha256),
                         (0, len(self.manifest), sha256(self.manifest).hexdigest()))
        self.assertEqual(tuple(iter_file_frames(source)), (self.manifest, *self.chunks))
        self.assertEqual(source.tell(), len(self.contents))
        self.assertEqual(tuple(iter_file_frames(source)), (self.manifest, *self.chunks))

    def test_final_lf_changes_file_hash_not_capture(self):
        with_lf = scan_file(BytesIO(self.contents + b"\n"))
        without_lf = scan_file(BytesIO(self.contents))
        self.assertNotEqual(with_lf.sha256, without_lf.sha256)
        self.assertEqual(with_lf.manifest_sha256, without_lf.manifest_sha256)
        self.assertTrue(with_lf.final_lf)
        self.assertEqual(with_lf.size_bytes, without_lf.size_bytes + 1)
        self.assertEqual(tuple(iter_file_frames(BytesIO(self.contents))), (self.manifest, *self.chunks))

    def test_file_bounds(self):
        self.assert_rejected(b"", "contract_invalid")
        self.assert_rejected(b"\xef\xbb\xbf" + self.contents, "json_invalid")
        self.assert_rejected(self.contents.replace(b"\n", b"\r\n", 1), "json_invalid")
        self.assert_rejected(self.manifest + b"\n\n" + b"\n".join(self.chunks), "json_invalid")
        self.assert_rejected(self.manifest + b"\n" + self.chunks[0] + b"\n" + self.chunks[0],
                             "layout_invalid")
        self.assert_rejected(self.manifest + b"\n" + b"\n".join(self.chunks[:-1]), "layout_invalid")
        self.assert_rejected(self.manifest + b"\n" + self.chunks[0][:-1], "json_invalid")
        self.assert_rejected(b"PK\x03\x04" + self.contents, "json_invalid")
        self.assert_rejected(self.manifest + b" " * (65_537 - len(self.manifest)), "payload_too_large")
        self.assert_rejected(self.contents + b"\n" + b" " * 262_145, "payload_too_large")

    def test_reordered_chunks_validate_and_preserve_file_order(self):
        reordered = (self.manifest, *reversed(self.chunks))
        source = BytesIO(b"\n".join(reordered))
        result = scan_file(source)
        self.assertEqual(len(result.lines), len(reordered))
        self.assertEqual(tuple(iter_file_frames(source)), reordered)

    def test_rejects_foreign_capture_and_wrong_chunk_layout(self):
        chunk = json.loads(self.chunks[0])
        chunk["capture_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        foreign = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
        self.assert_rejected(self.manifest + b"\n" + foreign + b"\n" + b"\n".join(self.chunks[1:]),
                             "reference_conflict")
        chunk = json.loads(self.chunks[0])
        chunk["sample_start"] += 1
        wrong_start = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
        self.assert_rejected(self.manifest + b"\n" + wrong_start + b"\n" + b"\n".join(self.chunks[1:]),
                             "layout_invalid")

    def test_rejects_stream_digest_mismatch(self):
        manifest = json.loads(self.manifest)
        manifest["streams"][0]["data_sha256"] = "0" * 64
        changed = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        changed_digest = sha256(changed).hexdigest()
        reissued_chunks = []
        for raw in self.chunks:
            chunk = json.loads(raw)
            chunk["manifest_sha256"] = changed_digest
            reissued_chunks.append(json.dumps(chunk, separators=(",", ":")).encode("utf-8"))
        self.assert_rejected(changed + b"\n" + b"\n".join(reissued_chunks), "digest_mismatch")

    def test_rejects_oversized_file_before_line_scan(self):
        self.assert_rejected(b" " * (33_554_432 + 514), "payload_too_large")

    def test_iterator_rejects_more_than_513_lines(self):
        source = BytesIO((b"{}\n" * 514))
        with self.assertRaises(ContractError) as caught:
            tuple(iter_file_frames(source))
        self.assertEqual(caught.exception.code, "layout_invalid")

    def test_rejects_unseekable_source(self):
        class Unseekable(BytesIO):
            def seekable(self):
                return False

        with self.assertRaises(ContractError) as caught:
            scan_file(Unseekable(self.contents))
        self.assertEqual(caught.exception.code, "contract_invalid")

    def test_cli_success_and_rejection(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "validate_waveform.py"
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "good.owpwave"
            valid.write_bytes(self.contents)
            success = subprocess.run([sys.executable, str(script), "--input", str(valid)],
                                     capture_output=True, text=True, check=False)
            self.assertEqual(success.returncode, 0, success.stderr)
            result = json.loads(success.stdout)
            self.assertEqual(result["sha256"], sha256(self.contents).hexdigest())
            self.assertEqual(result["manifest_sha256"], sha256(self.manifest).hexdigest())
            self.assertEqual(result["lines"], len(self.chunks) + 1)
            invalid = Path(directory) / "bad.owpwave"
            invalid.write_bytes(self.contents[:-1])
            rejected = subprocess.run([sys.executable, str(script), "--input", str(invalid)],
                                      capture_output=True, text=True, check=False)
            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertEqual(json.loads(rejected.stdout)["ok"], False)
            reordered = Path(directory) / "reordered.owpwave"
            reordered.write_bytes(b"\n".join((self.manifest, *reversed(self.chunks))))
            accepted = subprocess.run([sys.executable, str(script), "--input", str(reordered)],
                                      capture_output=True, text=True, check=False)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            overlong = Path(directory) / "overlong.owpwave"
            overlong.write_bytes(self.manifest + b"\n" + b" " * 262_145)
            too_large = subprocess.run([sys.executable, str(script), "--input", str(overlong)],
                                       capture_output=True, text=True, check=False)
            self.assertEqual(too_large.returncode, 2, too_large.stderr)
            self.assertEqual(json.loads(too_large.stdout)["reason_code"], "payload_too_large")


if __name__ == "__main__":
    unittest.main()
