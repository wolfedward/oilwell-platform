"""Negative assertions against fresh owned HTTP resources, with exact reasons."""
from contextlib import contextmanager
from pathlib import Path
import time

from .inversion_http_v2 import InversionOutcome
from .lab_http import LabHttpError


def expect_refusal(case_id,phase,status,code,action):
    try:action()
    except LabHttpError as error:
        if (error.status,error.code)!=(status,code):
            raise ValueError('negative assertion encountered the wrong status/reason') from error
        return InversionOutcome(case_id,'passed','refused','refused',phase,code,'refused',{'http_status':status})
    raise ValueError('negative operation unexpectedly succeeded')


@contextmanager
def temporary_permissions(http,*,add=frozenset(),remove=frozenset()):
    """Only the owned test algorithm role; exact optimistic restoration even on error."""
    roles=http.request('GET','/api/roles',actor='administrator')['data']
    selected=[r for r in roles if r['code']=='algorithm_engineer' and r['status']=='active']
    if len(selected)!=1:raise ValueError('unique active test role required')
    before=selected[0];path='/api/roles/'+str(before['id'])
    permissions=sorted((set(before['permissions'])|set(add))-set(remove))
    if set(permissions)==set(before['permissions']):raise ValueError('guard must actually change permissions')
    try:
        changed=http.request('PATCH',path,{'version':before['version'],'permissions':permissions},actor='administrator')['data']
        if set(changed['permissions'])!=set(permissions):raise ValueError('test role mutation not confirmed')
        yield
    finally:
        current=http.request('GET',path,actor='administrator')['data']
        if current['code']!=before['code'] or current['status']!=before['status']:raise ValueError('test role identity changed')
        if set(current['permissions'])!=set(before['permissions']):
            if set(current['permissions'])!=set(permissions):raise ValueError('concurrent permission change; refuse overwrite')
            http.request('PATCH',path,{'version':current['version'],'permissions':before['permissions']},actor='administrator')
            restored=http.request('GET',path,actor='administrator')['data']
            if set(restored['permissions'])!=set(before['permissions']):raise ValueError('test role restoration failed')


def wait_running(session,run_id):
    until=time.monotonic()+30
    while time.monotonic()<until:
        current=session.get('/runs/'+run_id)
        if current['state']=='running':return current
        if current['state']!='queued':raise ValueError('guard missed running state')
        time.sleep(.1)
    raise TimeoutError('owned worker did not claim guard run')


def selected_request(session,facts,model):
    from .inversion_run_v2 import post,run_request
    parameter=facts['parameter']
    selection=post(session,'/selections',dict(assembly_id=parameter['assembly_id'],parameter_id=parameter['id'],
        parameter_sha256=parameter['content_sha256'],parameter_review_version=parameter['review_version'],
        model_id=model['id'],model_sha256=model['content_sha256'],model_review_version=model['review_version']))
    return run_request(parameter,model,facts['pair'],facts['dataset'],selection)


