"""Fresh, source-bound isolated inversion verification. Imports are passive.

The private configuration pins a separately reviewed post-fixture security
baseline. This tool never initializes, repairs, or starts a database service.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
GIT=Path('C:/Users/hp/AppData/Local/hermes/git/cmd/git.exe')


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def source_inventory():
    names=subprocess.check_output([str(GIT),'ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT)
    return {name:sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(set(
        names.decode('utf-8').rstrip('\0').split('\0'))) if name and (ROOT/name).is_file()}


def qualify(report):
    try:
        ids=report['discovered']; started=report['started']; terminal=report['terminal']
        return bool(ids and len(set(ids))==len(ids) and started==ids and
            [item['id'] for item in terminal]==ids and report['tests_run']==len(ids) and
            all(item['outcome']=='success' and isinstance(item['duration_seconds'],(int,float))
                and 0<=item['duration_seconds']<float('inf') for item in terminal) and
            report['loader_errors']==0 and report['unittest_ok'] is True and
            report['categories']=={} and report['source_before'] and
            report['source_before']==report['source_after']==report['parent_source_before']==report['parent_source_after'] and
            report['source_sha256']==sha256(encoded(report['parent_source_before'])).hexdigest() and
            report['security_ok'] is True and
            report['cleanup_ok'] is True and report['interrupted'] is False)
    except (KeyError,TypeError,ValueError): return False


class Results(unittest.TestResult):
    """One terminal record per case, with redacted callback accounting."""
    def __init__(self,sensitive_ids=(),sink=None):
        super().__init__(); self.sensitive_ids=set(sensitive_ids); self.sink=sink
        self.started=[]; self.terminal=[]; self.outcomes=[]; self._active={}

    def startTest(self,test):
        super().startTest(test); self.started.append(test.id())
        self._active[test.id()]=dict(start=time.monotonic(),categories=[])
        if self.sink: self.sink(dict(phase='case_started',test_id=test.id()))

    def _record(self,test,category):
        self.outcomes.append((test.id(),category))
        if test.id() not in self._active:
            if self.sink: self.sink(dict(phase='fixture_failed',test_id=test.id(),category=category))
            self.stop(); return
        self._active[test.id()]['categories'].append(category)
        if test.id() in self.sensitive_ids or category=='skip': self.stop()

    def addSuccess(self,test): pass
    def addError(self,test,err):
        self.errors.append((test,'redacted')); self._record(test,'error')
    def addFailure(self,test,err):
        self.failures.append((test,'redacted')); self._record(test,'failure')
    def addSkip(self,test,reason):
        self.skipped.append((test,'redacted')); self._record(test,'skip')
    def addExpectedFailure(self,test,err):
        self.expectedFailures.append((test,'redacted')); self._record(test,'expected_failure')
    def addUnexpectedSuccess(self,test):
        self.unexpectedSuccesses.append(test); self._record(test,'unexpected_success')
    def addSubTest(self,test,subtest,err):
        if err is not None:
            (self.addFailure if issubclass(err[0],test.failureException) else self.addError)(test,err)

    def stopTest(self,test):
        state=self._active.pop(test.id()); categories=state['categories']
        record=dict(id=test.id(),outcome=categories[0] if categories else 'success',
            categories=categories,duration_seconds=time.monotonic()-state['start'])
        self.terminal.append(record)
        if self.sink: self.sink(dict(phase='case_terminal',**record))
        super().stopTest(test)


def cases(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite): yield from cases(item)
        elif isinstance(item,unittest.TestCase): yield item
        else: raise TypeError('Concrete unittest cases required')


def write_fresh(path,value):
    with Path(path).open('xb') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())


class EvidenceSink:
    def __init__(self,path,limit=16*1024*1024):
        self.stream=Path(path).open('xb'); self.limit=limit; self.size=0
    def __call__(self,event):
        payload=encoded(event)+b'\n'
        if self.size+len(payload)>self.limit: raise RuntimeError('Evidence limit reached')
        self.stream.write(payload); self.stream.flush(); os.fsync(self.stream.fileno())
        self.size+=len(payload)
    def close(self): self.stream.close()


def run_suite(suite,provider,baseline,*,sink,source_before,loader_errors=0):
    from tests.inversion_verification_security import execute_cases, Evidence, _family
    ordered=tuple(cases(suite)); ids=[case.id() for case in ordered]
    if not ids or len(ids)!=len(set(ids)) or loader_errors:
        raise ValueError('Missing, duplicated, or unloadable discovered cases')
    write_ids=dict(phase='discovered',ids=ids,count=len(ids)); sink(write_ids)
    result=Results({case.id() for case in ordered if _family(case)},sink)
    start=time.monotonic()
    outcome=execute_cases(ordered,provider,expected_final=baseline,approved_setup_changes={},
        evidence=Evidence(sink=sink),result=result)
    return dict(discovered=ids,started=result.started,terminal=result.terminal,
        tests_run=result.testsRun,loader_errors=loader_errors,duration_seconds=time.monotonic()-start,
        categories=dict(Counter(category for _,category in result.outcomes)),
        source_before=source_before,source_after=source_inventory(),
        security_ok=outcome.security_ok,interrupted=outcome.interrupted,unittest_ok=outcome.unittest_ok,
        cleanup_ok=False)


def _environment():
    from tests.inversion_verification_environment import build_security_environment
    from tests.inversion_verification_security import CatalogSnapshotProvider
    authorized,connectors,gate=build_security_environment()
    return authorized,CatalogSnapshotProvider(authorized,connectors,full_identity_gate=gate)


def _set_opt_ins(authorized):
    for alias,var in [('registry','OILWELL_REGISTRY_TEST_CONFIG'),('ingest','OILWELL_INGEST_TEST_CONFIG'),
        ('waveform','OILWELL_WAVEFORM_TEST_CONFIG'),('inversion','OILWELL_INVERSION_TEST_CONFIG'),
        ('center','OILWELL_POSTGRES_TEST_CONFIG')]:
        os.environ[var]=str(authorized.configs[alias])
    os.environ['OILWELL_INGEST_PROVISION_IDENTITY']=str(authorized.identities['registry'])
    os.environ['OILWELL_TEST_UNUSED_MQTT']='isolated-unused-no-network'
    os.environ['PYTHONUTF8']='1'


def verification_scope(include_existing,selected_names=None):
    if selected_names is None: return 'test_*.py' if include_existing else 'test_inversion_*.py'
    if (type(selected_names) is not tuple or not 1<=len(selected_names)<=32 or
        any(not isinstance(name,str) or not re.fullmatch(
            r'tests\.test_[a-z0-9_]+\.[A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+',name)
            for name in selected_names) or len(set(selected_names))!=len(selected_names)):
        raise ValueError('Unique bounded canonical test method names required')
    return 'explicit-targeted'


def load_suite(include_existing,selected_names=None):
    """One discovery by default; internal affected-case reruns load names once."""
    scope=verification_scope(include_existing,selected_names)
    loader=unittest.TestLoader()
    if selected_names is None:
        suite=loader.discover(str(ROOT/'tests'),pattern=scope,top_level_dir=str(ROOT))
    else:
        suite=loader.loadTestsFromNames(selected_names)
        if loader.errors or [case.id() for case in cases(suite)]!=list(selected_names):
            raise ValueError('Requested test identities did not load exactly')
    return suite,len(loader.errors),scope


def _child(control,folder,baseline_values,source_before,include_existing,selected_names=None):
    if control.recv()!='contained': return
    control.close()
    os.environ['OILWELL_INVERSION_VERIFIER_CHILD']='1'
    folder=Path(folder); sink=EvidenceSink(folder/'cases.jsonl')
    try:
        from tests.inversion_verification_security import Snapshot, SECURITY_TEST_IDS
        authorized,provider=_environment(); _set_opt_ins(authorized)
        if Snapshot(baseline_values).differences(provider.snapshot()):
            raise RuntimeError('Security baseline changed before discovery')
        suite,loader_errors,scope=load_suite(include_existing,selected_names)
        # Reuse this single concrete suite; no second discovery for the denominator.
        ids={case.id() for case in cases(suite)}
        required=(set(SECURITY_TEST_IDS) if include_existing else {x for x in SECURITY_TEST_IDS if x.startswith('tests.test_inversion_')}) if selected_names is None else set()
        if not required<=ids: raise RuntimeError('Missing declared security coverage')
        report=run_suite(suite,provider,Snapshot(baseline_values),sink=sink,
            source_before=source_before,loader_errors=loader_errors)
        report.update(scope=scope,requested_ids=list(selected_names) if selected_names is not None else None)
        write_fresh(folder/'child-result.json',report)
    except BaseException as error:
        sink(dict(phase='child_failed',category=type(error).__name__))
        raise SystemExit(1) from None
    finally: sink.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--include-existing-regression',action='store_true')
    args=parser.parse_args(argv)
    # Live setup and the external supervisor are deliberately explicit.
    return supervise(args)


def supervise(args,*,selected_names=None):
    """Parent remains outside the test job and always checks catalog postmortem."""
    import multiprocessing
    from uuid import uuid4
    from tests.inversion_verification_security import Snapshot
    from backend.inversion.worker import OwnedJob, _OwnedSpawnProcess
    if os.environ.get('OILWELL_INVERSION_VERIFIER_CHILD')=='1':
        raise RuntimeError('Recursive live verification refused')
    scope=verification_scope(args.include_existing_regression,selected_names)
    config_path=args.config.resolve(strict=True)
    if config_path.stat().st_size>65536: raise ValueError('Verification configuration too large')
    config=json.loads(config_path.read_bytes())
    if set(config)!={'schema','security_baseline_path','security_baseline_sha256'} or config['schema']!='inversion.verification/1':
        raise ValueError('Explicit reviewed isolated verification configuration required')
    baseline_path=Path(config['security_baseline_path']).resolve(strict=True)
    if baseline_path.stat().st_size>8*1024*1024: raise ValueError('Security baseline too large')
    raw=baseline_path.read_bytes()
    if len(raw)>8*1024*1024 or sha256(raw).hexdigest()!=config['security_baseline_sha256']:
        raise ValueError('Reviewed security baseline digest mismatch')
    baseline=Snapshot(json.loads(raw))
    authorized,provider=_environment()
    if baseline.differences(provider.snapshot()): raise RuntimeError('Unapproved security baseline')
    output=args.output.resolve(strict=True)
    if not output.is_dir() or output.is_symlink() or output.is_junction(): raise ValueError('Private local output required')
    # Only ignored reports below this worktree; no tracked output or old report reuse.
    relative=output.relative_to(ROOT)
    if subprocess.run([str(GIT),'check-ignore','-q',str(relative)],cwd=ROOT).returncode:
        raise ValueError('Output must be ignored')
    folder=output/('run-'+uuid4().hex); folder.mkdir()
    protect_directory(folder)
    before=source_inventory()
    revision=subprocess.check_output([str(GIT),'rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    write_fresh(folder/'partial.json',dict(status='incomplete',source_sha256=sha256(encoded(before)).hexdigest(),
        scope=scope,requested_ids=list(selected_names) if selected_names is not None else None,
        security_baseline_sha256=config['security_baseline_sha256']))
    write_fresh(folder/'source.json',before)
    sink=EvidenceSink(folder/'supervisor.jsonl')
    process=None; job=None; parent=None; child=None; failure=None; started=time.monotonic()
    report={}; exitcode=None; residual=None; clean=False; security_ok=False
    try:
        _set_opt_ins(authorized)
        context=multiprocessing.get_context('spawn'); parent,child=context.Pipe()
        job=OwnedJob()
        process=_OwnedSpawnProcess(target=_child,args=(child,str(folder),baseline.values,before,args.include_existing_regression,selected_names))
        process.start(); child.close(); child=None
        job.attach(process)
        sink(dict(phase='owned_child_started',pid=process.pid,service_started=False))
        parent.send('contained')
        while process.is_alive():
            process.join(1)
            if time.monotonic()-started>14400:
                raise TimeoutError('Bounded regression deadline')
        exitcode=process.exitcode; residual=job.active()
        if residual: job.stop()
        clean=not process.is_alive() and job.active()==0 and residual==0
    except BaseException as error:
        failure=type(error).__name__
    finally:
        # Close the containment gate first, including assignment/start failures.
        # Every cleanup phase remains independent so a job error cannot prevent
        # reaping the exact process handle retained by _OwnedSpawnProcess.
        for channel in (parent,child):
            try:
                if channel is not None: channel.close()
            except BaseException as error:
                clean=False; failure=type(error).__name__
        try:
            if job is not None:
                if job.active(): job.stop()
        except BaseException as error:
            clean=False; failure=type(error).__name__
        try:
            if process is not None:
                if getattr(process,'_ownership_uncertain',False):
                    raise RuntimeError('Owned process identity uncertain')
                if process.pid is not None:
                    if process.is_alive(): process.terminate()
                    process.join(3)
                    if process.is_alive(): raise RuntimeError('Owned process reap failed')
                    exitcode=process.exitcode
                process.close()
        except BaseException as error:
            clean=False; failure=type(error).__name__
        finally:
            try:
                if job is not None: job.close()
            except BaseException as error:
                clean=False; failure=type(error).__name__
        # Fresh connections and full per-alias gates after any normal or hard exit.
        try:
            delta=baseline.differences(provider.snapshot()); security_ok=not delta
            sink(dict(phase='external_postmortem',clean=security_ok,differences=[vars(x) for x in sorted(delta)[:64]]))
            clean=clean and resource_state_clean(authorized,provider)
        except BaseException as error:
            security_ok=False; clean=False; failure=type(error).__name__
        child_result=folder/'child-result.json'
        try:
            if not child_result.is_file() or child_result.stat().st_size>16*1024*1024:
                raise ValueError('Missing or oversized child evidence')
            child_raw=child_result.read_bytes()
            if len(child_raw)>16*1024*1024: raise ValueError('Oversized child evidence')
            report=json.loads(child_raw)
            if not isinstance(report,dict): raise ValueError('Invalid child evidence')
        except (OSError,ValueError):
            report={}; clean=False; failure=failure or 'InvalidChildEvidence'
        # Keep both child observations: a later reversion cannot erase drift.
        report.update(parent_source_before=before,parent_source_after=source_inventory(),
            security_ok=security_ok and report.get('security_ok') is True,
            cleanup_ok=clean,interrupted=bool(failure or exitcode!=0 or report.get('interrupted',True)),
            supervisor=dict(exit_code=exitcode,residual_owned_processes=residual,failure_category=failure,
                elapsed_seconds=time.monotonic()-started),
            environment=dict(system_identifier='7688689458460612184',endpoint='127.0.0.1:3822',
                databases=['oilwell_registry_test','oilwell_ingest_reliability_test','oilwell_waveforms_test'],
                python=sys.version,platform=sys.platform),
            revision=revision,source_sha256=sha256(encoded(before)).hexdigest(),
            scope=scope,requested_ids=list(selected_names) if selected_names is not None else None,
            ownership=dict(database_service='Pre-existing task-owned service; controller restores original stopped state',
                test_processes='Contained child and descendants; external parent checks after exit'),
            boundaries=['Synthetic software verification only; no field accuracy or control acceptance',
                'Physical storage, WAL and indexes are outside logical run artifact accounting',
                'Original workspace, frozen demo and port 8765 checks belong to final controller review'])
        report['ok']=qualify(report)
        write_fresh(folder/'report.json',report)
        sink(dict(phase='finished',ok=report['ok'])); sink.close()
    print(json.dumps(dict(report=str(folder/'report.json'),ok=report['ok'],tests_run=report.get('tests_run',0))))
    return 0 if report['ok'] else 1


def protect_directory(path):
    """Protect only the newly created report directory, then verify its ACL."""
    environment=dict(os.environ,INVERSION_PRIVATE_REPORT=str(path))
    script=r'''$ErrorActionPreference='Stop'
$target=$env:INVERSION_PRIVATE_REPORT
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
foreach($sid in @($user.Value,'S-1-5-32-544','S-1-5-18')) {
 $identity=New-Object System.Security.Principal.SecurityIdentifier($sid)
 $rule=New-Object System.Security.AccessControl.FileSystemAccessRule($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
 $acl.AddAccessRule($rule)
}
[System.IO.Directory]::SetAccessControl($target,$acl)
$actual=[System.IO.Directory]::GetAccessControl($target)
$rules=@($actual.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | ForEach-Object {
 @{sid=$_.IdentityReference.Value;rights=[int]$_.FileSystemRights;inheritance=[int]$_.InheritanceFlags;
   propagation=[int]$_.PropagationFlags;access=[int]$_.AccessControlType;inherited=$_.IsInherited}
})
@{protected=$actual.AreAccessRulesProtected;user=$user.Value;rules=$rules} | ConvertTo-Json -Depth 4 -Compress
'''
    result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],env=environment,
        check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    actual=json.loads(result.stdout)
    expected=[dict(sid=sid,rights=2032127,inheritance=3,propagation=0,access=0,inherited=False)
        for sid in (actual['user'],'S-1-5-32-544','S-1-5-18')]
    if (actual['protected'] is not True or
        sorted(actual['rules'],key=lambda rule:rule['sid'])!=sorted(expected,key=lambda rule:rule['sid'])):
        raise RuntimeError('Private ACL verification failed')


def resource_state_clean(authorized,provider):
    # Reader-only checks after fresh identity validation; no fixture reset/repair.
    provider.full_identity_gate('waveform')
    from backend.center.config import _postgres_settings
    from backend.secret_store import read_windows_credential
    import psycopg, tomllib
    path=authorized.configs['inversion']
    config=tomllib.loads(path.read_text(encoding='utf-8'))
    settings=_postgres_settings(config['postgres_reader'],path,os.environ,read_windows_credential)
    with psycopg.connect(**(settings|{'connect_timeout':3})) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='3s'")
        capacity=conn.execute('SELECT queued,running,reserved_bytes FROM inversion.capacity WHERE singleton=1').fetchone()
        reservations=conn.execute("SELECT count(*) FROM inversion.reservations WHERE state<>'settled'").fetchone()[0]
    return capacity==(0,0,0) and reservations==0


if __name__=='__main__':
    raise SystemExit(main())
