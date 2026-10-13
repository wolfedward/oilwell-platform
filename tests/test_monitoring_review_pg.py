import unittest
from uuid import UUID, uuid4

from tests.monitoring_support import MonitoringDatabase


class MonitoringIndependentReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MonitoringDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)

    def test_creator_cannot_review_even_with_admin_identity_and_no_state_changes(self):
        from backend.registry.contracts import Actor, validate_command
        from backend.registry.errors import RegistryError
        from backend.registry.service import RegistryService
        from backend.registry.transactions import RegistryTransactions
        device = self.db.command('device.create', gateway_id='GW', device_id='D'+uuid4().hex,
                                 name='独立复核测试', transport='mqtt_direct', usage='commissioning')['entity']
        mapping = self.db.command('map.create', device['id'], expected_version=device['version'],
                                 parent_version=None, points=[{'point_code':'load','unit':'kN'}])['entity']
        body = dict(expected_version=mapping['device_version'], map_version=mapping['map_version'],
                    sha256=mapping['sha256'], decision='approved', reason='独立复核软件测试', idempotency_key=str(uuid4()))
        with self.db.connect('registry_writer') as conn:
            before = conn.execute('SELECT revision FROM device_registry.registry_head').fetchone()[0]
        service = RegistryService(RegistryTransactions(lambda:self.db.connect('registry_writer')),
                                  require_independent_review=True)
        # Changed username/display name/role must not turn the creator into a different identity.
        actor = Actor('platform-local','101','renamed-admin','管理员','administrator')
        command = validate_command('map.review', UUID(device['id']), actor, body,
            method='POST', route=f'/api/registry/devices/{device["id"]}/point-maps/1/review')
        with self.assertRaises(RegistryError) as caught: service.execute(command)
        self.assertEqual((caught.exception.status,caught.exception.code),(403,'registry.independent_review_required'))
        with self.db.connect('registry_writer') as conn:
            self.assertEqual(conn.execute('SELECT revision FROM device_registry.registry_head').fetchone()[0],before)
            self.assertEqual(conn.execute('SELECT status FROM device_registry.point_map_versions WHERE device_id=%s',
                                         (device['id'],)).fetchone()[0],'draft')
            self.assertEqual(conn.execute('SELECT count(*) FROM device_registry.operation_receipts WHERE key=%s',
                                         (body['idempotency_key'],)).fetchone()[0],0)
        other = validate_command('map.review', UUID(device['id']), Actor('platform-local','202','reviewer','复核员','reviewer'),
                                 body, method='POST', route=f'/api/registry/devices/{device["id"]}/point-maps/1/review')
        self.assertEqual(service.execute(other)['entity']['status'],'approved')
        self.assertTrue(service.execute(other)['replayed'])
