"""Evidence qualification must not turn short, empty or failed runs into a pass."""
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import verify_ingest_reliability as verifier
from tools.verify_ingest_reliability import qualify


class VerificationEvidenceTests(unittest.TestCase):
    def test_preexisting_frozen_source_drift_cannot_pass_unchanged_snapshot(self):
        # Real temporary files and ZIP: an identical before/after snapshot must
        # not conceal source drift that predates this particular experiment.
        with tempfile.TemporaryDirectory(prefix='wp03-baseline-') as directory:
            root = Path(directory)
            frozen = root / 'frozen'
            frozen.mkdir()
            source = frozen / 'source.txt'
            source.write_bytes(b'original source')
            archive_path = root / 'baseline.zip'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('source.txt', b'original source')
                archive.writestr('.superpowers/another-plan/private.txt', b'not ours to read')
            with patch.multiple(verifier, FROZEN=frozen, FROZEN_ZIP=archive_path,
                                ZIP_SHA256=sha256(archive_path.read_bytes()).hexdigest()), \
                    patch.object(verifier, 'git', side_effect=lambda *args, **kw:
                                 verifier.TAG_COMMIT if args[0] == 'rev-parse' else '?? scripts/'):
                self.assertTrue(verifier.protected_snapshot()['source_matches_archive'])
                source.write_bytes(b'already modified before this experiment')
                with self.assertRaisesRegex(AssertionError, 'baseline'):
                    verifier.protected_snapshot()

    def values(self):
        return {'duration_seconds': 1800.1, 'messages': 800, 'bytes': 400000, 'stored': 800,
                'duplicates_in_database': 0, 'restarts': 12, 'pending': 0,
                'exact_payloads': True, 'capacity_exhausted': False}

    def test_nonempty_complete_measured_soak_qualifies(self):
        self.assertEqual(qualify(self.values(), {'testsRun': 14, 'failures': 0, 'errors': 0, 'skipped': 0}, True), [])

    def test_short_empty_incomplete_or_capacity_failed_run_never_qualifies(self):
        for changes in ({'duration_seconds': 1799.99}, {'messages': 0}, {'bytes': 0}, {'stored': 799},
                        {'duplicates_in_database': 1}, {'restarts': 9}, {'pending': 1},
                        {'exact_payloads': False}, {'capacity_exhausted': True}):
            with self.subTest(changes=changes):
                self.assertTrue(qualify(self.values() | changes,
                                       {'testsRun': 14, 'failures': 0, 'errors': 0, 'skipped': 0}, True))

    def test_test_failure_error_skip_no_tests_or_protected_change_blocks_pass(self):
        for counts in ({'testsRun': 0}, {'failures': 1}, {'errors': 1}, {'skipped': 1}):
            with self.subTest(counts=counts):
                self.assertTrue(qualify(self.values(),
                                       {'testsRun': 14, 'failures': 0, 'errors': 0, 'skipped': 0} | counts, True))
        self.assertTrue(qualify(self.values(), {'testsRun': 14, 'failures': 0, 'errors': 0, 'skipped': 0}, False))
