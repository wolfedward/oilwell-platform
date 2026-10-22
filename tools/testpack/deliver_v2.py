"""Stage an immutable v2 pack, then start only its freshly owned validation lab.

Preparation/readiness is deliberately separate from the opt-in live test. No
production instance, existing directory or historical report is adopted.
"""
import argparse
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

from tools import review_deploy as deploy
from tools.review_deploy_paths import claim_target,private_acl,write_json_private,replace_json_private,verify_target
from tools.review_deploy_source import export_committed_source,verify_export
from .lab import validate_lab_target,validate_lab_ports,V2_PORTS,initialize_lab,load_lab,stop_lab
from .storage import canonical_bytes,load_pack,safe_path,strict_json,generator_commit_v2


def require_offline_proofs(report,positives):
    """A generic green offline report without every finite proof cannot launch."""
    checks=report.get('checks',[])
    if report.get('status')!='passed' or not positives:raise ValueError('offline validation blocked')
    for case in positives:
        rows=[r for r in checks if r.get('check_id')==case+'-finite-inversion']
        if len(rows)!=1:raise ValueError('missing or duplicate finite proof')
        row=rows[0];actual=row.get('actual',{})
        if (row.get('status')!='passed' or actual.get('source_proof_status')!='verified' or
            actual.get('passed') is not True or actual.get('native_points')!=1025 or
            actual.get('field_accuracy_certified') is not False or actual.get('approved_execution_status')!='not_run'):
            raise ValueError('finite proof not approved for software validation')
        for key in ('original_prediction_coverage','joint_coverage'):
            if not Fraction(4,5)<=Fraction(str(actual[key]))<=1:raise ValueError('original coverage gate')
        if actual.get('load_rmse_N') is None or not 0<=Fraction(str(actual['load_rmse_N']))<=500:
            raise ValueError('comparison RMSE gate')


def rollback_plan(release,instances):
    if (release.get('schema')!='owp.testpack.v2-release/1' or
        set(instances)!={'monitoring','review'} or release.get('instances')!=instances):
        raise ValueError('rollback instance ownership mismatch')
    return tuple(instances.values())


def _clean(source):
    source=Path(source).resolve(strict=True)
    if not (source/'.git').exists():raise ValueError('explicit source repository required')
    status=subprocess.run(['git','-C',str(source),'status','--porcelain=v1','--untracked-files=all'],
        check=True,capture_output=True,timeout=15)
    if status.stdout:raise ValueError('release requires completely clean committed HEAD')
    return source


def closure(files):
    names={name for name in files if (name.startswith('tools/testpack/') and name.endswith('.py')) or
           (name.startswith('config/testpack/') and name.endswith('.json'))}
    names.update('backend/inversion/'+name for name in ('synthetic_registry.py','interval_runtime.py','contracts.py','transactions.py'))
    return {name:files[name] for name in sorted(names)}


def _child(stage,args,log,timeout):
    environment={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','PGPASSWORD')
        and not k.startswith(('PLATFORM_','REVIEW_'))}
    environment.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    with log.open('xb') as output:
        result=subprocess.run([str(stage/'venv/Scripts/python.exe'),'-B','-m','tools.testpack',*map(str,args)],
            cwd=stage/'code',env=environment,stdout=output,stderr=subprocess.STDOUT,timeout=timeout,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise ValueError('exported validation failed; retained log: '+str(log))


def generation_timeout(profile):
    # Whole offline pack construction, not the immutable 180s per-run deadline.
    # Extended enumerates 44 independent trajectories (smoke enumerates four).
    if profile=='smoke':return 900
    if profile=='extended':return 7200
    raise ValueError('unknown validation profile')


def deliver(root:Path,source:Path,*,profile:str):
    if profile not in ('smoke','extended'):raise ValueError('unknown validation profile')
    root=validate_lab_target(Path(root));source=_clean(source)
    ports=validate_lab_ports(V2_PORTS)
    root.mkdir();private_acl(root)
    release={'schema':'owp.testpack.v2-release/1','root':str(root),'profile':profile,
             'status':'preparing','approved_execution_status':'not_run','instances':{}}
    write_json_private(root/'release.json',release)
    stage=root/'source';marker=claim_target(stage,forbidden=(source,))
    marker['inversion_v2']=True
    write_json_private(stage/'private/marker.json',marker)
    deploy._private_directories(stage)
    marker['source']=export_committed_source(source,stage/'code');deploy._update(stage,marker)
    # Assert the entire generator closure was exported, not only a handpicked subset.
    tracked=subprocess.run(['git','-C',str(source),'ls-tree','-r','--name-only','HEAD'],
        check=True,capture_output=True,timeout=15).stdout.decode().splitlines()
    expected=closure({name:'' for name in tracked})
    if set(expected)!=set(closure(marker['source']['files'])):raise ValueError('generator source closure omitted')
    deploy._venv(stage)
    release.update(source=marker['source'],stage=str(stage));replace_json_private(root/'release.json',release)
    pack=root/('pack-v2-'+profile);offline=root/('offline-v2-'+profile+'.json')
    _child(stage,['generate','--config',stage/'code/config/testpack'/('inversion-v2-'+profile+'.json'),
        '--output',pack],root/'generate.log',generation_timeout(profile))
    _child(stage,['validate','--pack',pack,'--report',offline],root/'offline.log',7200)
    from .inversion_run_v2 import positive_case_ids
    package=load_pack(pack)
    require_offline_proofs(strict_json(offline.read_bytes()),positive_case_ids(package))
    lab=initialize_lab(root/('lab-v2-'+profile),pack=package,source=source,ports=ports)
    instances={p:verify_target(lab.root/p)['instance_id'] for p in ('monitoring','review')}
    release.update(instances=instances,lab=str(lab.root),pack=str(pack),offline=str(offline))
    replace_json_private(root/'release.json',release)
    for child in (lab.monitoring_root,lab.review_root):
        deployed=verify_target(child)
        verify_export(child/'code',deployed['source'])
        if deployed['source']!=marker['source']:raise ValueError('deployed source differs from validated export')
        deploy.launch(child)
    release.update(status='ready',approved_execution_status='not_run')
    replace_json_private(root/'release.json',release)
    return release


def rollback(root):
    root=safe_path(Path(root));release=strict_json((root/'release.json').read_bytes())
    if release.get('root')!=str(root):raise ValueError('release root mismatch')
    lab=load_lab(root/('lab-v2-'+release['profile']))
    instances={p:verify_target(lab.root/p)['instance_id'] for p in ('monitoring','review')}
    ids=rollback_plan(release,instances)
    for p in instances:
        if verify_target(lab.root/p).get('inversion_v2') is not True:raise ValueError('not a v2 instance')
    stop_lab(lab)
    receipt={'stopped_instance_ids':list(ids),'preserved_data':True,'new_runs_enabled':False}
    write_json_private(root/'rollback.json',receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','stop'))
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--profile',choices=('smoke','extended'),default='smoke')
    args=parser.parse_args()
    if args.action=='prepare':
        if args.source is None:parser.error('--source required')
        result=deliver(args.root,args.source,profile=args.profile)
    else:result=rollback(args.root)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
