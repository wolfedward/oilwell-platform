"""Deterministic software fixtures for not-yet-connected production modules.

No live runtime, transport, or control side effect. Identifiers explicitly use
preview UUID namespace and keep their original SQLite well association.
"""
from hashlib import sha256
import json
import math
from uuid import uuid5,NAMESPACE_URL

STAMP='2026-09-12T02:00:00.000000Z'
SOURCE='software_fixture'
def uid(name):return str(uuid5(NAMESPACE_URL,'oilwell-page-preview:'+name))
def digest(value):return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def paged(items):return {'items':items,'page':1,'page_size':20,'total':len(items),'has_more':False,'registry_revision':'1','snapshot_revision':'preview-1','received_before':STAMP,'from_time':'2026-09-11T02:00:00Z','to_time':STAMP,'queried_at':STAMP}

def fixtures(wells):
    routes={};devices=[];catalog=[]
    def put(path,data):routes[path]=data
    for i,well in enumerate(wells[:8]):
        wid=uid('well-'+well['id']);did=uid('device-'+well['id']);gateway='preview-gateway-01';device='preview-terminal-'+str(i+1).zfill(3)
        catalog.append({'id':wid,'original_well_id':well['id'],'area_code':'preview-area','well_code':well.get('code') or well['id'],'name':well['name'],'status':'active','version':'1','source_kind':SOURCE})
        record={'id':did,'name':well['name']+' · 前端终端样例','gateway_id':gateway,'device_id':device,'transport':'modbus_tcp','usage':'commissioning','usage_locked':False,'status':'active','version':'1','manufacturer':'开放参考设计','model':'MCU+DSP 参考架构','serial_number':'PREVIEW-'+str(i+1).zfill(3),'updated_at':STAMP,'created_at':STAMP,'original_well_id':well['id'],'source_kind':SOURCE}
        devices.append(record);base='/api/registry/devices/'+did
        put(base,{**record,'registry_revision':'1','received_before':STAMP})
        binding={'well_id':wid,'valid_from':STAMP,'valid_to':None,'audit_event_id':'preview-audit-1'}
        points=[{'point_code':code,'name':name,'address':40001+j*2,'register_address':j*2,'function_code':3,'data_type':'float32','byte_order':'ABCD','scale':1,'offset':0,'unit':unit,'sample_interval_ms':1000} for j,(code,name,unit) in enumerate([('active_power','有功功率','kW'),('current','电机电流','A'),('voltage','电机电压','V'),('load','悬点载荷','kN'),('displacement','悬点位移','m')])]
        content={'transport':'modbus_tcp','points':points};maprow={'version':'1','parent_version':None,'sha256':digest(content),'status':'approved','content':content,'created_at':STAMP,'source_kind':SOURCE,'author':{'user_id':'preview-author'}}
        put(base+'/bindings',paged([binding]));put(base+'/point-map-assignments',paged([{'map_version':'1',**binding}]))
        put(base+'/point-maps',paged([maprow,{**maprow,'version':'2','parent_version':'1','status':'draft'}]))
        put(base+'/point-maps/1',maprow);put(base+'/point-maps/2',{**maprow,'version':'2','status':'draft'})
        put(base+'/link-status',{'link_status':'delayed','last_received_at':STAMP,'age_seconds':240,'source_kind':SOURCE})
        put(base+'/state-history',paged([{'status':'active',**binding}]))
    put('/api/registry/wells',paged(catalog));put('/api/registry/devices',paged(devices))
    put('/api/registry/audit',paged([{'id':'preview-audit-1','event_at':STAMP,'action':'fixture.register','actor':{'display_name':'预置资料'},'reason':'软件场景','before_data':None,'after_data':{'source_kind':SOURCE}}]))
    put('/api/registry/resolved-telemetry',paged([{'gateway_id':'preview-gateway-01','device_id':'preview-terminal-001','point_code':'active_power','value':10.32,'unit':'kW','sequence':'1024','source':SOURCE,'quality':'good','measured_at':STAMP,'matched':True,'reasons':['fixture_mapping'],'well_id':catalog[0]['id'] if catalog else None,'sample_state':'active','current_state':'active','expected_map_version':'1'}]))
    caseid,factid,receiptid=[uid(x) for x in ['case','fact','receipt']]
    raw={'message_id':'preview-message-1024','gateway_id':'preview-gateway-01','device_id':'preview-terminal-001','sequence':'1024','points':[{'code':'active_power','value':None,'unit':'kW','quality':'bad'}],'source_kind':SOURCE}
    text=json.dumps(raw,ensure_ascii=False);sha=sha256(text.encode()).hexdigest()
    case={'id':caseid,'fact_id':factid,'state':'open','version':'1','topic':'oilwell/v2/preview-gateway-01/preview-terminal-001/telemetry','gateway_id':raw['gateway_id'],'device_id':raw['device_id'],'message_id':raw['message_id'],'payload_sha256':sha,'payload_bytes':str(len(text.encode())),'reason_code':'contract_invalid','validator_version':'telemetry.v2','received_at':STAMP,'first_seen':STAMP,'last_seen':STAMP,'duplicate_count':'2','transport_anomaly_count':'1','counters_saturated':False,'receipt_id':receiptid,'qos':'1','retain':False,'dup':False,'updated_at':STAMP,'source_kind':SOURCE}
    receipt={'id':receiptid,'fact_id':factid,'topic':'oilwell/v2/preview-gateway-01/preview-terminal-001/ingest-ack','gateway_id':raw['gateway_id'],'device_id':raw['device_id'],'message_id':raw['message_id'],'payload_sha256':sha,'status':'quarantined','reason_code':'contract_invalid','recorded_at':STAMP,'requested_generation':'1','sent_generation':'0','lease_until':None,'next_attempt_at':None,'requested_at':STAMP,'attempts':'1','last_error_code':'preview_no_publisher','broker_acked_at':None,'version':'1','delivery_state':'pending','source_kind':SOURCE}
    base='/api/ingest-reliability'
    put(base+'/cases',paged([case]));put(base+'/cases/'+caseid,{'entity':case})
    put(base+'/receipts',paged([receipt]));put(base+'/receipts/'+receiptid,{'entity':receipt})
    put(base+'/health',{'total_bytes':'16777216','quarantine_bytes':'8388608','used_total_bytes':case['payload_bytes'],'used_quarantine_bytes':case['payload_bytes'],'last_clock_seen':STAMP,'open_cases':'1','oldest_open_at':STAMP,'pending_receipts':'1','oldest_pending_at':STAMP,'publisher_last_seen':None,'quarantined_cases':'1','new_cases_24h':'1','oldest_open_age_seconds':240,'oldest_pending_age_seconds':240,'healthy':False,'reasons':['preview.publisher_disabled'],'publisher_error_code':None,'publisher_workers':{'workers':'0','active':'0','future':'0','failed':'0','unverified':'0'},'queried_at':STAMP,'source_kind':SOURCE})
    put(base+'/facts/'+factid+'/preview',{'fact_id':factid,'payload_sha256':sha,'payload_bytes':case['payload_bytes'],'text':text,'truncated':False,'raw_bytes':None,'source_kind':SOURCE})
    put(base+'/facts/'+factid+'/raw',{'text':text,'source_kind':SOURCE})
    put(base+'/audit',paged([{'id':uid('ingest-audit'),'fact_id':factid,'case_id':caseid,'receipt_id':receiptid,'actor':{'display_name':'预置资料','role_code':'maintenance_engineer'},'action':'fixture.quarantine','reason':'质量码异常场景','before_data':{},'after_data':{'state':'open'},'recorded_at':STAMP}]))
    base='/api/waveforms';captureid=uid('capture');pairid=uid('pair');datasetid=uid('dataset');evidenceid=uid('evidence')
    capture={'capture_pk':captureid,'capture_id':captureid,'gateway_id':raw['gateway_id'],'device_id':raw['device_id'],'state':'complete','version':'1','first_seen':STAMP,'has_conflicts':False,'stalled':False,'source_kind':SOURCE,'utc_start':STAMP,'utc_uncertainty_ns':'500000','missing_chunks':{},'completion':{'status':'complete'},'cycle':{'start_index':'0','end_index':'119'},'restrictions':['software_fixture_only'],'streams':[{'stream_id':'sensor','source':{'gateway_id':raw['gateway_id'],'device_id':raw['device_id'],'boot_id':uid('boot')},'rate':{'num':10,'den':1},'first_sample_index':'0','frame_count':120,'channels':[{'channel_id':'load','quantity':'load','unit':'kN','calibration_id':evidenceid},{'channel_id':'displacement','quantity':'displacement','unit':'m','calibration_id':evidenceid}]}]}
    put(base+'/captures',paged([capture]));put(base+'/captures/'+captureid,capture)
    for view,items in {'provenance':[{'transport':'fixture','source_ref':'preview-v1','received_at':STAMP}], 'facts':[{'fact_id':factid,'payload_sha256':sha,'received_at':STAMP}], 'outcomes':[{'status':'complete','reason':'软件样例','recorded_at':STAMP}]}.items():put(base+'/captures/'+captureid+'?view='+view,paged(items))
    put(base+'/captures/'+captureid+'?view=summary',capture)
    evaluation={'quality_run_id':uid('quality'),'recorded_at':STAMP,'source_key':{'gateway_id':raw['gateway_id'],'device_id':raw['device_id'],'capture_id':captureid},'purpose':'software_pair_test','status':'eligible','historical_registry':{'status':'resolved','resolution_reasons':[],'revision':'1','time_basis':{'unit':'ns'},'well':{'id':catalog[0]['id'] if catalog else '', 'area_code':'preview-area','well_code':catalog[0]['well_code'] if catalog else ''}}}
    put(base+'/captures/'+captureid+'/evaluations',paged([evaluation]))
    pts=[{'index':str(i),'raw_index':str(i),'relative_ns':str(i*100000000),'value':round(48+16*math.sin(i*math.tau/120)+.5*math.sin(i*.9),5),'quality':0} for i in range(120)]
    window={'source_kind':SOURCE,'source':capture['streams'][0]['source'],'mode':'summary','start':'0','count':120,'total_frames':120,'restrictions':['software_fixture_only'],'channels':[{'metadata':{'channel_id':'load','quantity':'load','unit':'kN','calibration':None,'sampling_config':None},'time_basis':{'coordinate':'corrected','utc_start':STAMP,'utc_uncertainty_ns':'500000','time_correction_ns':'0'},'status':'ok','restrictions':[],'points':pts,'gaps':[]}]}
    put(base+'/captures/'+captureid+'/streams/sensor/window',window)
    pair={'id':pairid,'pair_version_id':pairid,'version':1,'state':'approved','review_state':'approved','creator':{'user_id':'preview-author'},'content_sha256':digest(pts),'body':{'selection':{'channels':[{'role':'displacement','capture_pk':captureid,'stream_id':'sensor','channel_id':'displacement','first_index':'0','last_index':'119'},{'role':'load','capture_pk':captureid,'stream_id':'sensor','channel_id':'load','first_index':'0','last_index':'119'}]}},'source_kind':SOURCE}
    put(base+'/pairs',paged([pair]));put(base+'/pairs/'+pairid,pair)
    put(base+'/pairs/'+pairid+'/preview',{'historical_status':'eligible','current_status':'eligible','current_reasons':[],'current_restrictions':['software_fixture_only'],'dependencies_current':True})
    xy=[{'index':str(i),'relative_ns':str(i*100000000),'displacement':round(1.8*(1-math.cos(i*math.tau/120)),5),'load':round(48+16*math.tanh(4*math.sin(i*math.tau/120)),5)} for i in range(120)]
    put(base+'/pairs/'+pairid+'/preview/window',{'source_kind':SOURCE,'status':'ok','mode':'summary','start':'0','count':120,'total_frames':120,'points':xy,'gaps':[],'restrictions':['software_fixture_only'],'displacement_source':{'unit':'m'},'load_source':{'unit':'kN'}})
    dataset={'dataset_id':datasetid,'title':'同步功图软件验证集','state':'frozen','purpose':'software_pair_test','member_count':1,'source_kind':SOURCE}
    put(base+'/datasets',paged([dataset]));put(base+'/datasets/'+datasetid,{'draft':{'id':datasetid,'revision':'1','title':dataset['title'],'state':'frozen','purpose':'software_pair_test','split_plan':{}},'version':{'id':uid('dataset-version'),'draft_revision':'1','purpose':'software_pair_test','content_sha256':digest(pair),'restriction_reasons':['software_fixture_only'],'independent_split':False}})
    put(base+'/datasets/'+datasetid+'?view=members',paged([{'kind':'pair','reference_id':pairid}]))
    put(base+'/datasets/'+datasetid+'?view=files',paged([{'filename':'fixture-metadata.json','sha256':digest(pair),'source_kind':SOURCE}]))
    evidence={'id':evidenceid,'evidence_id':evidenceid,'kind':'calibration','version':'1','state':'draft','review_state':'draft','content_sha256':digest(window),'body':{'unit':'kN','scale':1,'offset':0,'source_kind':SOURCE},'creator':{'user_id':'preview-author'},'source_kind':SOURCE}
    put(base+'/evidence',paged([evidence]));put(base+'/evidence/'+evidenceid,evidence)
    put('/api/preview/views/acquisition',{'devices':devices,'wells':catalog,'point_map':points if devices else [],'stages':[{'stage':name,'status':state,'latency_ms':delay,'sequence':'1024','time':STAMP} for name,state,delay in [('Modbus 只读采样','软件样例',32),('边缘缓冲','已留存',4),('MQTT QoS 1 上行','传输场景',86),('中心持久化','校验隔离',12),('应用回执','尚未发送',None)]],'source_kind':SOURCE,'fixture_at':STAMP})
    return routes
