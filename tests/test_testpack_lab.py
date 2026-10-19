from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools import review_deploy_config as config
from tools.testpack.lab import validate_lab_ports,validate_lab_target,DEFAULT_PORTS
from tools.testpack.lab_http import validate_request


def fixture():
    return {'schema':'owp.testpack.fixture/1','lab_id':str(uuid4()),'pack_id':'tp-'+'a'*24,
            'pack_sha256':'b'*64,'gateway_id':'REVIEW-GW',
            'device_ids':['TESTPACK-PUMP-001','TESTPACK-PUMP-002'],'modbus_port':15020}


class TestPackLab(unittest.TestCase):
    def test_owned_instances_do_not_touch_protected_paths_or_ports(self):
        for port in (8765,8770,8772,8774):
            ports=deepcopy(DEFAULT_PORTS);ports['monitoring']['http']=port
            with self.assertRaises(ValueError):validate_lab_ports(ports,probe=False)
        ports=deepcopy(DEFAULT_PORTS);ports['review']['http']=ports['monitoring']['http']
        with self.assertRaises(ValueError):validate_lab_ports(ports,probe=False)
        with self.assertRaises(ValueError):validate_lab_ports({**DEFAULT_PORTS,'dsn':'postgres://business'},probe=False)
        with TemporaryDirectory() as parent:
            parent=Path(parent)
            (parent/'existing/private').mkdir(parents=True)
            (parent/'existing/private/marker.json').write_text('{}')
            with self.assertRaises(ValueError):validate_lab_target(parent/'existing/new')
            with self.assertRaises(ValueError):validate_lab_target(parent/'existing')
            (parent/'repo').mkdir();(parent/'repo/.git').mkdir()
            with self.assertRaises(ValueError):validate_lab_target(parent/'repo/new')
            self.assertEqual(validate_lab_target(parent/'fresh'),parent/'fresh')

    def test_fixture_config_sealed_at_initialization(self):
        declaration=fixture()
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'private').mkdir()
            secrets={name:'distinct-'+name for name in config.runtime_secrets('monitoring',fixture=declaration)}
            config.write_configs(root,ports=DEFAULT_PORTS['monitoring'],credentials=secrets,source_ref=str(uuid4()),
                                 profile='monitoring',fixture=declaration)
            config.broker_configuration(root,mqtt_port=19884,profile='monitoring',fixture=declaration)
            (root/'private/mqtt.passwd').write_text('test-hash')
            sealed=config.config_hashes(root)
            self.assertEqual(set(sealed),set(config.SEALED_FILES)|{'fixture.json','gateway.toml'})
            config.verify_config_hashes(root,sealed)
            acl=(root/'private/mqtt.acl').read_text().split('user review_mqtt_testpack\n')[1].splitlines()
            self.assertEqual(acl,[f'topic {mode} oilfield/{version}/REVIEW-GW/{device}/{suffix}'
                for device in declaration['device_ids'] for mode,version,suffix in
                (('write','v1','telemetry'),('read','v2','ingest-ack'))])
            self.assertFalse(any('+' in rule or '#' in rule for rule in acl))
            original=(root/'private/fixture.json').read_bytes()
            (root/'private/fixture.json').write_bytes(original+b' ')
            with self.assertRaises(ValueError):config.verify_config_hashes(root,sealed)
        self.assertNotIn('mqtt_testpack',config.runtime_secrets('monitoring'))

    def test_unowned_pid_never_stopped(self):
        from tools.review_deploy_process import stop_services
        expected={'pid':1234,'created':'old','executable':'owned.exe','command':'owned'}
        for change in ({'created':'reused'},{'executable':'other.exe'},{'command':'unrelated'}):
            with patch('tools.review_deploy_process.inspect_pid',return_value=expected|change),patch('tools.review_deploy_process.os.kill') as kill:
                with self.assertRaises(ValueError):stop_services(Path.cwd(),{'center':expected})
                kill.assert_not_called()

    def test_http_no_arbitrary_urls_or_auth_header_injection(self):
        for path in ('https://host/api/x','//host/api/x','/api/../internal','/api/%2e%2e/x','/api/x\r\nHost:evil','/api/'+('x'*9000)):
            with self.assertRaises(ValueError):validate_request('GET',path,{})
        for key in ('Host','Cookie','Origin','X-Monitoring-Session','Authorization'):
            with self.assertRaises(ValueError):validate_request('POST','/api/waveforms/imports',{key:'x'})
        validate_request('POST','/api/waveforms/imports/abc/lines/0',{'Content-Type':'application/octet-stream','If-Match':'0'})

    def test_actual_attachment_digest_header_is_bounded(self):
        headers={'Content-Type':'image/png','If-Match':'0','Idempotency-Key':str(uuid4()),
                 'X-Waveform-Reason':'Software%20evidence','X-Content-SHA256':'a'*64}
        validate_request('POST','/api/waveforms/attachments',headers)
        for digest in ('x','A'*64,'a'*63,'a'*65):
            with self.assertRaises(ValueError):
                validate_request('POST','/api/waveforms/attachments',headers|{'X-Content-SHA256':digest})

    def test_anonymous_request_does_not_reuse_actor_cookie(self):
        from tools.testpack.lab_http import LabHttp
        client=object.__new__(LabHttp);client.port=19999;client.profile='review';client.sessions={'administrator':{'Cookie':'private'}}
        with patch.object(client,'login') as login,patch.object(client,'_send',return_value=(401,b'{"ok":false}', '')) as send:
            client.request('GET','/api/production-studies',actor=None,expected=401)
            login.assert_not_called()
            self.assertNotIn('Cookie',send.call_args.args[3])

    def test_fixture_cannot_use_legacy_ports_even_via_direct_initialize(self):
        from tools.review_deploy import initialize
        with TemporaryDirectory() as folder,patch('tools.review_deploy.claim_target') as claim:
            for ports in ({'http':8774,'postgres':56432,'mqtt':19884},
                          {'http':18774,'postgres':56432,'mqtt':15020}):
                with self.assertRaises(ValueError):
                    initialize(Path(folder)/'new',source=Path.cwd(),ports=ports,fixture=fixture())
            claim.assert_not_called()

    def test_backup_refused_before_stopping_fixture(self):
        from tools.review_deploy import backup
        with patch('tools.review_deploy._checked',return_value=(Path.cwd(),{'state':'running'})), \
             patch('tools.review_deploy.deployment_fixture',return_value=fixture()), \
             patch('tools.review_deploy.stop') as stop:
            with self.assertRaises(ValueError):backup(Path.cwd())
            stop.assert_not_called()

    def test_selective_start_does_not_restart_broker_or_leak_sender_credentials(self):
        from tools.review_deploy_process import start_services
        credentials={name:'secret-'+name for name in config.runtime_secrets('monitoring',fixture=fixture())}
        with patch('tools.review_deploy_process._start',return_value={'pid':1}) as start:
            result=start_services(Path.cwd(),ports=DEFAULT_PORTS['monitoring'],credentials=credentials,
                                  profile='monitoring',components={'center'})
            self.assertEqual(set(result),{'center'})
            self.assertEqual(start.call_count,1)
            self.assertNotIn(config.env_name('mqtt_testpack'),start.call_args.kwargs['env'])
            with self.assertRaises(ValueError):start_services(Path.cwd(),ports={},credentials={},components={'broker'})

    def test_gateway_fixture_loads_actual_runtime_contract(self):
        from backend.gateway.config import load_gateway_config
        declaration=fixture()
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'private').mkdir()
            secrets={name:'distinct-'+name for name in config.runtime_secrets('monitoring',fixture=declaration)}
            config.write_configs(root,ports=DEFAULT_PORTS['monitoring'],credentials=secrets,source_ref=str(uuid4()),
                                 profile='monitoring',fixture=declaration)
            gateway=load_gateway_config(root/'private/gateway.toml',env={config.env_name(k):v for k,v in secrets.items()})
            self.assertEqual(gateway.confirmation_mode,'application')
            self.assertEqual(gateway.spool_database,root/'private/testpack-spool.db')
            self.assertEqual([len(d.points) for d in gateway.devices],[12,12])
            self.assertEqual([d.points[0].unit_id for d in gateway.devices],[1,2])

    def test_source_export_only_explicit_testpack_modules(self):
        from tools.review_deploy_source import _included
        self.assertTrue(_included('tools/testpack/lab.py'))
        self.assertTrue(_included('tools/testpack/storage.py'))
        for path in ('tools/private/review_deploy.py','tools/testpack/private.json','tools/testpack/private.py'):
            self.assertFalse(_included(path))


if __name__=='__main__':unittest.main()