def run_guards(lab,pack,root,session,model):
    from .inversion_run_v2 import (prepare_case,V2WaveformSession,post,approved_execution,save,outcome_wire,AUTHOR,REVIEWER)
    from tools.review_deploy_paths import private_acl
    outcomes=[]
    def fresh(name,callback=None):
        directory=root/name;directory.mkdir();private_acl(directory)
        wave=V2WaveformSession(lab,directory)
        facts=prepare_case(wave,session,pack,'cycle-base',directory,before_review=callback)
        return directory,facts
    def record(outcome):
        outcomes.append(outcome);save(root,outcome.case_id+'-outcome.json',outcome_wire(outcome))
    for name,status,code in (('permission',403,'forbidden'),('self-review',409,'inversion.self_review'),
                             ('stale-review',409,'inversion.version_conflict')):
        def guard(client,parameter,name=name,status=status,code=code):
            action=lambda:post(client,'/parameters/'+parameter['id']+'/review',{'decision':'approve'},
                version=parameter['review_version']-(1 if name=='stale-review' else 0),
                actor=REVIEWER if name=='stale-review' else AUTHOR,status=200)
            if name=='self-review':
                with temporary_permissions(client.http,add={'inversion.parameters.review'}):
                    actual=expect_refusal('guard-'+name,'parameter_review',status,code,action)
            else:actual=expect_refusal('guard-'+name,'parameter_review',status,code,action)
            record(actual)
        fresh('guard-'+name,guard)
    directory,facts=fresh('guard-wrong-pin')
    request=selected_request(session,facts,model)
    wrong=dict(request,parameter_sha256='0'*64)
    record(expect_refusal('guard-wrong-pin','run_create',409,'inversion.parameter_pin_changed',lambda:post(session,'/runs',wrong,status=202)))
    directory,facts=fresh('guard-preflight-revoke')
    request=selected_request(session,facts,model)
    if session.preflight(request)['allowed'] is not True:raise ValueError('guard preflight did not pass first')
    post(session,'/parameters/'+facts['parameter']['id']+'/revoke',version=facts['parameter']['review_version'],actor=REVIEWER,status=200)
    record(expect_refusal('guard-preflight-revoke','run_create',409,'inversion.parameter_pin_changed',lambda:post(session,'/runs',request,status=202)))
    directory,facts=fresh('guard-permission-loss')
    request=selected_request(session,facts,model);queued=post(session,'/runs',request,status=202)
    wait_running(session,queued['run_id'])
    with temporary_permissions(session.http,remove={'waveform.raw.read'}):
        terminal=session.wait_run(queued['run_id'],deadline=time.monotonic()+210)
        if (terminal['state'],terminal['reason_code'],terminal.get('result_id'))!=('blocked','authority_changed',None):
            raise ValueError('running permission loss not enforced')
    record(InversionOutcome('guard-permission-loss','passed','refused','refused','publication','authority_changed','refused',{'run':terminal,'permissions_restored':True}))
    directory,facts=fresh('guard-result-revoke')
    actual=approved_execution(session,facts,model,directory,'cycle-base')
    previous=actual['result'];parameter=facts['parameter']
    post(session,'/parameters/'+parameter['id']+'/revoke',version=parameter['review_version'],actor=REVIEWER,status=200)
    current=session.get('/results/'+previous['id'])
    if current['content_sha256']!=previous['content_sha256'] or current['current_applicability']['state']!='restricted':
        raise ValueError('historical result mutated or remained quantitative')
    refused=expect_refusal('guard-result-revoke','export_create',409,'inversion.quantitative_export_restricted',
        lambda:post(session,'/exports',dict(result_id=previous['id'],intent='quantitative',format='json')))
    evidence=post(session,'/exports',dict(result_id=previous['id'],intent='evidence',format='json'),actor='administrator')
    payload=session.download(evidence['id'],actor='administrator')
    with (directory/'restricted-evidence-export.json').open('xb') as stream:stream.write(payload)
    record(InversionOutcome(refused.case_id,refused.assertion_status,refused.expected_outcome,refused.observed_outcome,
        refused.phase,refused.reason_code,refused.approved_execution_status,
        dict(before_sha256=previous['content_sha256'],after_sha256=current['content_sha256'],current=current,evidence_export=evidence)))
    # The process fault harness is external to the server and keeps supervisors
    # alive. It must attest the exact fresh credential-free compute child.
    from .owned_compute_fault_v2 import pause_compute
    for mode in ('cancel','deadline'):
        directory,facts=fresh('guard-'+mode)
        request=selected_request(session,facts,model);started=time.monotonic()
        queued=post(session,'/runs',request,status=202);wait_running(session,queued['run_id'])
        with pause_compute(lab,request) as paused:
            if mode=='cancel':
                running=session.get('/runs/'+queued['run_id'])
                post(session,'/runs/'+queued['run_id']+'/cancel',version=running['version'],status=200)
            terminal=session.wait_run(queued['run_id'],deadline=time.monotonic()+210)
            expected=('cancelled','user_cancelled') if mode=='cancel' else ('failed','deadline_expired')
            if (terminal['state'],terminal['reason_code'])!=expected or terminal.get('result_id') is not None:
                raise ValueError('owned process terminal state differs')
            paused.require_terminated()
        health=session.get('/health')['capacity']
        if any(health[k]!=0 for k in ('running','queued','reserved_bytes')):raise ValueError('owned capacity not released')
        again=session.get('/runs/'+queued['run_id'])
        if again!=terminal:raise ValueError('late result or terminal mutation observed')
        record(InversionOutcome('guard-'+mode,'passed','refused','refused','worker',terminal['reason_code'],'refused',
            dict(run=terminal,elapsed_seconds=time.monotonic()-started,capacity=health,owned_compute_reaped=True,
                 no_late_result=True,stale_publication_attempt='not_executed; separate pure generation/owner fence test')))
    return tuple(outcomes)
