"""Explicit opt-in integration entry; never discovers or reuses a business DSN."""
import os
import json
import subprocess
import time
from hashlib import sha256
from pathlib import Path

from tools.review_deploy import _marker as component_marker
from tools.review_deploy_process import inspect_pid,owned_process
from .lab import checked_pack,load_lab
from .storage import load_pack,safe_path

GUARDS={
    'guard-permission':('parameter_review','forbidden'),
    'guard-self-review':('parameter_review','inversion.self_review'),
    'guard-stale-review':('parameter_review','inversion.version_conflict'),
    'guard-wrong-pin':('run_create','inversion.parameter_pin_changed'),
    'guard-preflight-revoke':('run_create','inversion.parameter_pin_changed'),
    'guard-permission-loss':('publication','authority_changed'),
    'guard-result-revoke':('export_create','inversion.quantitative_export_restricted'),
    'guard-cancel':('worker','user_cancelled'),
    'guard-deadline':('worker','deadline_expired'),
}


def require_complete(outcomes,positives):
    from .inversion_run_v2 import FAULTS
    from .report_inversion_v2 import project
    expected=set(positives)|set(FAULTS)|set(GUARDS)
    if len(outcomes)!=len(expected) or {o.case_id for o in outcomes}!=expected:
        raise ValueError('inversion outcome inventory incomplete or duplicated')
    for outcome in outcomes:
        if outcome.assertion_status!='passed':raise ValueError('inversion assertion failed')
        if outcome.case_id in positives:
            project(outcome)  # Requires proof, real IDs, approved result, export and metrics.
            if (outcome.phase,outcome.reason_code,outcome.approved_execution_status)!=('export','','passed'):
                raise ValueError('positive execution incomplete')
        else:
            if outcome.approved_execution_status!='refused' or (outcome.expected_outcome,outcome.observed_outcome)!=('refused','refused'):
                raise ValueError('negative assertion not an observed refusal')
            if outcome.case_id in GUARDS:
                if (outcome.phase,outcome.reason_code)!=GUARDS[outcome.case_id]:raise ValueError('guard phase/code differs')
            elif outcome.case_id=='cycle-missing':
                if (outcome.phase,outcome.reason_code)!=('waveform','missing_samples'):raise ValueError('missing-sample phase/code differs')
            else:
                facts=outcome.facts
                expected_reason=facts['reasons'][0] if facts['reasons'] else 'insufficient_coverage'
                if (outcome.phase!='budget' or outcome.reason_code!=expected_reason or facts['passed'] is not False
                    or facts['source_proof_status']!='verified'):raise ValueError('fault budget refusal differs')
    return True


def live_paths(env=None):
    env=os.environ if env is None else env
    if env.get('OILWELL_TESTPACK_INVERSION_V2_LIVE')!='1':return None
    names=('OILWELL_TESTPACK_LIVE_ROOT','OILWELL_TESTPACK_LIVE_PACK','OILWELL_TESTPACK_LIVE_DELIVERY')
    if any(not env.get(name) for name in names):raise ValueError('explicit live mode requires all owned paths')
    return tuple(Path(env[name]) for name in names)


def verify_owned_runtime(lab,pack):
    pack=checked_pack(lab,pack)
    for root in (lab.monitoring_root,lab.review_root):
        marker=component_marker(root)
        if marker['state']!='running' or not marker['processes']:raise ValueError('owned runtime is not running')
        required={'http','broker','center','reliability_publisher','wave_receiver','wave_assembler','wave_publisher','inversion'}
        if not required<=set(marker['processes']):raise ValueError('owned runtime components missing')
        for expected in marker['processes'].values():
            if not owned_process(expected,inspect_pid(expected['pid'])):raise ValueError('owned process identity changed')
    return pack


