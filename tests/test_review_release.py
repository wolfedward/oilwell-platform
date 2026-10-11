"""Actual review HTTP with actual numeric engines, never mocked computation."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path

from backend.http_server import create_server
from backend.review_runtime import initialize_identity
from tools.verify_review_release import ReviewClient, verify_workflows
from tests.test_condition_studies import example as condition_example
from tests.test_recovery_studies import recovery_example, effects_example
from tests.test_energy_schedule import schedule_example
from tests.test_motor_spectrum import payload as spectrum_example
from tests.test_protection_replay import settings, frame


def vectors():
    schedule = schedule_example()
    well = schedule['wells'][0]
    well.pop('recovery')
    well['pumping_liquid_rate_m3_h'] = 6
    well['max_starts_per_calendar_day'] = 8
    well['candidates'] = [{'candidate_id': 'bounded', 'segments':
        [{'state': 'on', 'duration_minutes': 120}, {'state': 'off', 'duration_minutes': 60}] * 8}]
    return {'condition_training': condition_example(),
            'condition_prediction': {'schema': 'condition.prediction/1', 'model_record_id': 'replace',
                'rows': [{'sample_id': 'new', 'well_id': 'new-well', 'cycle_id': 'new-cycle',
                          'observed_at': '2026-01-04T00:00:00+00:00', 'values': [-1.9, .05, 10]}],
                'confidence_min': .5, 'ood_margin': .2},
            'recovery_fit': recovery_example(), 'energy_schedule': schedule,
            'energy_effects': effects_example(), 'motor_spectrum': spectrum_example(),
            'protection_replay': {'schema': 'motor.protection/1', 'settings': settings(),
                                  'frames': [frame(0, 11), frame(1000, 11)]}}


class ReviewReleaseTests(unittest.TestCase):
    def test_actual_engines_review_dependencies_export_restart_and_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            identity = Path(folder) / 'identity.db'
            accounts = initialize_identity(identity)
            server = create_server('127.0.0.1', 0, identity, runtime_mode='review')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = ReviewClient(f'http://127.0.0.1:{server.server_port}', accounts['accounts'])
                report = verify_workflows(client, vectors())
                self.assertEqual(report['status'], 'passed')
                self.assertEqual(set(report['records']), set(vectors()))
                self.assertGreaterEqual(len(report['checks']), 15)
                self.assertFalse(report['control_enabled'])
                self.assertGreater(server.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='studies.export'"), 0)
                hashes = {kind: row['result_sha256'] for kind, row in report['records'].items()}
            finally:
                server.shutdown(); thread.join(3); server.server_close()
            restarted = create_server('127.0.0.1', 0, identity, runtime_mode='review')
            worker = threading.Thread(target=restarted.serve_forever, daemon=True)
            worker.start()
            try:
                client = ReviewClient(f'http://127.0.0.1:{restarted.server_port}', accounts['accounts'])
                for kind, record in report['records'].items():
                    read = client.get('/api/production-studies/' + record['id'])
                    self.assertEqual(read['result_sha256'], hashes[kind])
                    self.assertEqual(read['source']['kind'], 'software_fixture')
                self.assertEqual(restarted.database.scalar('SELECT COUNT(*) FROM wells'), 0)
                self.assertEqual(restarted.database.scalar('SELECT COUNT(*) FROM telemetry'), 0)
            finally:
                restarted.shutdown(); worker.join(3); restarted.server_close()

    def test_client_refuses_nonloopback_url_before_login_or_writes(self):
        for url in ('https://example.com', 'http://0.0.0.0:8770', 'http://localhost:8770',
                    'http://127.0.0.1:8770/private', 'http://user@127.0.0.1:8770'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ReviewClient(url, [])
