"""One owned smoke run, explicit completeness gates and a real browser check."""
from dataclasses import asdict
from datetime import datetime,timezone
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import time

from tools.review_deploy_paths import private_acl,write_json_private
from .contracts import CheckResult
from .lab import checked_pack,run_context,finish_context
from .storage import canonical_bytes,safe_path,strict_json
from .report import REQUIRED,write_report,export_public_report,overall_status
from .validate import validate_pack
from .viewer import serve_viewer


def summarize(name,layer,expected_ids,checks):
    ids=[c.check_id for c in checks]
    if len(set(ids))!=len(ids):raise ValueError('duplicate observed checks')
    selected=[c for c in checks if c.check_id in expected_ids]
    if any(c.layer!=layer for c in selected):raise ValueError('summary layer differs from observed evidence')
    missing=sorted(set(expected_ids)-set(ids))
    statuses=[c.status for c in selected]
    status=('failed' if 'failed' in statuses else 'blocked' if 'blocked' in statuses else
            'not_run' if missing or 'not_run' in statuses or not selected else 'passed')
    return CheckResult(name,layer,status,{'required_checks':list(expected_ids)},
        {'observed_checks':[{'id':c.check_id,'status':c.status} for c in selected],'missing':missing})


def browser_check(pack,public_report,root):
    server=serve_viewer(pack,public_report)
    runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node=os.environ.get('OWP_TESTPACK_NODE') or shutil.which('node') or str(runtime/'bin/node.exe')
    env=dict(os.environ)
    if 'NODE_PATH' not in env and (runtime/'node_modules').is_dir():env['NODE_PATH']=str(runtime/'node_modules')
    started=time.monotonic()
    try:
        result=subprocess.run([node,str(Path(__file__).with_name('verify_viewer.cjs')),
            f'http://127.0.0.1:{server.server_port}/',pack.manifest['pack_id'],str(root/'viewer.png')],
            capture_output=True,timeout=90,env=env)
        if result.returncode:raise RuntimeError('browser process failed')
        actual=strict_json(result.stdout)
        status='passed' if actual.get('ok') is True else 'failed'
        # The browser is a separate actual check, not a reused fixture result.
        actual.update(pack_sha256=sha256((pack.root/'manifest.json').read_bytes()).hexdigest(),
            viewed_report_sha256=sha256(Path(public_report).read_bytes()).hexdigest())
        if (root/'viewer.png').is_file():actual['screenshot_sha256']=sha256((root/'viewer.png').read_bytes()).hexdigest()
        elif status=='passed':raise ValueError('successful browser check lacks screenshot')
    except Exception as error:status='failed';actual={'error_type':type(error).__name__}
    finally:server.shutdown();server.server_close()
    actual['elapsed_seconds']=round(time.monotonic()-started,6)
    check=CheckResult('viewer-interaction','browser',status,{'actual_pack_and_report':True},actual)
    write_json_private(root/'browser-check.json',asdict(check))
    return check


def run_delivery(lab,pack,run_root):
    from .telemetry_run import run_telemetry,STEPS as TELEMETRY
    from .waveform_run import run_waveforms,run_waveform_guards
    from .study_run import run_studies,STEPS as STUDIES
    pack=checked_pack(lab,pack)
    if pack.manifest['profile']!='smoke':raise ValueError('network delivery uses the registered smoke subset only')
    root=safe_path(Path(run_root),missing=True)
    if root.exists() or root.parent!=lab.root:raise ValueError('new direct owned lab child required')
    roots={name:root.with_name(root.name+'-'+name) for name in ('telemetry','waves','guards','studies')}
    if any(path.exists() for path in roots.values()):raise ValueError('fresh stage directories required')
    root.mkdir();private_acl(root);context=run_context(lab,pack)
    write_json_private(root/'run.json',{'schema':'owp.testpack.delivery/1',**context})
    checks=[c for c in validate_pack(pack) if c.check_id not in REQUIRED]
    def execute(name,runner):
        try:
            result=runner()
            checks.extend(result if isinstance(result,tuple) else (result,))
        except Exception as error:
            checks.append(CheckResult('stage-'+name,'software_network' if name=='telemetry' else 'http','failed',{},
                {'error_type':type(error).__name__}))
        write_json_private(root/(name+'-checkpoint.json'),{'checks':[asdict(c) for c in checks]})
    execute('telemetry',lambda:run_telemetry(lab,pack,roots['telemetry']))
    execute('waves',lambda:run_waveforms(lab,pack,roots['waves']))
    execute('guards',lambda:run_waveform_guards(lab,pack,roots['guards'],roots['waves']))
    execute('studies',lambda:run_studies(lab,pack,roots['studies']))
    records=roots['studies']/'final-records.json'
    if records.is_file():
        checks.append(CheckResult('study-results','http','passed',{'source':'actual_export'},
            {'study_records':list(strict_json(records.read_bytes()).values()),'export_sha256':sha256(records.read_bytes()).hexdigest()}))
    cycles=[c['case_id'] for c in pack.manifest['cases'] if c['facts'].get('data_kind')=='cycle']
    for name,layer,expected in [('modbus-mqtt-storage','software_network',['telemetry-'+s for s in TELEMETRY]),
        ('waveform-evidence-http','http',[c+'-waveform' for c in cycles]+['waveform-source-revocation-guards']),
        ('studies-http','http',['study-'+s for s in STUDIES]),
        ('inversion-comparison','numeric',[c+'-approved-inversion' for c in cycles])]:
        checks.append(summarize(name,layer,expected,checks))
    metadata={k:context[k] for k in ('run_id','lab_id','started_at','deployment_commits')}
    metadata['source_commit']=context['runner']['source_commit']
    # Snapshot before browser execution is retained; it correctly says browser not_run.
    preview=root/'pre-browser';preview.mkdir()
    write_report(pack,checks,preview,run_metadata=metadata)
    preview_public=export_public_report(preview,root/'pre-browser-public')
    checks.append(browser_check(pack,preview_public,root))
    metadata['finished_at']=datetime.now(timezone.utc).isoformat()
    write_report(pack,checks,root,run_metadata=metadata)
    public=export_public_report(root,root/'public')
    finish_context(root)
    return tuple(checks),public
