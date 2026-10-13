"""Real loopback services owned by a new monitoring fixture; no legacy targets."""
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from backend.http_server import create_server
from backend.gateway.contracts import TelemetrySample, UplinkBatch
from tools.review_deploy import _broker_passwords
from tools.review_deploy_config import broker_configuration, env_name, runtime_secrets
from tools.review_deploy_paths import write_json_private
from tools.review_deploy_ready import center_subscribed
from tests.ingest_broker import Peer
from tests.monitoring_support import MonitoringDatabase

ROOT = Path(__file__).resolve().parents[1]


def wait_until(check, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result: return result
        threading.Event().wait(.05)
    raise AssertionError('Owned integration evidence did not arrive before deadline')


class MonitoringLive:
    def __init__(self):
        self.db = MonitoringDatabase()
        self.cookies = {}
        self.children = []
        self.server = self.thread = self.sender = self.center = None
        self.logs = []
        self.device_code = 'MONITORING-TEST-1'
        self.topic = 'oilfield/v1/REVIEW-GW/'+self.device_code+'/telemetry'

    def __enter__(self):
        self.db.__enter__()
        try:
            broker_configuration(self.db.root, mqtt_port=self.db.ports['mqtt'], profile='monitoring')
            _broker_passwords(self.db.root, self.db.credentials, 'monitoring')
            self._process(['C:/Program Files/Mosquitto/mosquitto.exe','-c',str(self.db.root/'private/mosquitto.conf')], 'broker', {})
            from tools.review_deploy_process import port_available
            wait_until(lambda: not port_available(self.db.ports['mqtt']))
            self._start_center_http()
            self.sender = Peer(SimpleNamespace(port=self.db.ports['mqtt'], password=self.db.credentials['mqtt_monitoring_test']),
                               'review_mqtt_monitoring_test')
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def _process(self, command, name, secrets):
        log = (self.db.root/'logs'/f'{name}.log').open('ab')
        self.logs.append(log)
        env = {key:value for key,value in os.environ.items() if not key.startswith(('REVIEW_','PLATFORM_'))
               and key not in ('PYTHONPATH','PGPASSWORD')}
        env.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
        env.update({env_name(k):v for k,v in secrets.items()})
        child = subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.children.append(child)
        return child

    def _start_center_http(self):
        log = self.db.root/'logs/center.log'
        offset = log.stat().st_size if log.exists() else 0
        self.center = self._process([sys.executable,'-B','-u','-m','backend.center','--config',str(self.db.root/'private/center.toml')],
                                   'center',{k:self.db.credentials[k] for k in ('mqtt_center','reliable_ingestor')})
        wait_until(lambda: center_subscribed(log,offset))
        with patch.dict(os.environ,{env_name(k):self.db.credentials[k] for k in runtime_secrets('monitoring')}):
            self.server = create_server('127.0.0.1',self.db.ports['http'],self.db.root/'private/identity.db',
                center_config_path=self.db.root/'private/center.toml',runtime_mode='review',ui_mode='monitoring',studies_engines={})
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def restart_http_center(self):
        self.server.shutdown(); self.thread.join(3); self.server.server_close(); self.server = None
        self.center.terminate(); self.center.wait(timeout=10)
        self._start_center_http()

    @staticmethod
    def url(path, filters): return path+('?' + urlencode(filters) if filters else '')

    @staticmethod
    def command_body(**fields):
        return {'reason':'独立安装软件链路验证','idempotency_key':str(uuid4())}|fields

    def request(self, method, path, body=None, *, role='administrator', raw=False):
        port = self.db.ports['http']
        headers = {'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'}
        if role in self.cookies: headers['Cookie'] = self.cookies[role]
        conn = http.client.HTTPConnection('127.0.0.1',port,timeout=10)
        try:
            conn.request(method,path,None if body is None else json.dumps(body),headers)
            response = conn.getresponse(); payload = response.read()
            if path == '/api/auth/login' and response.status == 200:
                cookie = response.getheader('Set-Cookie','').split(';',1)[0]
                if not cookie.startswith('oilwell_monitoring_session='): raise AssertionError('Incorrect monitoring cookie')
                self.cookies[role] = cookie
            return response.status, payload if raw else json.loads(payload)
        finally: conn.close()

    def login(self, role):
        account = next(a for a in self.db.accounts['accounts'] if a['username']==role)
        status, data = self.request('POST','/api/auth/login',{'username':role,'password':account['password']},role=role)
        if status != 200: raise AssertionError(('Login refused',role,status))
        capabilities = self.get('/api/auth/me',role=role)['capabilities']
        if any(capabilities[name]['status']!='ready' for name in ('monitoring','registry','ingest_reliability')):
            raise AssertionError('Real capabilities unavailable')
        return data['data']

    def get(self, path, *, role='administrator', **filters):
        status, data = self.request('GET',self.url(path,filters),role=role)
        if status != 200: raise AssertionError((status,data))
        return data['data']

    def post(self, path, *, role='administrator', **fields):
        status, data = self.request('POST',path,self.command_body(**fields),role=role)
        if status not in (200,201): raise AssertionError((status,data))
        return data['data']

    def register(self):
        well = self.post('/api/registry/wells',area_code='SOFTWARE-TEST',well_code='MQTT-'+uuid4().hex,name='软件链路测试井')['entity']
        device = self.post('/api/registry/devices',gateway_id='REVIEW-GW',device_id=self.device_code,
                           name='软件链路测试终端',transport='mqtt_direct',usage='commissioning')['entity']
        path = '/api/registry/devices/'+device['id']
        mapping = self.post(path+'/point-maps',expected_version=device['version'],parent_version=None,
                            points=[{'point_code':'load','unit':'kN','engineering_min':0,'engineering_max':80}])['entity']
        review = path+'/point-maps/'+mapping['map_version']+'/review'
        body = self.command_body(expected_version=mapping['device_version'],sha256=mapping['sha256'],decision='approved')
        status, data = self.request('POST',review,body)
        if status != 403 or data['error']['code'] != 'registry.independent_review_required':
            raise AssertionError('Self review was not refused')
        checked = self.post(review,role='production_engineer',expected_version=mapping['device_version'],
                            sha256=mapping['sha256'],decision='approved')['entity']
        bound = self.post(path+'/bindings',expected_version=checked['device_version'],well_id=well['id'])['entity']
        assigned = self.post(path+'/point-map-assignment',expected_version=bound['version'],map_version=mapping['map_version'])['entity']
        active = self.post(path+'/state',role='reviewer',expected_version=assigned['version'],status='active',check_note='软件链路验证')['entity']
        return well, device | {'version':active['version'],'map_version':mapping['map_version']}

    def publish(self, samples, *, retain=False):
        now = datetime.now(timezone.utc)
        readings = [TelemetrySample(gateway_id='REVIEW-GW',device_id=self.device_code,**(
            {'measured_at':now,'point_code':'load','sequence':i,'value':41.125,'unit':'kN','quality':'good','source':'software-test'}|row))
                    for i,row in enumerate(samples)]
        message = 'MON-'+uuid4().hex
        batch = UplinkBatch.create('REVIEW-GW',self.device_code,'BOOT-SOFTWARE-TEST',readings,message_id=message,created_at=now)
        payload = batch.to_json().encode('utf-8')
        if self.sender.publish(self.topic,payload,retain=retain) != 0: raise AssertionError('MQTT PUBACK was not accepted')
        return message,payload

    def wait_message(self, message, *, duplicates=None):
        def read():
            status,data = self.request('GET','/api/production-monitoring/acquisition/messages/'+message)
            if status == 404: return False
            if status != 200: raise AssertionError((status,data))
            result = data['data']
            return result if duplicates is None or result['processing']['duplicate_count']==duplicates else False
        return wait_until(read)

    def wait_case(self, message):
        return wait_until(lambda: (self.get('/api/ingest-reliability/cases',message_id=message)['items'] or [None])[0])

    def save_evidence(self, evidence):
        write_json_private(self.db.root/'private/integration-evidence.json',evidence)
        # An explicit optional sink, never an external test database selector.
        if os.environ.get('MONITORING_EVIDENCE_PATH'):
            write_json_private(Path(os.environ['MONITORING_EVIDENCE_PATH']),evidence|{'owned_root':str(self.db.root)})

    def browser(self,well,device,filters,*,realtime_well=None):
        runtime=Path(os.environ['USERPROFILE'])/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        result=subprocess.run([str(runtime/'bin/node.exe'),str(ROOT/'tests/fixtures/monitoring_live.cjs')],
            input=json.dumps({'port':self.db.ports['http'],'cookie':self.cookies['administrator'],
                              'well':well,'device':device,'filters':filters,'realtime_well':realtime_well}),
            cwd=ROOT,env=os.environ|{'NODE_PATH':str(runtime/'node_modules')},capture_output=True,
            text=True,encoding='utf-8',timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def __exit__(self,*args):
        if self.sender: self.sender.close()
        if self.server:
            self.server.shutdown(); self.thread.join(3); self.server.server_close()
        for child in reversed(self.children):
            if child.poll() is None: child.terminate(); child.wait(timeout=10)
        for log in self.logs: log.close()
        self.db.__exit__(*args)
