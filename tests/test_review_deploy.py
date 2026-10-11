"""Owned review deployment boundaries. All tests use disposable paths only."""
import tempfile
import unittest
from pathlib import Path

from tools.review_deploy_paths import claim_target, verify_target


class TargetTests(unittest.TestCase):
    def test_existing_target_is_never_adopted_or_removed(self):
        with tempfile.TemporaryDirectory() as parent:
            target = Path(parent) / 'existing'
            target.mkdir()
            sentinel = target / 'keep.txt'
            sentinel.write_text('user data', encoding='utf-8')
            with self.assertRaises(ValueError):
                claim_target(target, forbidden=(Path(parent),))
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'user data')

    def test_linked_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as parent:
            actual = Path(parent) / 'actual'
            actual.mkdir()
            linked = Path(parent) / 'linked'
            try:
                linked.symlink_to(actual, target_is_directory=True)
            except OSError:
                import subprocess
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(actual)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode:
                    self.skipTest('Directory link creation unavailable')
            with self.assertRaises(ValueError):
                claim_target(linked / 'new', forbidden=())

    def test_marker_binds_expected_instance_and_ports(self):
        with tempfile.TemporaryDirectory() as parent:
            target = Path(parent) / 'new'
            marker = claim_target(target, forbidden=())
            marker['ports'] = {'http': 8770, 'postgres': 55432, 'mqtt': 1884}
            from tools.review_deploy_paths import write_json_private
            write_json_private(target / 'private' / 'marker.json', marker)
            self.assertEqual(verify_target(target, expected_ports=marker['ports'])['instance_id'], marker['instance_id'])
            with self.assertRaises(ValueError):
                verify_target(target, expected_ports={'http': 8771, 'postgres': 55432, 'mqtt': 1884})


class ConfigurationTests(unittest.TestCase):
    def test_generated_configs_load_with_distinct_review_roles(self):
        from tools.review_deploy_config import write_configs, runtime_secrets
        from backend.center.config import load_center_config
        from backend.registry.config import load_registry_config
        from backend.ingest_reliability.config import load_reliability_config
        from backend.waveforms.config import load_waveform_config
        from backend.inversion.config import load_inversion_config
        import os
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            (root / 'private').mkdir()
            (root / 'private' / 'inversion-staging').mkdir()
            (root / 'private' / 'identity.db').write_bytes(b'placeholder')
            secrets = {name: 'different-secret-' + name for name in runtime_secrets()}
            paths = write_configs(root, ports={'http': 8770, 'postgres': 55432, 'mqtt': 1884},
                                  credentials=secrets, source_ref='126a2c57-2dca-4902-89ed-90c53a2a1355')
            old = os.environ.copy()
            os.environ.update({f'REVIEW_{name.upper()}_PASSWORD': value for name, value in secrets.items()})
            try:
                center = load_center_config(paths['center'])
                registry = load_registry_config(paths['center'])
                reliability = load_reliability_config(paths['center'], component='management')
                waveform = load_waveform_config(paths['waveform'], component='management')
                inversion = load_inversion_config(paths['inversion'])
                self.assertEqual(center.postgres['port'], 55432)
                self.assertEqual(len({center.postgres['user'], registry.writer['user'], registry.reader['user']}), 3)
                self.assertTrue(reliability.enabled)
                self.assertIn('receiver', waveform.postgres)
                self.assertEqual(inversion.identity_db_path, root / 'private' / 'identity.db')
            finally:
                os.environ.clear()
                os.environ.update(old)


