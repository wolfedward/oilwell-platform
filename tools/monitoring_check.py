"""Explicit layered gate. Never discovers legacy destructive integration suites."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

from tools.review_deploy_paths import write_json_private, private_acl

SUITES = {
    'unit': ('tests.test_monitoring_runtime','tests.test_monitoring_api','tests.test_monitoring_session',
             'tests.test_monitoring_deploy','tests.test_review_deploy','tests.test_registry_contracts'),
    'browser': ('tests.test_monitoring_shell_browser','tests.test_monitoring_registry_browser',
                'tests.test_monitoring_telemetry_browser','tests.test_monitoring_quality_browser',
                'tests.test_registry_browser','tests.test_ingest_browser'),
    'integration': ('tests.test_monitoring_privileges_pg','tests.test_monitoring_wells_pg',
                    'tests.test_monitoring_history_pg','tests.test_monitoring_acquisition_pg',
                    'tests.test_monitoring_quality_pg','tests.test_monitoring_review_pg','tests.test_monitoring_e2e'),
}


def successful(result):
    return result.wasSuccessful() and result.testsRun > 0 and not result.skipped


class EvidenceResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.passed = []

    def addSuccess(self,test):
        self.passed.append(test.id())
        super().addSuccess(test)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layer',choices=(*SUITES,'all'),required=True)
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args(argv)
    report = args.report.absolute()
    report.parent.mkdir(parents=True,exist_ok=True)
    private_acl(report.parent)
    # Evidence file is unique; never reuse an old successful receipt.
    evidence_path = report.parent/('monitoring-network-'+uuid4().hex+'.json')
    previous = os.environ.get('MONITORING_EVIDENCE_PATH')
    os.environ['MONITORING_EVIDENCE_PATH'] = str(evidence_path)
    layers = list(SUITES) if args.layer == 'all' else [args.layer]
    results = {}; ok = True
    try:
        for layer in layers:
            started = time.monotonic()
            log = report.parent/('monitoring-'+layer+'-'+uuid4().hex+'.log')
            with log.open('x',encoding='utf-8') as stream:
                private_acl(log)
                suite = unittest.defaultTestLoader.loadTestsFromNames(SUITES[layer])
                result = unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=EvidenceResult).run(suite)
            results[layer] = {'passed':len(result.passed),'run':result.testsRun,'failures':len(result.failures),
                              'errors':len(result.errors),'required_skips':len(result.skipped),
                              'ok':successful(result),'seconds':round(time.monotonic()-started,2),
                              'log':str(log),'passed_tests':result.passed,
                              'scope':'browser API doubles' if layer=='browser' else
                                      'fresh owned PostgreSQL plus actual HTTP/MQTT' if layer=='integration' else 'contracts and isolated boundaries'}
            ok = ok and successful(result)
            print(f'{layer}: {len(result.passed)}/{result.testsRun}; required skips={len(result.skipped)}',flush=True)
    finally:
        if previous is None: os.environ.pop('MONITORING_EVIDENCE_PATH',None)
        else: os.environ['MONITORING_EVIDENCE_PATH'] = previous
    import json
    evidence = json.loads(evidence_path.read_text(encoding='utf-8')) if evidence_path.exists() else None
    network_verified = bool(evidence and evidence.get('restart_verified'))
    if 'integration' in layers and not network_verified: ok = False
    pg_passed = sum('_pg.' in name or name.endswith('test_new_fixture_uses_monitoring_profile_and_retains_five_accounts')
                    for name in results.get('integration',{}).get('passed_tests',[]))
    summary = {'format':1,'created_at':datetime.now(timezone.utc).isoformat(),'ok':ok,'layers':results,
               'unit':results.get('unit'),'browser':results.get('browser'),
               'postgres':{'passed_tests':pg_passed,'owned_clusters_only':True},
               'http':{'actual_network_verified':network_verified,'roles_checked':evidence.get('roles_checked',0) if evidence else 0},
               'mqtt':{'actual_broker_verified':network_verified,'evidence':str(evidence_path) if evidence else None},
               'required_skips':sum(r['required_skips'] for r in results.values()),
               'forbidden_requests':evidence.get('forbidden_requests',[]) if evidence else [],
               'field_validation':False}
    # Report is an explicit user-selected output, atomically replaced only here.
    from tools.review_deploy_paths import replace_json_private
    (replace_json_private if report.exists() else write_json_private)(report,summary)
    print(str(report),flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
