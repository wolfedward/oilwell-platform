"""Only the marked registry schema INSIDE the owned waveform database."""
from uuid import UUID,uuid4
import psycopg
from tools.provision_waveform_registry_test import owner_settings, sidecar_identity
from tests.waveform_support import WaveformDatabaseCase,TEST_DATABASE
from backend.registry.contracts import Actor,validate_command
from backend.registry.service import RegistryService
from backend.registry.transactions import RegistryTransactions

REGISTRY_DATA=('operation_receipts','point_map_assignments','point_map_versions','well_bindings',
               'device_state_periods','audit_events','devices','wells')


class WaveformRegistryCase(WaveformDatabaseCase):
    def setUp(self):
        super().setUp()
        self.registry_settings=owner_settings()
        expected=sidecar_identity(self.manifest)
        self.assertEqual(self.registry_settings['user'],expected['role'])
        with self.registry_connect() as conn:
            self.assertEqual(conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='device_registry'").fetchone(),(expected['role'],))
            # No schema/extension/view drop; no CASCADE, legacy reset or legacy config.
            conn.execute('TRUNCATE '+','.join('device_registry.'+n for n in REGISTRY_DATA))
            conn.execute('UPDATE device_registry.registry_head SET revision=1 WHERE singleton=1')

    def registry_connect(self):
        conn=psycopg.connect(**self.registry_settings); self.connections.append(conn)
        self._check_endpoint(conn)
        self.assertEqual(conn.execute('SELECT current_user').fetchone(),(self.registry_settings['user'],))
        conn.rollback(); return conn

    def registry_command(self,action,target=None,**body):
        actor=Actor('fixture','1','test','Synthetic fixture','administrator')
        body={'reason':'Synthetic waveform history test','idempotency_key':str(uuid4()),**body}
        command=validate_command(action,UUID(target) if target else None,actor,body,method='POST',route='/fixture/'+action)
        return RegistryService(RegistryTransactions(self.registry_connect)).execute(command)

    def history_source(self,*,gateway='GW',device='D',well=None):
        if well is None:
            well=self.registry_command('well.create',area_code='A',well_code='W'+uuid4().hex,name='Synthetic well')['entity']
        d=self.registry_command('device.create',gateway_id=gateway,device_id=device,name='Synthetic source',
                                transport='mqtt_direct',usage='commissioning')['entity']
        mapping=self.registry_command('map.create',d['id'],expected_version=d['version'],parent_version=None,
            points=[dict(point_code='load',unit='kN',engineering_min=0,engineering_max=80)])['entity']
        reviewed=self.registry_command('map.review',d['id'],expected_version=mapping['device_version'],
            map_version=mapping['map_version'],sha256=mapping['sha256'],decision='approved')['entity']
        bound=self.registry_command('device.bind',d['id'],expected_version=reviewed['device_version'],well_id=well['id'])
        assigned=self.registry_command('map.assign',d['id'],expected_version=bound['entity']['version'],map_version=mapping['map_version'])['entity']
        active=self.registry_command('device.state',d['id'],expected_version=assigned['version'],status='active',check_note='Synthetic test only')
        return dict(well=well,device=active['entity'],active_at=active['committed_at'])
