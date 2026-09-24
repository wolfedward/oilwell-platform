"""Evidence qualification must not turn short, empty or failed runs into a pass."""
import unittest

from tools.verify_ingest_reliability import qualify


class VerificationEvidenceTests(unittest.TestCase):
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
