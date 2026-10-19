"""Real file-system boundaries; tampering must never become a trusted pack."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.testpack.config import parse_config
from tools.testpack.contracts import Artifact, Case
from tools.testpack.storage import (canonical_bytes, check_budget, derive_uniform,
                                    load_pack, strict_json, write_pack)


CONFIG = {'schema': 'owp.testpack.config/1', 'profile': 'smoke', 'seed': 20261019,
          'start_utc': '2026-09-30T16:00:00.000000Z', 'timezone': 'Asia/Shanghai',
          'max_bytes': 268435456}
GENERATOR = {'version': 'owp.testpack.generator/1', 'source_commit': 'a'*40,
             'source_sha256': 'b'*64, 'python_version': '3.13.2', 'rng': 'sha256-counter/1'}


def example(path='data/中文.json', content=b'{"value":1}\n'):
    return Case('case-1', 'mechanistic', (Artifact(path, 'application/json', content),),
                {'source': 'synthetic'}, ())


class TestPackContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='油井 测试 ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = parse_config(CONFIG)

    def write(self, name='pack', cases=None, config=None):
        return write_pack(self.root/name, config or self.config,
                          cases or (example(),), GENERATOR)

    def test_same_inputs_same_manifest_and_files(self):
        a, b = self.write('a'), self.write('b')
        self.assertEqual(a.manifest, b.manifest)
        self.assertEqual({p.relative_to(a.root).as_posix(): p.read_bytes()
                          for p in a.root.rglob('*') if p.is_file()},
                         {p.relative_to(b.root).as_posix(): p.read_bytes()
                          for p in b.root.rglob('*') if p.is_file()})
        self.assertEqual(load_pack(a.root).manifest, a.manifest)

    def test_changed_seed_changes_pack_identity(self):
        a = self.write('a')
        b = self.write('b', config=parse_config({**CONFIG, 'seed': CONFIG['seed']+1}))
        self.assertNotEqual(a.manifest['pack_id'], b.manifest['pack_id'])

    def test_target_ownership_and_member_paths(self):
        self.write()
        before = (self.root/'pack/manifest.json').read_bytes()
        with self.assertRaises(ValueError):
            self.write()
        self.assertEqual((self.root/'pack/manifest.json').read_bytes(), before)
        paths = ['../x', '/x', 'a//x', 'a/./x', 'a/../x', 'a\\x', 'C:x',
                 'NUL.txt', 'a/COM1', 'a/trailing.', 'a/trailing ', 'manifest.json', '']
        for i, path in enumerate(paths):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.write(str(i), (example(path),))
            self.assertFalse((self.root/str(i)).exists())
        with self.assertRaises(ValueError):
            self.write('duplicate', (example('data/A'), Case('case-2', 'mechanistic',
                       (Artifact('data/a', 'text/plain', b'x'),), {}, ())))

    def test_hardlinked_member_rejected(self):
        pack = self.write()
        p = pack.root/'data/中文.json'
        os.link(p, self.root/'alias')
        with self.assertRaises(ValueError):
            load_pack(pack.root)

    def test_symlink_ancestor_rejected(self):
        real = self.root/'real'
        real.mkdir()
        try:
            os.symlink(real, self.root/'link', target_is_directory=True)
        except OSError:
            self.skipTest('Host does not allow unprivileged symlinks; junction tested separately')
        with self.assertRaises(ValueError):
            write_pack(self.root/'link/new', self.config, (example(),), GENERATOR)
        self.assertFalse((real/'new').exists())

    def test_nonfinite_duplicate_keys_and_total_budget(self):
        for value in [float('nan'), float('inf'), -float('inf')]:
            with self.assertRaises(ValueError):
                canonical_bytes({'x': value})
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}']:
            with self.assertRaises(ValueError):
                strict_json(raw)
        with self.assertRaises(ValueError):
            check_budget([268435457], 268435456)
        with self.assertRaises(ValueError):
            self.write(config=parse_config({**CONFIG, 'max_bytes': 1}))
        self.assertFalse((self.root/'pack').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction semantics')
    def test_windows_junction_ancestor_rejected(self):
        real, junction = self.root/'real', self.root/'junction'
        real.mkdir()
        command = 'New-Item -ItemType Junction -Path $env:TESTPACK_JUNCTION -Target $env:TESTPACK_REAL | Out-Null'
        # -Command does not bind trailing strings to $args; pass literal data via env.
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                                 env={**os.environ, 'TESTPACK_JUNCTION': str(junction),
                                      'TESTPACK_REAL': str(real)}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(ValueError):
                write_pack(junction/'new', self.config, (example(),), GENERATOR)
            self.assertFalse((real/'new').exists())
        finally:
            if junction.exists():
                junction.rmdir()  # Remove only the test-owned junction, not its target.

    def test_self_consistent_digest_does_not_bypass_schema_or_budget(self):
        for index, mutate in enumerate([
            lambda m: m.update(untrusted='x'),
            lambda m: m['artifacts'][0].update(size_bytes=268435457),
            lambda m: m['artifacts'][0].update(path='../outside'),
            lambda m: m['source'].update(kind='field'),
        ]):
            pack = self.write(str(index))
            value = json.loads((pack.root/'manifest.json').read_bytes())
            mutate(value)
            raw = canonical_bytes(value)
            (pack.root/'manifest.json').write_bytes(raw)
            (pack.root/'manifest.sha256').write_bytes((hashlib.sha256(raw).hexdigest()+'\n').encode())
            with self.assertRaises(ValueError):
                load_pack(pack.root)

    def test_config_rejects_unknown_bool_seed_and_naive_time(self):
        for change in [{'seed': True}, {'seed': -1}, {'extra': 1},
                       {'start_utc': '2026-01-01'}, {'timezone': 'UTC'},
                       {'max_bytes': 268435457}, {'profile': 'production'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_config({**CONFIG, **change})

    def test_tampered_bytes_missing_and_unlisted_files_refused(self):
        p = self.write()
        (p.root/'data/中文.json').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            load_pack(p.root)
        p = self.write('other')
        (p.root/'unexpected.txt').write_bytes(b'not in manifest')
        with self.assertRaises(ValueError):
            load_pack(p.root)

    def test_manifest_tampering_and_duplicate_json_keys_refused(self):
        p = self.write()
        original = (p.root/'manifest.json').read_bytes()
        (p.root/'manifest.json').write_bytes(original.replace(b'"profile":', b'"profile":"x","profile":'))
        with self.assertRaises(ValueError):
            load_pack(p.root)

    def test_expected_checks_are_data_not_executable_expressions(self):
        bad = Case('case-1', 'mechanistic', (), {}, ({'check_id': 'bad', 'layer': 'offline',
                     'assertion': 'eval', 'target': 'x', 'expected': '__import__("os")'},))
        with self.assertRaises(ValueError):
            self.write(cases=(bad,))

    def test_canonical_utf8_lf_and_counter_vector(self):
        self.assertEqual(canonical_bytes({'z': 1, 'a': '井'}), '{"a":"井","z":1}\n'.encode())
        raw = b'[7,"cycle-base","power",0]\n'
        want = int.from_bytes(hashlib.sha256(raw).digest()[:8], 'big') / 2**64
        self.assertEqual(derive_uniform(7, 'cycle-base', 'power', 0), want)
        self.assertNotEqual(want, derive_uniform(7, 'cycle-base', 'power', 1))


if __name__ == '__main__':
    unittest.main()