class SourceTests(unittest.TestCase):
    def test_dirty_release_source_is_rejected_before_export(self):
        from tools.review_deploy_source import export_committed_source
        import subprocess
        with tempfile.TemporaryDirectory() as parent:
            repo = Path(parent) / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'server.py').write_text('print(1)\n', encoding='utf-8')
            subprocess.run(['git', '-C', str(repo), 'add', 'server.py'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=t@example.test',
                            'commit', '-qm', 'base'], check=True)
            (repo / 'server.py').write_text('print(2)\n', encoding='utf-8')
            destination = Path(parent) / 'code'
            with self.assertRaises(ValueError):
                export_committed_source(repo, destination)
            self.assertFalse(destination.exists())

    def test_export_contains_committed_bytes_and_hashes(self):
        from tools.review_deploy_source import export_committed_source
        import hashlib
        import subprocess
        with tempfile.TemporaryDirectory() as parent:
            repo = Path(parent) / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'server.py').write_text('print(1)\n', encoding='utf-8')
            (repo / 'requirements-production.txt').write_text('psycopg==3.3.5\n', encoding='utf-8')
            (repo / 'private.key').write_text('omit', encoding='utf-8')
            subprocess.run(['git', '-C', str(repo), 'add', 'server.py', 'requirements-production.txt', 'private.key'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=t@example.test',
                            'commit', '-qm', 'base'], check=True)
            result = export_committed_source(repo, Path(parent) / 'code')
            self.assertEqual((Path(parent) / 'code' / 'server.py').read_bytes(), b'print(1)\n')
            self.assertFalse((Path(parent) / 'code' / 'private.key').exists())
            self.assertEqual(result['files']['server.py'], hashlib.sha256(b'print(1)\n').hexdigest())


class MigrationOrderTests(unittest.TestCase):
    def test_first_waveform_migration_follows_exact_registry_projection_grants(self):
        from tools.review_deploy_pg import migration_steps, run_migration_steps
        visited = []
        run_migration_steps({name: (lambda key=name: visited.append(key)) for name in migration_steps()})
        self.assertLess(visited.index('registry_pregrant'), visited.index('waveform_migrate'))
        self.assertLess(visited.index('registry_migrate'), visited.index('registry_pregrant'))
        self.assertGreater(visited.index('registry_history_grant'), visited.index('waveform_migrate'))
        self.assertGreater(visited.index('inversion_metadata_grant'), visited.index('inversion_grants'))


class ProcessOwnershipTests(unittest.TestCase):
    def test_same_pid_with_different_creation_or_command_is_not_owned(self):
        from tools.review_deploy_process import owned_process
        expected = {'pid': 1842, 'created': '2026-10-11T12:00:00',
                    'executable': r'C:\review\venv\Scripts\python.exe',
                    'command': r'C:\review\venv\Scripts\python.exe C:\review\code\server.py --runtime-mode=review'}
        self.assertTrue(owned_process(expected, dict(expected)))
        actual = dict(expected, created='2026-10-11T12:01:00')
        self.assertFalse(owned_process(expected, actual))
        actual = dict(expected, command='python unrelated.py')
        self.assertFalse(owned_process(expected, actual))


class BackupTests(unittest.TestCase):
    def test_backup_manifest_rejects_tampered_dump_and_identity(self):
        from tools.review_deploy_backup import seal_backup, verify_backup
        with tempfile.TemporaryDirectory() as parent:
            folder = Path(parent)
            (folder / 'database.dump').write_bytes(b'PGDMP\x00example')
            (folder / 'identity.sqlite').write_bytes(b'SQLite format 3\x00example')
            (folder / 'accounts.json').write_text('{"accounts":[]}', encoding='utf-8')
            seal_backup(folder, instance_id='12345678-1234-1234-1234-123456789abc',
                        source={'commit': 'a' * 40, 'files': {'server.py': 'b' * 64}},
                        system_id='987654321', source_ref='126a2c57-2dca-4902-89ed-90c53a2a1355')
            self.assertEqual(verify_backup(folder)['system_id'], '987654321')
            (folder / 'database.dump').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verify_backup(folder)


class CommandSafetyTests(unittest.TestCase):
    def test_initialize_refuses_existing_target_before_any_provisioning(self):
        from tools.review_deploy import initialize
        with tempfile.TemporaryDirectory() as parent:
            target = Path(parent) / 'existing'
            target.mkdir()
            sentinel = target / 'keep.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with self.assertRaises(ValueError):
                initialize(target, source=Path(parent), ports={'http': 8770, 'postgres': 55432, 'mqtt': 1884})
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')

    def test_restore_refuses_existing_target_before_reading_backup(self):
        from tools.review_deploy import restore
        with tempfile.TemporaryDirectory() as parent:
            target = Path(parent) / 'existing'
            target.mkdir()
            with self.assertRaises(ValueError):
                restore(Path(parent) / 'missing-backup', target=target, source=Path(parent),
                        ports={'http': 8771, 'postgres': 55433, 'mqtt': 1885})


if __name__ == '__main__':
    unittest.main()
