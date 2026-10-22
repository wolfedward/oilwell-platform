"""The v1 approved model must not be silently redefined by new v2 code."""
from hashlib import sha256
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V1SealTests(unittest.TestCase):
    def test_v1_files_match_seal(self):
        seal = ROOT/'tests/fixtures/inversion_v1_seal.json'
        self.assertTrue(seal.is_file(), 'v1 baseline content seal not captured')
        value = json.loads(seal.read_bytes())
        self.assertEqual(value['baseline'], 'f779f96626b233136427c2f0982ef25e15afa66b')
        self.assertEqual(len(value['files']), 12)
        for name, expected in value['files'].items():
            with self.subTest(file=name):
                self.assertEqual(sha256((ROOT/name).read_bytes()).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
