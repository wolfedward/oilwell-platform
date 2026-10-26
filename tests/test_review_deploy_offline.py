"""Offline installer gates use artificial wheel fixtures and disposable targets.

Fixture digests replace the publisher registry only inside bundle parsing tests;
they are not accepted installation evidence. Actual installation is a separate
new-owned-target check using the unchanged publisher registry.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

REPO = Path(__file__).resolve().parents[1]


def wheel_bytes(spec, *, version=None, tags=None):
    buffer = io.BytesIO()
    folder = spec.filename.split('-')[0] + '-' + spec.version + '.dist-info'
    with ZipFile(buffer, 'w') as archive:
        archive.writestr(folder + '/METADATA',
                        f'Metadata-Version: 2.1\nName: {spec.name}\nVersion: {version or spec.version}\n')
        archive.writestr(folder + '/WHEEL', 'Wheel-Version: 1.0\n' +
                        ''.join('Tag: ' + tag + '\n' for tag in (tags or spec.tags)))
    return buffer.getvalue()


@contextmanager
def bundle_fixture(module, root):
    root.mkdir()
    specs = []
    for spec in module.WHEELS:
        data = wheel_bytes(spec)
        (root / spec.filename).write_bytes(data)
        specs.append(replace(spec, sha256=sha256(data).hexdigest()))
    with patch.object(module, 'WHEELS', tuple(specs)):
        yield root


def owned_fixture(root):
    from tools.review_deploy_paths import claim_target, write_json_private
    marker = claim_target(root, forbidden=())
    (root / 'code').mkdir(); (root / 'logs').mkdir()
    hashes = {}
    for name in ('requirements-production.txt', 'requirements-inversion-v2.txt'):
        data = (REPO / name).read_bytes()
        (root / 'code' / name).write_bytes(data); hashes[name] = sha256(data).hexdigest()
    marker.update(source={'commit': 'a' * 40, 'files': hashes}, inversion_v2=True)
    write_json_private(root / 'private/marker.json', marker)
    return marker


class OfflineBundleTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.review_deploy_offline'),
                             'strict offline installer missing')
        return importlib.import_module('tools.review_deploy_offline')

    def test_complete_bundle_returns_verified_original_bytes(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as root:
            found = module.read_bundle(root)
            self.assertEqual(len(found), 6)
            self.assertEqual(found['paho_mqtt-2.1.0-py3-none-any.whl'],
                             (root / 'paho_mqtt-2.1.0-py3-none-any.whl').read_bytes())

    def test_missing_extra_and_wrong_filename_refused(self):
        module = self.module()
        for change in ('missing', 'extra', 'rename'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp, \
                 bundle_fixture(module, Path(temp) / 'wheels') as root:
                item = root / module.WHEELS[0].filename
                if change == 'missing': item.unlink()
                elif change == 'extra': (root / 'unapproved.whl').write_bytes(b'extra')
                else: item.rename(root / 'wrong.whl')
                with self.assertRaises(ValueError): module.read_bundle(root)

    def test_each_tampered_package_and_hardlink_refused(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as root:
            for spec in module.WHEELS:
                item = root / spec.filename
                original = item.read_bytes(); item.write_bytes(original + b'tamper')
                with self.subTest(spec=spec.name), self.assertRaises(ValueError): module.read_bundle(root)
                item.write_bytes(original)
            os.link(root / module.WHEELS[0].filename, Path(temp) / 'alias')
            with self.assertRaises(ValueError): module.read_bundle(root)

    def test_trusted_digest_does_not_replace_metadata_version_or_abi_check(self):
        module = self.module()
        for change in ({'version': '0.0.0'}, {'tags': ('cp312-cp312-win_amd64',)}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp, \
                 bundle_fixture(module, Path(temp) / 'wheels') as root:
                specs = list(module.WHEELS); payload = wheel_bytes(specs[-1], **change)
                (root / specs[-1].filename).write_bytes(payload)
                specs[-1] = replace(specs[-1], sha256=sha256(payload).hexdigest())
                with patch.object(module, 'WHEELS', tuple(specs)), self.assertRaises(ValueError):
                    module.read_bundle(root)

    def test_source_drift_refused_but_crlf_and_comments_allowed(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            code = Path(temp)
            for name in ('requirements-production.txt', 'requirements-inversion-v2.txt'):
                (code / name).write_bytes((REPO / name).read_bytes())
            module.check_source(code)
            production = code / 'requirements-production.txt'
            production.write_text('# checked\n' + production.read_text().replace('\n', '\r\n'), newline='')
            module.check_source(code)
            for text in ('paho-mqtt==2.1.0\n', (REPO / production.name).read_text() + '\n--extra-index-url https://example.invalid\n',
                         (REPO / production.name).read_text().replace('[binary]', '')):
                production.write_text(text)
                with self.assertRaises(ValueError): module.check_source(code)

    def test_target_python_refuses_wrong_architecture_version_or_gil(self):
        module = self.module()
        good = {'version': '3.13.2', 'implementation': 'CPython', 'system': 'Windows',
                'bits': 64, 'gil': 1, 'platform': 'win-amd64'}
        with patch.object(module.subprocess, 'check_output', return_value=json.dumps(good).encode()):
            self.assertEqual(module.probe_target(Path('python.exe')), good)
        for key, value in (('version', '3.13.3'), ('platform', 'win-arm64'), ('bits', 32), ('gil', 0)):
            with self.subTest(key=key), patch.object(module.subprocess, 'check_output',
                    return_value=json.dumps(dict(good, **{key: value})).encode()), self.assertRaises(ValueError):
                module.probe_target(Path('python.exe'))

    def test_pip_failure_has_no_network_fallback_or_inherited_configuration(self):
        module = self.module(); calls = []
        def fail(argv, **kwargs):
            calls.append((argv, kwargs))
            raise subprocess.CalledProcessError(1, argv)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,
                {'PIP_INDEX_URL': 'https://invalid.example', 'PIP_TARGET': 'other', 'PYTHONPATH': 'other'}), \
             patch.object(module.subprocess, 'run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                module.run_pip(Path('python.exe'), ['install', '--no-index'], Path(temp) / 'pip.log')
        self.assertEqual(len(calls), 1)
        args, options = calls[0]
        self.assertEqual(args[1:5], ['-I', '-B', '-m', 'pip'])
        self.assertIn('--isolated', args); self.assertIn('--no-cache-dir', args)
        self.assertEqual(options['env']['PIP_CONFIG_FILE'], os.devnull)
        self.assertNotIn('PIP_INDEX_URL', options['env']); self.assertNotIn('PIP_TARGET', options['env'])
        self.assertNotIn('PYTHONPATH', options['env'])

    def test_invalid_bundle_cannot_claim_initialize_or_release_target(self):
        module = self.module()
        from tools import review_deploy
        from tools.testpack.deliver_v2 import deliver
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); wheels = root / 'missing'
            # No database, venv or service calls can occur before bundle admission.
            with patch('tools.review_deploy.require_free_ports'):
                with self.assertRaises(ValueError):
                    review_deploy.initialize(root / 'instance', source=REPO, offline_wheels=wheels)
            with patch('tools.testpack.deliver_v2._clean', return_value=REPO), \
                 patch('tools.testpack.deliver_v2.validate_lab_ports', return_value={}):
                with self.assertRaises(ValueError): deliver(root / 'release', REPO, profile='smoke', offline_wheels=wheels)
            self.assertFalse((root / 'instance').exists()); self.assertFalse((root / 'release').exists())

    def test_snapshot_uses_verified_bytes_and_binds_owner_source_and_lock(self):
        module = self.module()
        self.assertTrue(callable(getattr(module, 'stage_snapshot', None)), 'owned snapshot absent')
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as wheels:
            root = Path(temp) / 'owned'; marker = owned_fixture(root)
            with patch.object(module, 'probe_target', return_value=dict(module.TARGET)):
                snapshot = module.stage_snapshot(root, wheels, Path('python.exe'))
            admitted = module.verify_snapshot(root, snapshot)
            self.assertEqual(admitted['instance_id'], marker['instance_id'])
            self.assertEqual(admitted['source_commit'], 'a' * 40)
            lock = (snapshot / 'production.lock').read_text()
            self.assertIn('tzdata==2026.5 --hash=sha256:', lock)
            self.assertIn('psycopg[binary]==3.3.5 --hash=sha256:', lock)
            self.assertEqual(len(lock.splitlines()), 5)
            first = wheels / module.WHEELS[0].filename
            original = first.read_bytes(); first.write_bytes(b'changed upstream')
            self.assertEqual((snapshot / 'wheels' / first.name).read_bytes(), original)
            module.verify_snapshot(root, snapshot)
            with self.assertRaises(ValueError): module.stage_snapshot(root, wheels, Path('python.exe'))
            (snapshot / 'production.lock').write_text(lock + '\n--extra-index-url https://invalid.example\n')
            with self.assertRaises(ValueError): module.verify_snapshot(root, snapshot)

    def test_installed_versions_are_checked_before_success_receipt(self):
        module = self.module()
        self.assertTrue(callable(getattr(module, 'finish_install', None)), 'installation proof absent')
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as wheels:
            root = Path(temp) / 'owned'; owned_fixture(root)
            with patch.object(module, 'probe_target', return_value=dict(module.TARGET)):
                snapshot = module.stage_snapshot(root, wheels, Path('python.exe'))
                wrong = {w.name: w.version for w in module.WHEELS}; wrong['tzdata'] = '2020.1'
                original_output = subprocess.check_output
                def packages_output(packages):
                    return lambda argv, **kwargs: (json.dumps(packages).encode() if '-c' in argv
                                                  else original_output(argv, **kwargs))
                with patch.object(module.subprocess, 'check_output', side_effect=packages_output(wrong)), \
                     self.assertRaises(ValueError): module.finish_install(root, snapshot, inversion_v2=True)
                self.assertFalse((snapshot / 'installed.json').exists())
                right = {w.name: w.version for w in module.WHEELS}
                with patch.object(module.subprocess, 'check_output', side_effect=packages_output(right)):
                    module.finish_install(root, snapshot, inversion_v2=True)
            receipt = json.loads((snapshot / 'installed.json').read_text())
            self.assertEqual(receipt['packages'], right)
            self.assertEqual(receipt['network_fallback'], False)

    def test_production_install_uses_only_owned_hashed_closed_lock(self):
        module = self.module()
        self.assertTrue(callable(getattr(module, 'install_production', None)), 'offline pip install absent')
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as wheels:
            root = Path(temp) / 'owned'; owned_fixture(root)
            with patch.object(module, 'probe_target', return_value=dict(module.TARGET)):
                snapshot = module.stage_snapshot(root, wheels, Path('python.exe'))
            commands = []
            def record(argv, **kwargs): commands.append(argv)
            with patch.object(module.subprocess, 'run', side_effect=record):
                module.install_production(root, snapshot)
            install = commands[0]
            for flag in ('--no-index', '--no-deps', '--require-hashes', '--only-binary=:all:'):
                self.assertIn(flag, install)
            self.assertEqual(install[install.index('--find-links') + 1], str(snapshot / 'wheels'))
            self.assertEqual(install[install.index('-r') + 1], str(snapshot / 'production.lock'))
            self.assertEqual(commands[-1][-1], 'check')

    def test_offline_selection_reaches_numeric_installer_without_online_runner(self):
        module = self.module()
        from tools import review_deploy_numeric as numeric
        import inspect
        self.assertIn('offline_wheels', inspect.signature(numeric.install).parameters)
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as wheels:
            root = Path(temp) / 'owned'; owned_fixture(root)
            inventory = {'wheel_sha256': numeric.WHEEL_SHA256, 'native': {'a.pyd': 'a'*64},
                         'licenses': {'license': 'b'*64}}
            actual = {'approved_wheel_sha256': numeric.WHEEL_SHA256,
                      'native_files': [{'name': 'a.pyd', 'sha256': 'a'*64}],
                      'licenses': [{'name': 'license', 'sha256': 'b'*64}]}
            commands = []
            original_run = subprocess.run
            def pip_only(argv, **kwargs):
                if 'pip' in argv: commands.append(argv)
                else: return original_run(argv, **kwargs)
            with patch.object(numeric, 'probe_python', return_value={k: v for k, v in module.TARGET.items() if k != 'platform'}), \
                 patch.object(module, 'probe_target', return_value=dict(module.TARGET)), \
                 patch.object(numeric, 'inspect_wheel', return_value=inventory), \
                 patch.object(numeric, 'verify_installed'), patch.object(numeric, 'run_exported_backend', return_value=actual), \
                 patch.object(numeric, '_run', side_effect=AssertionError('online runner invoked')), \
                 patch.object(module.subprocess, 'run', side_effect=pip_only):
                numeric.install(root, offline_wheels=wheels)
            self.assertEqual(len(commands), 2)
            self.assertIn('--no-index', commands[0]); self.assertIn('--require-hashes', commands[0])
            self.assertEqual((root / 'private/inversion-v2/wheels' / numeric.WHEEL_NAME).read_bytes(),
                             (wheels / numeric.WHEEL_NAME).read_bytes())

    def test_direct_numeric_offline_entry_checks_architecture_before_writes(self):
        module = self.module()
        from tools import review_deploy_numeric as numeric
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as wheels:
            root = Path(temp) / 'owned'; owned_fixture(root)
            with patch.object(numeric, 'probe_python', return_value={k: v for k, v in module.TARGET.items() if k != 'platform'}), \
                 patch.object(module, 'probe_target', side_effect=ValueError('wrong platform')):
                with self.assertRaisesRegex(ValueError, 'wrong platform'): numeric.install(root, offline_wheels=wheels)
            self.assertFalse((root / 'private/inversion-v2').exists())

    def test_venv_propagates_owned_snapshot_and_does_not_fall_back(self):
        module = self.module()
        from tools import review_deploy as deploy
        import inspect
        self.assertIn('offline_wheels', inspect.signature(deploy._venv).parameters)
        with tempfile.TemporaryDirectory() as temp, bundle_fixture(module, Path(temp) / 'wheels') as wheels:
            root = Path(temp) / 'owned'; owned_fixture(root)
            original_run = subprocess.run
            calls = []
            def boundary(argv, **kwargs):
                if 'venv' in argv:
                    python = root / 'venv/Scripts/python.exe'; python.parent.mkdir(parents=True)
                    python.write_bytes(b'test executable placeholder')
                    return subprocess.CompletedProcess(argv, 0)
                if 'pip' in argv: calls.append(argv); return subprocess.CompletedProcess(argv, 0)
                return original_run(argv, **kwargs)
            received = []
            def numeric_install(target, *, offline_wheels=None):
                received.append(offline_wheels)
                raise RuntimeError('numeric gate failed')
            with patch.object(module, 'probe_target', return_value=dict(module.TARGET)), \
                 patch.object(module.subprocess, 'run', side_effect=boundary), \
                 patch('tools.review_deploy_numeric.install', side_effect=numeric_install):
                with self.assertRaisesRegex(RuntimeError, 'numeric gate failed'):
                    deploy._venv(root, offline_wheels=wheels)
            self.assertEqual(received, [root / 'private/offline-dependencies/wheels'])
            self.assertEqual(len(calls), 2)
            self.assertIn('--no-index', calls[0])
            self.assertFalse((root / 'private/offline-dependencies/installed.json').exists())

    def test_init_and_prepare_cli_preserve_explicit_offline_selection(self):
        self.module()
        from contextlib import redirect_stdout
        from tools import review_deploy as deploy
        from tools.testpack import deliver_v2
        import sys
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); selected = root / 'wheels'
            with patch.object(deploy, 'initialize', return_value={}) as initialize, redirect_stdout(io.StringIO()):
                self.assertEqual(deploy.main(['init', '--target', str(root / 'new'),
                                             '--offline-wheels', str(selected)]), 0)
            self.assertEqual(initialize.call_args.kwargs['offline_wheels'], selected)
            with patch.object(deliver_v2, 'deliver', return_value={}) as deliver, redirect_stdout(io.StringIO()), \
                 patch.object(sys, 'argv', ['deliver', 'prepare', '--root', str(root / 'new'),
                                           '--source', str(REPO), '--offline-wheels', str(selected)]):
                deliver_v2.main()
            self.assertEqual(deliver.call_args.kwargs['offline_wheels'], selected)

    def test_lab_keeps_offline_selection_for_child_before_any_db_operations(self):
        module = self.module()
        from types import SimpleNamespace
        from tools.testpack import lab
        import inspect
        self.assertIn('offline_wheels', inspect.signature(lab.initialize_lab).parameters)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); pack_root = root / 'pack'; pack_root.mkdir()
            (pack_root / 'manifest.json').write_text('{}')
            package = SimpleNamespace(root=pack_root, manifest={'pack_id': 'test-only',
                                      'configuration': {'schema': 'owp.testpack.config/2'}})
            selected = root / 'wheels'; calls = []
            def refuse(target, **kwargs):
                calls.append(kwargs)
                raise RuntimeError('stop before any child DB')
            with patch.object(lab, 'load_pack', return_value=package), \
                 patch.object(lab, 'validate_lab_ports', side_effect=lambda ports: ports), \
                 patch.object(module, 'preflight', return_value=dict(module.TARGET)), \
                 patch.object(lab.deploy, 'initialize', side_effect=refuse):
                with self.assertRaisesRegex(RuntimeError, 'stop before any child DB'):
                    lab.initialize_lab(root / 'new', pack=package, source=REPO, ports=lab.V2_PORTS,
                                       offline_wheels=selected)
            self.assertEqual(calls[0]['offline_wheels'], selected)


if __name__ == '__main__': unittest.main()
