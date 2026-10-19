"""Actual loopback waveform intake and evidence workflow. No database writes.

All approvals below are software test-account actions, never human signatures.
The fixed inverse budget fails; this runner must not approve or run it anyway.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
import time
from urllib.parse import quote, urlencode
from uuid import uuid4

from backend.waveforms.files import scan_file
from backend.waveforms.samples import iter_samples
from backend.waveforms.wire import parse_frame
from tools.review_deploy_paths import private_acl
from .config import parse_config
from .contracts import Channel, Cycle, EvidenceBinding, CheckResult
from .evidence_report import render_evidence
from .inversion_binding import number, software_budget
from .lab import checked_handle
from .lab_http import LabHttp, LabHttpError
from .storage import canonical_bytes, load_pack, safe_path, strict_json, generator_fingerprint
from .waveforms import encode_cycle


def common(start):
    instant=datetime.fromisoformat(start.replace('Z','+00:00'))
    fmt=lambda value:value.isoformat(timespec='microseconds').replace('+00:00','Z')
    return {'source_kind':'synthetic','valid_from':fmt(instant-timedelta(minutes=1)),
        'valid_to':fmt(instant+timedelta(minutes=1)),'attachment_ids':[],
        'notes':'Software fixture conversion and timing; not physical calibration or human certification.'}


def channel_bodies(channel,start,sampling=None):
    endpoint={'gateway_id':'REVIEW-GW','device_id':'TESTPACK-PUMP-001','stream_id':'native','channel_id':channel.role}
    limits={'active_power':(-250000,250000),'crank_angle':(-20,20),'load':(0,100),'displacement':(-1,10)}
    lo,hi=limits[channel.role]; scale=__import__('decimal').Decimal(channel.scale)
    if sampling is not None:
        return common(start)|{'endpoint':endpoint,'sampling_config_version_id':sampling['id'],
            'sampling_config_sha256':sampling['content_sha256'],'scale':channel.scale,'offset':channel.offset,
            'unit':channel.unit,'calibrated_range':{'min':number(lo),'max':number(hi),'unit':channel.unit},
            'zero':'0','direction':'positive','measurement_uncertainty':number(scale/2),
            'time_correction_ns':'0','residual_ns':'0','method':'Synthetic affine conversion',
            'basis':'Declared raw integer quantization; not traceable sensor calibration','calibrated_at':start}
    power=channel.role=='active_power'
    result=common(start)|{'endpoint':endpoint,'reported_config_id':None,'sensor_id':'Software channel',
        'quantity':'total_active_power' if power else channel.role,'unit':channel.unit,'encoding':'i32le',
        'measurement_source':'synthetic','signal_kind':'window_mean' if power else 'instantaneous','effective_bits':32,
        'input_range':{'min':number(__import__('decimal').Decimal(lo)/scale),
                       'max':number(__import__('decimal').Decimal(hi)/scale),'unit':channel.unit},
        'gain':'1','bias':'0','wiring':None,'analog_filter':None,'digital_filter':None,'rate':{'num':128,'den':1},
        'clock':{'clock_domain_id':'synthetic-clock','mode':'shared_sampling_clock','trigger':'Software shared sample index'},
        'window':None,'power':None,'reported_point_map_version':None}
    if power:
        result.update(wiring={'connection':'motor_terminal_three_phase','voltage_reference':'line_to_line',
            'phase':'total','phase_sequence':'ABC','current_direction':'positive_into_motor'},
            window={'definition':'Centered software arithmetic mean','averaging_window_ns':'1000000','timestamp_convention':'center'},
            power={'method':'Synthetic total active power','averaging_window_ns':'1000000','timestamp_convention':'center',
                'bandwidth_hz':'100','bandwidth_basis':'Declared software model bandwidth, not instrument response'})
    return result


def inv_pin(item):
    return {'id':item['id'],'content_sha256':item['content_sha256'],'review_version':int(item['version'])}


def ref(item):
    return {'id':item['id'],'content_sha256':item['content_sha256'],'version':str(item['version'])}


def bind_channels(client,channels,start):
    sampling={};calibration={};reported={}
    for channel in channels:
        reported[channel.role]='testpack-'+uuid4().hex
        body=channel_bodies(channel,start)|{'reported_config_id':reported[channel.role]}
        sampled=client.evidence('sampling_config',body)
        calibrated=client.evidence('calibration',channel_bodies(channel,start,sampled))
        sampling[channel.role]=inv_pin(sampled);calibration[channel.role]=inv_pin(calibrated)
    return EvidenceBinding(sampling,calibration,None,None,reported)


def save_bound_file(root,case_id,raw):
    if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,95}',case_id):raise ValueError('unsafe case id')
    path=safe_path(root/(case_id+'.owpwave'),missing=True)
    with path.open('xb') as stream:stream.write(raw)
    return {'path':path.name,'sha256':sha256(raw).hexdigest(),'bytes':len(raw)}


def inversion_gate(cycle):
    budget=software_budget(cycle)
    # There is intentionally no approval bypass, even if a future budget oracle
    # changes. A validated parameter workflow requires an explicitly revised plan.
    return {'status':'blocked','parameter_submitted':False,'run_id':None,'result_id':None,
        'budget':budget,'reason':'fixed_budget_failed' if not budget['passed'] else 'approved_workflow_not_implemented'}


def guard_selection(selection,*,profile=None,purpose=None,sync_version=None):
    result=deepcopy(selection)
    if profile is not None:result['profile']=profile
    if purpose is not None:result['purpose']=purpose
    if sync_version is not None:result['sync']['version']=str(sync_version)
    return result


def waveform_outcome(case_id,quality):
    required={'cycle-clipped':{'bad_or_clipped_samples'},'cycle-missing':{'missing_samples'},
              'cycle-shifted':{'missing_samples','insufficient_coverage'}}.get(case_id)
    reasons=set(quality['reasons'])
    if required is None:return 'passed' if quality['status']=='eligible' else 'failed'
    return 'passed' if quality['status']=='ineligible' and required<=reasons<=required|{'unregistered'} else 'failed'


def read_cycle(pack,case_id):
    from io import BytesIO
    meta=strict_json((pack.root/'cycles'/f'{case_id}.json').read_bytes())
    raw=(pack.root/'cycles'/f'{case_id}.owpwave').read_bytes()
    parsed=scan_file(BytesIO(raw));stream=parsed.manifest.streams[0]
    values={c['role']:[] for c in meta['channels']};qualities={k:[] for k in values}
    for line in raw.splitlines()[1:]:
        chunk=parse_frame(line)
        for sample in iter_samples(stream,chunk.data,sample_start=chunk.sample_start):
            values[sample.channel_id].append(0 if sample.raw_value is None else sample.raw_value)
            qualities[sample.channel_id].append(sample.quality)
    channels=tuple(Channel(c['role'],c['unit'],c['scale'],c['offset'],tuple(values[c['role']]),tuple(qualities[c['role']])) for c in meta['channels'])
    cycle=Cycle(case_id,meta['sample_rate_hz'],tuple(i*7812500 for i in range(meta['frame_count'])),
                channels,meta['turn_index'],meta['model'],meta['diagnostics'])
    return cycle,raw


class WaveformSession:
    def __init__(self,lab,root):
        self.http=LabHttp(lab,'review');self.root=root

    def post(self,path,body,version=0,actor='administrator'):
        return self.http.request('POST','/api/waveforms'+path,{'idempotency_key':str(uuid4()),
            'expected_version':str(version),'reason':'Software testpack evidence workflow, not human sign-off','body':body},actor=actor)['data']

    def get(self,path):
        return self.http.request('GET','/api/waveforms'+path,actor='administrator')['data']

    def evidence(self,kind,body):
        item=self.post('/evidence',{'kind':kind,'body':body})
        self.post('/evidence/'+item['id']+'/review',{'decision':'approve'},item['version'],'reviewer')
        current=self.get('/evidence/'+item['id'])
        if current['review_state']!='approved':raise ValueError('software evidence review not approved')
        return current

    def attachment(self,summary,name):
        png=render_evidence(summary,self.root/(name+'.png')).read_bytes()
        item=self.http.request('POST','/api/waveforms/attachments',png,actor='administrator',headers={
            'Content-Type':'image/png','Idempotency-Key':str(uuid4()),'If-Match':'0',
            'X-Waveform-Reason':quote('Software time and budget report'),'X-Content-SHA256':sha256(png).hexdigest()})['data']
        return item

    def archive(self,raw):
        lines=raw.splitlines();manifest=strict_json(lines[0]);key={k:manifest[k] for k in ('gateway_id','device_id','capture_id')}
        current=self.post('/imports',key|{'file_sha256':sha256(raw).hexdigest(),'bytes':len(raw),
            'line_count':len(lines),'final_lf':raw.endswith(b'\n')})
        import_id=current['import_id'];manifest_fact=None
        for index,line in enumerate(lines):
            current=self.http.request('POST',f'/api/waveforms/imports/{import_id}/lines/{index}',line,
                actor='administrator',headers={'Content-Type':'application/octet-stream','Idempotency-Key':str(uuid4()),
                    'If-Match':str(current['version']),'X-Waveform-Reason':'Exact%20software%20file%20line'})['data']
            if index==0:manifest_fact=current['fact_id']
        current=self.post('/imports/'+import_id+'/seal',{},current['version'])
        deadline=time.monotonic()+30
        while True:
            matches=[v for v in self.get('/captures?'+urlencode({**{k:key[k] for k in ('gateway_id','device_id')},'page_size':200}))['items'] if v['capture_id']==key['capture_id']]
            if matches and matches[0]['state']=='complete':break
            if time.monotonic()>deadline:raise TimeoutError('capture did not assemble')
            time.sleep(.2)
        cap=matches[0]
        sources=self.get('/captures/'+cap['capture_pk']+'?'+urlencode({'view':'provenance','transport':'file','source_ref':import_id}))['items']
        source=next(v for v in sources if v['fact_id']==manifest_fact)
        return {'manifest':manifest,'manifest_sha256':sha256(lines[0]).hexdigest(),'capture':key,
            'capture_pk':cap['capture_pk'],'import':current,'provenance':{k:source[k] for k in ('observation_id','outcome_id','fact_id')},'import_id':import_id}

    def selection(self,archive,cycle,binding,start,attachment):
        manifest=archive['manifest'];key=archive['capture'];source=manifest['streams'][0]['source']
        origin=self.evidence('origin',common(start)|{'capture':key,'source_identity':source,
            'provenance':archive['provenance']|{'transport':'file','source_ref':archive['import_id'],
                'import_id':archive['import_id'],'manifest_sha256':archive['manifest_sha256']},
            'verification_method':'synthetic','verification_basis':'Exact sealed generated file',
            'credential_attachment_ids':[],'broker_config_ref':None})
        endpoint={'capture':key,'source':source,'stream_id':'native','clock_domain_id':'synthetic-clock'}
        sync=self.evidence('sync',common(start)|{'attachment_ids':[attachment['id']],
            'method':'verified_linear_mapping','reference':endpoint,'endpoints':[{'endpoint':endpoint,
                'reference_sample_index':'0','reference_event':'Software common sample zero','nominal_reference_ns':'0',
                'rate':{'num':128,'den':1},'valid_start_ns':'0','valid_end_ns':'8000000001',
                'offset_error_ns':'0','frequency_error_ppm':'0','residual_ns':'0','clock_divisor':None}]})
        quantities=[{k:c[k] for k in ('quantity','unit','signal_kind','measurement_source')}|{'role':c['channel_id']}
                    for c in manifest['streams'][0]['channels']]
        ranges=[{'role':c.role,'range':channel_bodies(c,start,{'id':str(uuid4()),'content_sha256':'0'*64})['calibrated_range']} for c in cycle.channels]
        profile_body=common(start)|{'purpose':'software_pair_test','target_equipment_type':'Synthetic rigid four-bar',
            'electrical_input':'three_phase_total_active_power','required_quantities':quantities,
            'min_rates':[{'role':c.role,'rate':{'num':128,'den':1}} for c in cycle.channels],
            'max_sync_error_ns':'0','ranges':ranges,'required_evidence':['sampling_config','calibration','origin','sync'],
            'trial_basis':'Independent software model only; no field accuracy claim'}
        profile=self.evidence('profile',profile_body)
        sel={'purpose':'software_pair_test','profile':ref(profile),
            'captures':[{'key':key,'manifest_sha256':archive['manifest_sha256'],'origins':[ref(origin)]}],
            'channels':[{'role':c.role,'capture':key,'stream_id':'native','channel_id':c.role,
                'first_index':'0','last_index':'1024',
                'sampling_config':{'id':binding.sampling[c.role]['id'],'content_sha256':binding.sampling[c.role]['content_sha256'],'version':'2'},
                'calibration':{'id':binding.calibration[c.role]['id'],'content_sha256':binding.calibration[c.role]['content_sha256'],'version':'2'}} for c in cycle.channels],
            'cycle':{'capture':key,'cycle_id':manifest['cycle']['cycle_id']},'sync':ref(sync)}
        return sel,profile_body

    def pair_and_freeze(self,archive,selection,case_id):
        quality=self.post('/captures/'+archive['capture_pk']+'/evaluations',selection)
        pair=self.post('/pairs',{'selection':selection,'method_version':'linear_adjacent/1'})
        output={'quality':quality,'pair':pair,'dataset':None}
        if pair['body']['joint_result']['status']!='eligible':return output
        output['pair_review']=self.post('/pairs/'+pair['id']+'/review',{'decision':'approve'},1,'reviewer')
        draft=self.post('/datasets',{'title':'Software testpack '+case_id,'purpose':'software_pair_test','split_plan':{'kind':'none'}})
        draft=self.post('/datasets/'+draft['id']+'/update',{'changes':{'upsert_members':[{
            'kind':'pair','reference_id':pair['id'],'quality_run_id':pair['left_quality_run_id'],'split':'unassigned'}]}},draft['revision'])
        draft=self.post('/datasets/'+draft['id']+'/submit',{},draft['revision'])
        output['dataset']=self.post('/datasets/'+draft['id']+'/freeze',{},draft['revision'],'reviewer')
        return output

    def verify_guards(self,pack,baseline):
        """Real negative API evaluations; no manufactured rejection records."""
        selected=baseline['selection'];archive=baseline['archive'];cap=archive['capture_pk']
        profile=self.get('/evidence/'+selected['profile']['id'])
        body=deepcopy(profile['body']);body['purpose']='field_pair_input'
        field=self.evidence('profile',body)
        promoted=guard_selection(selected,profile=ref(field),purpose='field_pair_input')
        result=self.post('/captures/'+cap+'/evaluations',promoted)
        if any(row['status']=='eligible' for row in result['runs']):raise ValueError('synthetic source promoted')
        checks={'synthetic_field_refused':result}
        cycle,raw=read_cycle(pack,'cycle-base');unbound=self.archive(raw)
        start=pack.manifest['configuration']['start_utc']
        binding=EvidenceBinding(
            {c['role']:{'id':c['sampling_config']['id'],'content_sha256':c['sampling_config']['content_sha256'],'review_version':2} for c in selected['channels']},
            {c['role']:{'id':c['calibration']['id'],'content_sha256':c['calibration']['content_sha256'],'review_version':2} for c in selected['channels']},None,None)
        unbound_sel,_=self.selection(unbound,cycle,binding,start,baseline['attachment'])
        result=self.post('/captures/'+unbound['capture_pk']+'/evaluations',unbound_sel)
        if any(row['status']=='eligible' for row in result['runs']):raise ValueError('unbound archive promoted')
        checks['unbound_calibration_refused']=result
        self.post('/evidence/'+selected['sync']['id']+'/revoke',{},2,'reviewer')
        try:self.post('/captures/'+cap+'/evaluations',selected)
        except LabHttpError as error:
            if error.status!=409 or error.code!='waveform.stale_dependency':raise
            checks['stale_version_refused']={'http_status':error.status,'code':error.code}
        else:raise ValueError('stale evidence accepted')
        current=guard_selection(selected,sync_version=3)
        result=self.post('/captures/'+cap+'/evaluations',current)
        if any(row['status']=='eligible' for row in result['runs']):raise ValueError('revoked evidence accepted')
        checks['revoked_evidence_refused']=result
        checks['historical_pair_now_restricted']=self.get('/pairs/'+baseline['pair']['id']+'/preview')
        if checks['historical_pair_now_restricted']['current_status']=='eligible':raise ValueError('revoked pair remains eligible')
        return checks


def run_waveforms(lab,pack,run_root):
    checked_handle(lab);pack=load_pack(pack.root)
    root=safe_path(Path(run_root),missing=True)
    if root.exists() or root.parent!=lab.root:raise ValueError('new direct lab child required')
    root.mkdir();private_acl(root)
    before=sha256((pack.root/'manifest.json').read_bytes()).hexdigest()
    session=WaveformSession(lab,root);config=parse_config(pack.manifest['configuration']);checks=[]
    def save(name,value):
        with (root/name).open('xb') as stream:stream.write(canonical_bytes(value))
    save('run.json',{'schema':'owp.testpack.waveform-run/1','lab_id':lab.lab_id,'pack_id':pack.manifest['pack_id'],
        'pack_sha256':before,'runner':generator_fingerprint(),'source_kind':'synthetic',
        'software_actor_reviews':True,'human_signatures':False})
    for item in pack.manifest['cases']:
        if not item['case_id'].startswith('cycle-'):continue
        case_id=item['case_id'];actual={'stage':'read'}
        try:
            cycle,original=read_cycle(pack,case_id)
            actual['inversion']=inversion_gate(cycle)
            actual['stage']='channel_evidence';binding=bind_channels(session,cycle.channels,config.start_utc)
            bound=encode_cycle(cycle,config,binding).artifacts[0].content
            actual['original_sha256']=sha256(original).hexdigest();actual['bound']=save_bound_file(root,case_id,bound)
            actual['stage']='archive';archive=session.archive(bound)
            actual['archive']=archive
            actual['stage']='attachment';attachment=session.attachment({'case_id':case_id,'source_kind':'synthetic',
                'sample_rate_hz':128,'samples':1025,'time_ns':[0,8000000000],'time_uncertainty_ns':0,
                'channels':[{'role':c.role,'unit':c.unit,'scale':c.scale,'offset':c.offset} for c in cycle.channels],
                'budget':actual['inversion']['budget']},case_id)
            actual['attachment']=attachment
            actual['stage']='selection';selection,profile_body=session.selection(archive,cycle,binding,config.start_utc,attachment)
            actual['selection']=selection
            actual['stage']='pair';actual.update(session.pair_and_freeze(archive,selection,case_id))
            actual['stage']='complete'
            status=waveform_outcome(case_id,actual['pair']['body']['joint_result'])
        except Exception as error:
            status='failed';actual['error_type']=type(error).__name__
            if isinstance(error,LabHttpError):actual.update(http_status=error.status,error_code=error.code)
        save(case_id+'-result.json',actual)
        checks.append(CheckResult(case_id+'-waveform','http',status,{'source_kind':'synthetic'},actual,(case_id+'-result.json',)))
        if 'inversion' in actual:
            checks.append(CheckResult(case_id+'-approved-inversion','numeric','blocked',
                {'budget_passed':True,'rmse_max_N':500,'coverage_min':.8},actual['inversion'],(case_id+'-result.json',)))
        save('checkpoint-'+case_id+'.json',{'checks':[asdict(c) for c in checks]})
    unchanged=load_pack(pack.root).manifest==pack.manifest and sha256((pack.root/'manifest.json').read_bytes()).hexdigest()==before
    checks.append(CheckResult('original_pack_unchanged','offline','passed' if unchanged else 'failed',{'unchanged':True},{'unchanged':unchanged}))
    save('waveform-checks.json',{'source_kind':'synthetic','checks':[asdict(c) for c in checks]})
    return tuple(checks)


def run_waveform_guards(lab,pack,run_root,baseline_run):
    checked_handle(lab);pack=load_pack(pack.root)
    root=safe_path(Path(run_root),missing=True);baseline_root=safe_path(Path(baseline_run))
    if root.exists() or root.parent!=lab.root or baseline_root.parent!=lab.root:raise ValueError('owned lab paths required')
    origin=strict_json((baseline_root/'run.json').read_bytes())
    if origin['lab_id']!=lab.lab_id or origin['pack_sha256']!=sha256((pack.root/'manifest.json').read_bytes()).hexdigest():
        raise ValueError('baseline provenance differs')
    baseline=strict_json((baseline_root/'cycle-base-result.json').read_bytes())
    if baseline['stage']!='complete' or baseline['dataset'] is None:raise ValueError('completed baseline required')
    root.mkdir();private_acl(root)
    try:
        actual=WaveformSession(lab,root).verify_guards(pack,baseline);status='passed'
    except Exception as error:
        status='failed';actual={'error_type':type(error).__name__}
        if isinstance(error,LabHttpError):actual.update(http_status=error.status,error_code=error.code)
    check=CheckResult('waveform-source-revocation-guards','http',status,{'promotion_allowed':False},actual)
    with (root/'waveform-guards.json').open('xb') as stream:stream.write(canonical_bytes(asdict(check)))
    return check
