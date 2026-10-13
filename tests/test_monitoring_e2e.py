"""Actual owned PostgreSQL/HTTP/MQTT integration, never a fixed legacy DB."""
import unittest
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import io
import json
import sqlite3
from uuid import uuid4
from tests.monitoring_support import MonitoringDatabase


class MonitoringE2ETests(unittest.TestCase):
    def test_new_fixture_uses_monitoring_profile_and_retains_five_accounts(self):
        from tools.review_deploy_config import deployment_profile
        with MonitoringDatabase() as db:
            self.assertEqual(deployment_profile(db.root), 'monitoring')
            self.assertEqual(len(db.accounts['accounts']), 5)
            from backend.monitoring.privileges import probe_reader
            probe_reader(lambda: db.connect('monitoring_reader'))

    def test_actual_mqtt_http_roles_history_migration_cases_and_restart(self):
        from tests.monitoring_live_support import MonitoringLive
        with MonitoringLive() as live:
            accounts = live.db.accounts['accounts']
            for account in accounts:
                role = account['username']
                live.login(role)
                self.assertEqual(live.request('GET', '/api/registry/wells', role=role)[0], 200)
                for kind, allowed in [('wells', {'administrator','production_engineer'}),
                                      ('devices', {'administrator','maintenance_engineer'})]:
                    fields = ({'area_code':'SOFTWARE-TEST','well_code':'MATRIX-'+uuid4().hex,'name':'权限测试井'}
                              if kind == 'wells' else {'gateway_id':'REVIEW-GW','device_id':'MATRIX-'+uuid4().hex,
                              'name':'权限测试设备','transport':'mqtt_direct','usage':'commissioning'})
                    body = live.command_body(**fields)
                    status, _ = live.request('POST', '/api/registry/'+kind, body, role=role)
                    self.assertEqual(status, 201 if role in allowed else 403, (role, kind))
            well, device = live.register()
            t0 = datetime.now(timezone.utc)
            message, payload = live.publish([
                {'measured_at':t0, 'sequence':9007199254740993, 'value':41.123456789},
                {'measured_at':t0, 'sequence':9007199254740994, 'quality':'bad'},
                {'measured_at':t0, 'sequence':9007199254740995, 'quality':'uncertain'},
                {'measured_at':t0, 'sequence':9007199254740996, 'unit':'N'},
                {'measured_at':t0, 'sequence':9007199254740997, 'point_code':'unknown'},
                {'measured_at':t0+timedelta(minutes=10), 'sequence':9007199254740998},
                {'measured_at':t0-timedelta(hours=1), 'sequence':9007199254740999},
            ])
            live.wait_message(message)
            self.assertEqual(live.sender.publish(live.topic, payload), 0)
            acquisition = live.wait_message(message, duplicates='1')
            self.assertEqual(acquisition['sample_count'], '7')
            self.assertEqual(acquisition['processing']['receipt_status'], 'stored')
            self.assertEqual(acquisition['sources'], ['software-test'])
            quality = live.get('/api/production-monitoring/quality', view='resolution', device_id=device['device_id'])
            reasons = {r for row in quality['items'] for r in row['reasons']}
            self.assertTrue({'unit_mismatch','unknown_point','clock_anomaly','quality_bad','quality_uncertain'} <= reasons)
            self.assertTrue(all(not row['eligible_for_automation'] for row in quality['items']))
            history_path = '/api/production-monitoring/wells/'+well['id']+'/history'
            filters = {'registry_device_id':device['id'], 'point_code':'load',
                       'from_time':(t0-timedelta(hours=1)).isoformat(),'to_time':(t0+timedelta(hours=1)).isoformat()}
            first = live.get(history_path, **filters, page_size='2')
            self.assertEqual(first['total'], '5')  # Pre-binding delayed point remains unresolved, never reassigned.
            context = first['context']
            self.assertEqual(context['environment'], 'test')
            self.assertEqual(context['source_ref'], live.db.source_ref)
            snapshot = dict(revision=context['registry_revision'], received_before=context['received_before'], query_total=context['query_total'])
            rows = first['items']
            for page in ('2','3'):
                rows += live.get(history_path, **filters, **snapshot, page_size='2', page=page)['items']
            status, exported = live.request('GET', live.url(history_path+'.csv', filters | snapshot), raw=True)
            self.assertEqual(status, 200)
            csv_rows = list(csv.DictReader(io.StringIO(exported.decode('utf-8-sig'))))
            self.assertEqual([(r['message_id'],r['sample_index']) for r in rows],
                             [(r['message_id'],int(r['sample_index'])) for r in csv_rows])
            self.assertTrue(all(r['source']=='software-test' for r in csv_rows))
            self.assertTrue(any(r['sequence']=='9007199254740993' and r['value']=='41.123456789' for r in csv_rows))
            for days, expected in ((31,200),(32,422)):
                self.assertEqual(live.request('GET', live.url(history_path, filters | {
                    'from_time':(t0-timedelta(days=days)).isoformat(), 'to_time':t0.isoformat()}))[0], expected)
            well2 = live.post('/api/registry/wells', area_code='SOFTWARE-TEST',well_code='MOVED-'+uuid4().hex,name='迁入软件测试井')['entity']
            moved = live.post('/api/registry/devices/'+device['id']+'/bindings',
                              expected_version=device['version'], well_id=well2['id'])
            boundary = datetime.fromisoformat(moved['committed_at'])
            mixed, _ = live.publish([{'measured_at':t0,'sequence':11}, {'measured_at':boundary,'sequence':12}])
            mixed_data = live.wait_message(mixed)
            self.assertEqual(set(mixed_data['well_ids']), {well['id'],well2['id']})
            self.assertEqual(mixed_data['well_count'], '2')
            self.assertEqual(live.request('GET', live.url(history_path, filters | snapshot))[0],409)
            # Rebinding deliberately closes the prior map assignment and
            # suspends the device. Explicit re-assignment/review is required.
            latest_path='/api/production-monitoring/wells/'+well2['id']+'/latest'
            self.assertEqual(live.get(latest_path)['total'],'0')
            assigned=live.post('/api/registry/devices/'+device['id']+'/point-map-assignment',
                               expected_version=moved['entity']['version'],map_version=device['map_version'])['entity']
            live.post('/api/registry/devices/'+device['id']+'/state',role='reviewer',
                      expected_version=assigned['version'],status='active',check_note='迁井后软件核查')
            fresh,_=live.publish([{'sequence':15}]);live.wait_message(fresh)
            self.assertEqual(live.get(latest_path)['total'],'1')
            qmessage, qbytes = live.publish([{}], retain=True)
            case = live.wait_case(qmessage)
            raw_path = '/api/ingest-reliability/facts/'+case['fact_id']+'/raw'
            for account in accounts:
                role = account['username']
                status, content = live.request('GET', raw_path, role=role, raw=True)
                self.assertEqual(status, 200 if role in {'administrator','algorithm_engineer','maintenance_engineer'} else 403)
                if status == 200: self.assertEqual(content, qbytes)
            case_path = '/api/ingest-reliability/cases/'+case['id']
            ack_body = live.command_body(expected_version=case['version'])
            status, result = live.request('POST',case_path+'/ack',ack_body,role='maintenance_engineer')
            self.assertEqual(status,200)
            self.assertTrue(live.request('POST',case_path+'/ack',ack_body,role='maintenance_engineer')[1]['data']['replayed'])
            self.assertEqual(live.request('POST',case_path+'/ack',ack_body|{'reason':'不同请求内容'},role='maintenance_engineer')[0],409)
            self.assertEqual(live.request('POST',case_path+'/note',live.command_body(expected_version='1'))[0],409)
            for role in ('production_engineer','algorithm_engineer','reviewer'):
                self.assertEqual(live.request('POST',case_path+'/note',live.command_body(expected_version='2'),role=role)[0],403)
            current = live.get(case_path)['entity']
            live.post(case_path+'/close',expected_version=current['version'])
            self.assertEqual(live.get(case_path)['entity']['state'],'closed')
            self.assertEqual(live.get('/api/production-monitoring/quality',view='observations',device_id=device['device_id'],quality='bad')['total'],'1')
            # Controlled identity fixture mutation is local to this NEW test identity.
            with sqlite3.connect(live.db.root/'private/identity.db') as conn:
                conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='maintenance_engineer') AND permission_id IN (SELECT id FROM permissions WHERE code IN ('ingest.read','ingest.manage'))")
            self.assertEqual(live.request('POST',case_path+'/ack',ack_body,role='maintenance_engineer')[0],403)
            self.assertEqual(live.request('GET',case_path,role='maintenance_engineer')[0],403)
            self.assertEqual(live.request('GET','/api/ingest-reliability/receipts/'+case['receipt_id'],role='maintenance_engineer')[0],403)
            audit = live.get('/api/ingest-reliability/audit',case_id=case['id'])
            self.assertGreaterEqual(int(audit['total']),2)
            evidence = {'source':'software-test','source_ref':context['source_ref'],'environment':'test',
                        'message_id':message,'payload_sha256':hashlib.sha256(payload).hexdigest(),
                        'quarantine_fact_id':case['fact_id'],'quarantine_sha256':hashlib.sha256(qbytes).hexdigest(),
                        'case_id':case['id'],'csv_rows':len(csv_rows),'roles_checked':5,
                        'forbidden_requests':[], 'required_skips':0}
            evidence['actual_browser'] = live.browser(well['id'],device['id'],filters,realtime_well=well2['id'])
            live.restart_http_center()
            live.login('administrator')
            self.assertEqual(live.wait_message(message)['sample_count'],'7')
            # Retained MQTT is delivered again on re-subscribe: the existing
            # reliability policy reopens a closed, repeatedly rejected fact.
            repeated = live.get(case_path)['entity']
            self.assertEqual(repeated['state'],'open')
            self.assertEqual(str(repeated['duplicate_count']),'1')
            self.assertGreater(int(live.get('/api/ingest-reliability/audit',case_id=case['id'])['total']),int(audit['total']))
            self.assertEqual(live.request('GET',raw_path,raw=True)[1],qbytes)
            evidence['restart_verified'] = True
            live.save_evidence(evidence)

