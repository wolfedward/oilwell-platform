"""Seven real HTTP study engines, append-only reviews and dependency checks."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import time
from uuid import uuid4,uuid5,NAMESPACE_URL

from backend.studies.contracts import canonical
from tools.review_deploy_paths import private_acl
from .contracts import CheckResult
from .lab import checked_handle,restart_owned_component
from .lab_http import LabHttp,LabHttpError
from .storage import canonical_bytes,load_pack,safe_path,strict_json,generator_fingerprint
from .study_inputs import bind_study

PREFIX='/api/production-studies'
STEPS=('health','training','prediction','recovery','schedule','effects','spectrum','protection',
       'boundaries','exports','revoke','renew','snapshots','restart')


def require(condition):
    if not condition:raise ValueError('study verification failed')


def digest(value):return sha256(canonical(value).encode('utf-8')).hexdigest()


def bind_runtime(template,record_ids,pack_sha256,phase):
    envelope=bind_study(template,record_ids,pack_sha256)
    envelope['request_id']=str(uuid5(NAMESPACE_URL,envelope['request_id']+'/'+phase))
    return envelope


def assert_boundaries(detail):
    require(detail['source']['kind']=='software_fixture')
    require(detail['field_validation'] is False and detail['control_eligible'] is False)
    result=detail['result'];kind=detail['kind']
    flags={'condition_training':('field_validation','control_eligible'),
        'condition_prediction':('field_validation','control_eligible'),
        'recovery_fit':('field_validation','dispatch_allowed'),
        'energy_schedule':('field_validation','dispatch_allowed'),
        'energy_effects':('field_validation','dispatch_allowed','causal_effect_proven'),
        'motor_spectrum':('field_validation','control_eligible','fault_confirmation'),
        'protection_replay':('field_validation','control_eligible','hardware_execution')}
    require(all(result[key] is False for key in flags[kind]))
    if kind in ('recovery_fit','energy_schedule','energy_effects'):require(result['shadow_only'] is True)


def run_study_steps(session,root,steps=STEPS):
    results=[];failed=False
    for step in steps:
        actual={};status='not_run'
        if not failed:
            try:actual=session.execute(step);status='passed'
            except Exception as error:
                failed=True;status='failed';actual={'error_type':type(error).__name__}
                if isinstance(error,LabHttpError):actual.update(http_status=error.status,error_code=error.code)
        results.append(CheckResult('study-'+step,'http',status,{'software_only':True},actual))
        # Checkpoints may be replaced only within this new run's owned directory.
        (root/'study-checks.json').write_bytes(canonical_bytes({'source_kind':'synthetic','checks':[asdict(r) for r in results]}))
    return tuple(results)


class StudySession:
    def __init__(self,http,root,templates,expected,pack_sha,run_id,lab=None):
        self.http,self.root,self.templates,self.expected=http,root,templates,expected
        self.pack_sha,self.run_id,self.lab=pack_sha,run_id,lab
        self.ids={};self.records={};self.envelopes={};self.snapshots={};self.sequence=0

    def request(self,method,path,body=None,*,actor='administrator',expected=200,raw=False):
        response=self.http.request(method,path,body,actor=actor,expected=expected,raw=raw)
        return response if raw or expected>=400 else response['data']

    def get(self,record):return self.request('GET',PREFIX+'/'+record['id'])

    def rejected(self,method,path,body,status,code,actor='administrator'):
        response=self.request(method,path,body,actor=actor,expected=status)
        require(response['error']['code']==code)
        return {'http_status':status,'code':code}

    def create(self,name,*,phase='initial'):
        case_id='study-'+name;template=self.templates[case_id]
        body=bind_runtime(template,self.ids,self.pack_sha,self.run_id+'/'+phase)
        record=self.request('POST',PREFIX,body,expected=201)
        require(self.request('POST',PREFIX,body,expected=201)==record)
        require(self.request('GET',PREFIX+'/requests/'+body['request_id'])==record)
        require(record['status']==self.expected[case_id])
        key=name if phase=='initial' else name+'-'+phase
        self.records[key]=record;self.envelopes[key]=body
        mapping={'scenario':case_id,'phase':phase,'template_sha256':sha256(canonical_bytes(template)).hexdigest(),
            'bound_envelope_sha256':sha256(canonical_bytes(body)).hexdigest(),'request_id':body['request_id'],
            'record_id':record['id'],'input_sha256':record['input_sha256'],'result_sha256':record['result_sha256'],
            'review_version':record['version'],'dependencies':record['dependencies']}
        self.save(key+'-mapping.json',mapping);self.save(key+'-request.json',body)
        return record

    def save(self,name,value):
        with (self.root/name).open('xb') as stream:stream.write(canonical_bytes(value))

    def review(self,record,decision='approved',*,actor='reviewer',version=None,error=None):
        current=self.get(record)
        body={'request_id':str(uuid4()),'expected_version':current['version'] if version is None else version,
              'decision':decision,'comment':'Software test account only; not human approval or device permission'}
        path=PREFIX+'/'+record['id']+'/reviews'
        if error:return self.rejected('POST',path,body,*error,actor=actor)
        return self.request('POST',path,body,actor=actor)

    def select(self,record,error=None):
        current=self.get(record)
        body={'request_id':str(uuid4()),'expected_version':current['selection_version'],
              'record_id':record['id'],'comment':'Software shadow selection, no device dispatch'}
        if error:return self.rejected('POST',PREFIX+'/selections/'+record['kind'],body,*error)
        return self.request('POST',PREFIX+'/selections/'+record['kind'],body)

    def export(self,key,record):
        raw=self.request('GET',PREFIX+'/'+record['id']+'/export',raw=True)
        detail=strict_json(raw);assert_boundaries(detail)
        require(digest(detail['result'])==record['result_sha256'])
        require(digest({k:detail[k] for k in ('source','payload','dependencies')})==record['input_sha256'])
        self.sequence+=1;name=f'{key}-export-{self.sequence:03d}.json'
        with (self.root/name).open('xb') as stream:stream.write(raw)
        self.save(name+'.sha256.json',{'sha256':sha256(raw).hexdigest(),'record_id':record['id'],
            'version':detail['version'],'result_sha256':detail['result_sha256']})
        return detail

    def execute(self,step):
        if step=='health':
            health=self.request('GET','/api/health',actor=None)
            require(health['runtime_mode']=='review' and health['control_enabled'] is False and health['ui_mode']=='standard')
            self.rejected('GET',PREFIX,None,401,'unauthenticated',actor=None)
            self.request('POST','/api/commands',{},expected=404)
            return {'runtime_mode':'review','control_enabled':False,'anonymous_status':401,'legacy_commands_status':404}
        if step=='training':
            record=self.create('condition-training');self.ids['condition_training']=record['id']
            self.review(record,actor='administrator',error=(403,'self_review'))
            self.review(record);self.review(record,version=1,error=(409,'stale_version'));self.select(record)
            changed=deepcopy(self.envelopes['condition-training']);changed['title']+=' changed'
            self.rejected('POST',PREFIX,changed,409,'request_conflict')
            return {'record_id':record['id'],'self_review':403,'stale_version':409,'request_conflict':409,'idempotent':True}
        if step=='prediction':
            record=self.create('condition-prediction')
            require(record['dependencies'][0]['record_id']==self.ids['condition_training'])
            changed=deepcopy(self.envelopes['condition-prediction']);changed['request_id']=str(uuid4());changed['source']['kind']='submitted_observation'
            self.rejected('POST',PREFIX,changed,422,'source_mismatch')
            return {'record_id':record['id'],'source_promotion_rejected':True,'dependencies':record['dependencies']}
        if step=='recovery':
            record=self.create('recovery-fit');self.review(record)
            well=self.templates['study-recovery-fit']['envelope']['payload']['well_id']
            self.ids['recovery_fit:'+well]=record['id']
            return {'record_id':record['id'],'well_id':well}
        if step=='schedule':
            record=self.create('energy-schedule');self.review(record);self.select(record)
            return {'record_id':record['id'],'dependencies':record['dependencies'],'shadow_only':True}
        if step in ('effects','spectrum','protection'):
            name={'effects':'energy-effects','spectrum':'motor-spectrum','protection':'protection-replay'}[step]
            record=self.create(name);self.review(record)
            if step=='protection':self.select(record)
            return {'record_id':record['id'],'actual_status':record['status']}
        if step=='boundaries':
            output={}
            for name in ('recovery-short','schedule-infeasible','effects-low-coverage','spectrum-missing-info'):
                record=self.create(name);output[name]={'id':record['id'],'status':record['status']}
                if name!='spectrum-missing-info':self.review(record,error=(409,'invalid_decision'))
                else:require(self.get(record)['result']['support']=='spectrum_only')
            invalid=bind_runtime(self.templates['study-protection-invalid'],self.ids,self.pack_sha,self.run_id)
            self.rejected('POST',PREFIX,invalid,422,'invalid_field')
            self.rejected('GET',PREFIX+'/requests/'+invalid['request_id'],None,404,'not_found')
            output['protection-invalid']={'http_status':422,'receipt_status':404}
            return output
        if step=='exports':
            for key,record in self.records.items():self.export(key,record)
            return {'exported':len(self.records),'hashes_verified':True}
        if step=='revoke':
            training=self.records['condition-training'];recovery=self.records['recovery-fit']
            self.review(recovery,'revoked');self.review(training,'revoked')
            old_schedule=self.get(self.records['energy-schedule']);old_prediction=self.get(self.records['condition-prediction'])
            require(not old_schedule['dependency_current'] and not old_schedule['eligible'] and not old_schedule['is_selected'])
            require(not old_prediction['dependency_current'])
            self.select(self.records['energy-schedule'],error=(409,'invalid_dependency'))
            self.select(training,error=(409,'invalid_dependency'))
            retry=bind_runtime(self.templates['study-condition-prediction'],self.ids,self.pack_sha,self.run_id+'/revoked')
            self.rejected('POST',PREFIX,retry,409,'invalid_dependency')
            self.save('revoked-dependencies.json',{'schedule':old_schedule,'prediction':old_prediction})
            return {'old_schedule_eligible':False,'old_prediction_dependency_current':False,'new_prediction_refused':True}
        if step=='renew':
            self.review(self.records['condition-training']);self.select(self.records['condition-training'])
            self.review(self.records['recovery-fit'])
            for name in ('condition-prediction','energy-schedule'):
                renewed=self.create(name,phase='renewed');self.review(renewed)
                require(renewed['id']!=self.records[name]['id'])
                require(self.get(renewed)['dependency_current'] is True)
                require(self.get(self.records[name])['dependency_current'] is False)
                if name=='energy-schedule':self.select(renewed)
            return {'renewed_record_ids':[self.records[n+'-renewed']['id'] for n in ('condition-prediction','energy-schedule')],
                    'historical_dependencies_preserved':True}
        if step=='snapshots':
            self.snapshots={key:self.export(key,record) for key,record in self.records.items()}
            listed=self.request('GET',PREFIX+'?limit=100')
            require(all('payload' not in item and 'result' not in item for item in listed))
            self.save('final-records.json',self.snapshots)
            return {'records':len(self.snapshots),'kinds':sorted({r['kind'] for r in self.records.values()})}
        if step=='restart':
            if self.lab is None:raise ValueError('owned lab required for restart')
            restart_owned_component(self.lab,'review','http');self.http=LabHttp(self.lab,'review')
            deadline=time.monotonic()+40
            while True:
                try:
                    health=self.request('GET','/api/health',actor=None)
                    if health['runtime_mode']=='review':break
                except (OSError,LabHttpError):pass
                if time.monotonic()>deadline:raise TimeoutError('owned review HTTP restart timeout')
                time.sleep(.3)
            for key,before in self.snapshots.items():
                after=self.get(self.records[key]);assert_boundaries(after)
                require(all(after[k]==before[k] for k in ('input_sha256','result_sha256','version','source','dependency_current','eligible','is_selected')))
            return {'retained_records':len(self.snapshots),'component':'owned review HTTP','hashes_and_versions_unchanged':True}
        raise ValueError('unknown study step')


def run_studies(lab,pack,run_root):
    checked_handle(lab);pack=load_pack(pack.root)
    root=safe_path(Path(run_root),missing=True)
    if root.exists() or root.parent!=lab.root:raise ValueError('new owned lab child required')
    root.mkdir();private_acl(root)
    manifest_sha=sha256((pack.root/'manifest.json').read_bytes()).hexdigest()
    templates={};expected={}
    for case in pack.manifest['cases']:
        if case['facts'].get('data_kind')!='study':continue
        path=next(p for p in case['artifact_paths'] if p.startswith('templates/'))
        templates[case['case_id']]=strict_json((pack.root/path).read_bytes())
        expected[case['case_id']]=case['expected'][0]['expected']
    run_id=str(uuid4())
    session=StudySession(LabHttp(lab,'review'),root,templates,expected,manifest_sha,run_id,lab)
    session.save('run.json',{'schema':'owp.testpack.study-run/1','lab_id':lab.lab_id,'run_id':run_id,
        'pack_id':pack.manifest['pack_id'],'pack_sha256':manifest_sha,'runner':generator_fingerprint(),
        'source_kind':'software_fixture','human_signatures':False})
    results=run_study_steps(session,root)
    load_pack(pack.root)
    return results