def run(root:Path,pack:Path,delivery:Path):
    lab=load_lab(root);package=verify_owned_runtime(lab,load_pack(pack))
    destination=safe_path(delivery,missing=True)
    if destination.parent!=lab.root or destination.exists():raise ValueError('new owned run directory required')
    # The unittest is in the worktree, but the workflow MUST run against exactly
    # the same LF blob bytes as the installed checker/generator, not that worktree.
    from tools.review_deploy_source import verify_export
    marker=component_marker(lab.review_root);verify_export(lab.review_root/'code',marker['source'])
    environment={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','PGPASSWORD')
        and not k.startswith(('REVIEW_','PLATFORM_'))}
    environment.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    log=lab.root/'private'/('live-'+destination.name+'.log')
    with log.open('xb') as output:
        result=subprocess.run([str(lab.review_root/'venv/Scripts/python.exe'),'-B','-m',
            'tools.testpack.owned_v2_verify','--root',str(root),'--pack',str(pack),'--delivery',str(delivery)],
            cwd=lab.review_root/'code',env=environment,stdout=output,stderr=subprocess.STDOUT,timeout=21600,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise ValueError('owned workflow failed; inspect '+str(log))
    from .storage import strict_json
    from .inversion_http_v2 import InversionOutcome
    from .inversion_run_v2 import positive_case_ids
    path=safe_path(destination/'inversion-outcomes.json')
    if path.stat().st_size>256*1024**2:raise ValueError('workflow result size exceeded')
    outcomes=tuple(InversionOutcome(**row) for row in strict_json(path.read_bytes())['outcomes'])
    require_complete(outcomes,positive_case_ids(package))
    receipt=strict_json(safe_path(destination/'verified-live.json').read_bytes())
    if receipt['outcomes_sha256']!=sha256(path.read_bytes()).hexdigest() or receipt['source_commit']!=marker['source']['commit']:
        raise ValueError('live receipt binding differs')
    return {'outcomes':outcomes,'passed':True,'receipt':receipt}


def execute_owned(root,pack,delivery):
    from .inversion_run_v2 import run_inversion_cases,positive_case_ids,save
    from .resources_v2 import assert_fresh,staging,measure
    from .lab_http import LabHttp
    from .storage import generator_fingerprint_v2,strict_json
    from .report import write_report,export_public_report
    from .contracts import CheckResult
    from tools.review_deploy_numeric import verify as verify_numeric
    lab=load_lab(root);package=verify_owned_runtime(lab,load_pack(pack));destination=Path(delivery)
    code=lab.review_root/'code'
    if Path(__file__).resolve().parents[2]!=code.resolve() or Path.cwd().resolve()!=code.resolve():
        raise ValueError('live workflow must execute from owned exported source')
    if generator_fingerprint_v2()!=package.manifest['generator']:raise ValueError('installed generator differs from pack')
    for child in (lab.monitoring_root,lab.review_root):verify_numeric(child)
    namespace=assert_fresh(lab);before_staging=staging(lab)
    http=LabHttp(lab,'review');me=http.login('algorithm_engineer')
    roles=http.request('GET','/api/roles',actor='administrator')['data']
    role=next(r for r in roles if r['code']=='algorithm_engineer')
    baseline={k:role[k] for k in ('id','code','status','permissions')}
    outcomes=run_inversion_cases(lab,package,destination)
    require_complete(outcomes,positive_case_ids(package))
    current=http.request('GET','/api/roles/'+str(role['id']),actor='administrator')['data']
    if any(current[k]!=v for k,v in baseline.items()):raise ValueError('test role permissions not restored')
    after_me=http.login('algorithm_engineer')
    if me.get('permissions')!=after_me.get('permissions'):raise ValueError('reauthenticated permissions changed')
    deadline=time.monotonic()+20
    while staging(lab)!=before_staging and time.monotonic()<deadline:time.sleep(.25)
    if staging(lab)!=before_staging:raise ValueError('owned staging not restored')
    resources=measure(lab);save(destination,'resource-observations.json',resources)
    origin=strict_json((destination/'run.json').read_bytes())
    finished=strict_json((destination/'finished.json').read_bytes())
    offline=strict_json((destination/'offline-checks.json').read_bytes())
    checks=[CheckResult(**row) for row in offline['checks']]
    checks.append(CheckResult('inversion-comparison','numeric','passed',{'scope':'owned software'},
        {'approved_results':len(positive_case_ids(package)),'field_accuracy_certified':False}))
    metadata={k:origin[k] for k in ('run_id','lab_id','started_at','deployment_commits')}
    metadata.update(finished_at=finished['finished_at'],source_commit=origin['runner']['source_commit'])
    write_report(package,checks,destination,run_metadata=metadata,inversion_outcomes=outcomes)
    public=export_public_report(destination,destination/'public')
    receipt={'schema':'owp.testpack.live-verification/2','passed':True,'outcome_count':len(outcomes),
        'source_commit':origin['runner']['source_commit'],'source_kind':'synthetic','field_accuracy_certified':False,
        'outcomes_sha256':sha256((destination/'inversion-outcomes.json').read_bytes()).hexdigest(),
        'resources_sha256':sha256((destination/'resource-observations.json').read_bytes()).hexdigest(),
        'public_report_sha256':sha256(public.read_bytes()).hexdigest(),'namespace':namespace,
        'permissions_restored':True,'staging_restored':True}
    save(destination,'verified-live.json',receipt)
    return receipt


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--pack',type=Path,required=True);parser.add_argument('--delivery',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(execute_owned(args.root,args.pack,args.delivery),ensure_ascii=False),flush=True)
