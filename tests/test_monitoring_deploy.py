"""Monitoring deployment inventory and fail-closed profile boundaries."""
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools import review_deploy_config as config


class MonitoringDeploymentTests(unittest.TestCase):
    def test_profile_inventory_defaults_and_invalid_profile(self):
        self.assertEqual(config.pg_runtime(), config.PG_RUNTIME)
        self.assertEqual(config.mqtt_accounts(), config.MQTT_ACCOUNTS)
        self.assertEqual(config.pg_runtime('monitoring'), (*config.PG_RUNTIME, 'monitoring_reader'))
        self.assertEqual(config.mqtt_accounts('monitoring'), (*config.MQTT_ACCOUNTS, 'mqtt_monitoring_test'))
        with self.assertRaises(ValueError): config.pg_runtime('production-guess')

    def test_sealed_profile_config_independent_reader_and_exact_test_sender_acl(self):
        from backend.monitoring.config import load_monitoring_config
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'private').mkdir()
            credentials = {name: 'distinct-' + name for name in config.runtime_secrets('monitoring')}
            source = str(uuid4())
            config.write_configs(root, ports={'http': 8774, 'postgres': 55436, 'mqtt': 1888},
                                 credentials=credentials, source_ref=source, profile='monitoring')
            with patch.dict(os.environ, {config.env_name(k): v for k, v in credentials.items()}):
                loaded = load_monitoring_config(root / 'private/center.toml')
            self.assertEqual(loaded.environment, 'test')
            self.assertEqual(loaded.source_ref, source)
            self.assertEqual(loaded.postgres['user'], 'review_monitoring_reader')
            self.assertEqual(config.deployment_profile(root), 'monitoring')
            config.broker_configuration(root, mqtt_port=1888, profile='monitoring')
            acl = (root / 'private/mqtt.acl').read_text(encoding='utf-8').split('user review_mqtt_monitoring_test\n')[1]
            self.assertEqual(acl.splitlines(), [
                'topic write oilfield/v1/REVIEW-GW/MONITORING-TEST-1/telemetry',
                'topic write oilfield/v1/REVIEW-GW/MONITORING-TEST-2/telemetry'])
            self.assertNotIn(credentials['monitoring_reader'], (root / 'private/center.toml').read_text())

    def test_backup_retains_profile_roles_and_rejects_wrong_inventory(self):
        from tools.review_deploy_backup import seal_backup, verify_backup
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('database.dump', 'identity.sqlite', 'accounts.json'):
                (root / name).write_bytes(b'test')
            result = seal_backup(root, instance_id=str(uuid4()), source_ref=str(uuid4()), system_id='12345',
                source={'commit':'a'*40, 'files':{'server.py':'b'*64}}, row_counts={'public.telemetry_samples': 7},
                profile='monitoring')
            self.assertEqual(result['profile'], 'monitoring')
            self.assertIn('review_monitoring_reader', verify_backup(root)['roles'])
            self.assertEqual(verify_backup(root)['row_counts'], result['row_counts'])
            result['roles'].remove('review_monitoring_reader')
            (root / 'manifest.json').write_text(json.dumps(result))
            with self.assertRaises(ValueError): verify_backup(root)

    def test_fixed_export_includes_workbench_root(self):
        from tools.review_deploy_source import _included
        self.assertTrue(_included('workbench.html'))
        self.assertFalse(_included('private/accounts.json'))

    def test_occupied_port_is_refused_without_touching_service(self):
        from tools.review_deploy_process import require_free_ports
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            with self.assertRaises(ValueError):
                require_free_ports({'http': listener.getsockname()[1], 'postgres': 55436, 'mqtt':1888})
            self.assertGreater(listener.fileno(), -1)

    def test_profile_mismatch_is_rejected_not_silently_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'private').mkdir()
            credentials = {name:'distinct-'+name for name in config.runtime_secrets('monitoring')}
            config.write_configs(root,ports={'http':8774,'postgres':55436,'mqtt':1888},
                                 credentials=credentials,source_ref=str(uuid4()),profile='monitoring')
            marker = root/'private/marker.json'
            marker.write_text('{"profile":"review"}')
            before = marker.read_bytes()
            with self.assertRaises(ValueError): config.deployment_profile(root)
            self.assertEqual(marker.read_bytes(), before)

    def test_check_layers_are_explicit_and_required_skips_fail(self):
        from tools.monitoring_check import SUITES, successful
        self.assertEqual(set(SUITES), {'unit','browser','integration'})
        self.assertIn('tests.test_monitoring_e2e', SUITES['integration'])
        self.assertNotIn('tests.test_ingest_postgres', sum((list(v) for v in SUITES.values()), []))
        result = unittest.TestResult(); result.testsRun=1
        self.assertTrue(successful(result))
        result.skipped.append(('required','unavailable'))
        self.assertFalse(successful(result))

    def test_wrong_cluster_identity_refuses_launch_before_service_start(self):
        from tools.review_deploy import launch
        marker = {'state':'ready','ports':{'http':8774,'postgres':55436,'mqtt':1888},'system_id':'expected'}
        with patch('tools.review_deploy._checked',return_value=(Path('C:/owned-new'),marker)), \
             patch('tools.review_deploy.port_available',return_value=True), \
             patch('tools.review_deploy.cluster_running',return_value=True), \
             patch('tools.review_deploy._credential_file',return_value={}), \
             patch('tools.review_deploy.probe_system_identifier',return_value='different'), \
             patch('tools.review_deploy.start_services') as start:
            with self.assertRaisesRegex(ValueError,'identity mismatch'): launch('C:/owned-new')
            start.assert_not_called()

    def test_http_launch_has_reader_but_no_owner_or_sender_password(self):
        from tools.review_deploy_process import start_services
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'logs').mkdir()
            (root/'logs/runtime.json').write_text(json.dumps({'host':'127.0.0.1','port':8774,'pid':1,'instance_id':'test'}))
            credentials={name:'secret-'+name for name in (*config.PG_OWNERS,*config.runtime_secrets('monitoring'))}
            record={'pid':1,'command':str(root/'code/server.py')+' --runtime-mode=review --ui-mode=monitoring'}
            with patch('tools.review_deploy_process._start',return_value=record) as start, \
                 patch('tools.review_deploy_process._record',return_value=record), \
                 patch('tools.review_deploy_process.port_available',return_value=True), \
                 patch('tools.review_deploy_process.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
                start_services(root,ports={'http':8774,'mqtt':1888},credentials=credentials,profile='monitoring')
            env=run.call_args.kwargs['env']
            self.assertIn('--ui-mode=monitoring',run.call_args.args[0])
            self.assertEqual(env[config.env_name('monitoring_reader')],credentials['monitoring_reader'])
            for captured in [env,*[call.kwargs['env'] for call in start.call_args_list]]:
                for role in (*config.PG_OWNERS,'mqtt_monitoring_test'):
                    self.assertNotIn(config.env_name(role),captured)


