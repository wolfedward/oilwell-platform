"""Isolated real-process WP-03 evidence runner, never a deployment command."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FROZEN = Path('C:/Users/hp/Documents/外专双百-演示版-20260912')
FROZEN_ZIP = Path('C:/Users/hp/Documents/外专双百-演示版-20260912-source.zip')
ORIGINAL = Path('C:/Users/hp/Documents/外专双百')
ZIP_SHA256 = 'bb6bda07c853fee576ceed73dc6e60046f0acc753d116071722b3d617d95ca6a'
TAG_COMMIT = 'd392c831e45e9273790d1c250950853966f5b709'


def qualify(soak, counts, protected_unchanged):
    reasons = []
    if soak.get('duration_seconds', 0) < 1800:
        reasons.append('soak.shorter_than_1800_seconds')
    if soak.get('messages', 0) <= 0 or soak.get('bytes', 0) <= 0:
        reasons.append('soak.no_data')
    if soak.get('stored') != soak.get('messages') or soak.get('pending') != 0:
        reasons.append('soak.incomplete_recovery')
    if soak.get('duplicates_in_database') != 0 or soak.get('exact_payloads') is not True:
        reasons.append('soak.data_integrity')
    if soak.get('restarts', 0) < 10:
        reasons.append('soak.insufficient_restarts')
    if soak.get('capacity_exhausted') is not False:
        reasons.append('soak.capacity_unverified')
    if counts.get('testsRun', 0) < 14 or any(counts.get(key) != 0 for key in ('failures', 'errors', 'skipped')):
        reasons.append('tests.incomplete_or_failed')
    if not protected_unchanged:
        reasons.append('protected_artifacts.changed')
    return reasons


def git(*args, cwd=ROOT):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding='utf-8', timeout=10).stdout.strip()


def protected_snapshot():
    archive_hash = sha256(FROZEN_ZIP.read_bytes()).hexdigest()
    if archive_hash != ZIP_SHA256:
        raise AssertionError('Frozen source archive no longer matches recorded baseline')
    names = []
    with zipfile.ZipFile(FROZEN_ZIP) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if name.is_absolute() or '..' in name.parts:
                raise AssertionError('Unsafe frozen archive entry')
            # Other plans scratch is not ours to inspect, even in the old archive.
            if not member.is_dir() and name.parts[0] != '.superpowers':
                names.append(str(name))
        files = {name: sha256((FROZEN / name).read_bytes()).hexdigest() for name in sorted(names)}
        archive_files = {name: sha256(archive.read(name)).hexdigest() for name in sorted(names)}
    tag = git('rev-parse', 'demo-v2.0-20260912^{}')
    if tag != TAG_COMMIT:
        raise AssertionError('Frozen tag moved')
    return {'zip_sha256': archive_hash, 'tag_commit': tag, 'frozen_source_files': files,
            'source_matches_archive': files == archive_files,
            'original_git_status': git('status', '--porcelain=v1', cwd=ORIGINAL)}


class CaseResults(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({'test': test.id(), 'outcome': 'passed'})

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.cases.append({'test': test.id(), 'outcome': 'failed'})

    def addError(self, test, error):
        super().addError(test, error)
        self.cases.append({'test': test.id(), 'outcome': 'error'})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases.append({'test': test.id(), 'outcome': 'skipped', 'reason': reason})


def soak(seconds):
    from tests.ingest_support import IngestDatabaseCase
    from tests.ingest_stack import Stack
    fixture = IngestDatabaseCase()
    expected = {}
    recovery = []
    peak_backlog = 0
    faults = 0
    fixture.setUp()
    try:
        with Stack(fixture) as stack:
            stack.start()
            start = time.monotonic()
            deadline = start + seconds
            next_message = start
            next_fault = start
            interval = max(.1, seconds / 12)
            next_progress = start
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_message:
                    identifier = 'SOAK-' + str(len(expected) + 1)
                    if faults < 12 and now >= next_fault:
                        stack.stop_process('publisher')
                        observation = stack.enqueue_one(stack.observation(identifier, value=35 + len(expected) % 23))
                        expected[identifier] = observation.payload
                        stack.await_gateway_state('awaiting_receipt', identifier)
                        peak_backlog = max(peak_backlog, 1)
                        began = time.monotonic()
                        target = ('gateway', 'center', 'broker')[faults % 3]
                        getattr(stack, 'restart_' + target)()
                        stack.enable_receipt_delivery()
                        stack.await_gateway_state('stored', identifier)
                        recovery.append({'process': target, 'seconds': time.monotonic() - began})
                        faults += 1
                        next_fault = start + faults * interval
                    else:
                        observation = stack.enqueue_one(stack.observation(identifier, value=35 + len(expected) % 23))
                        expected[identifier] = observation.payload
                    next_message = max(next_message + 2, time.monotonic())
                with sqlite_queue(stack.spool) as conn:
                    pending = conn.execute("SELECT count(*) FROM uplink_queue_v2 WHERE state NOT IN ('stored','quarantined')").fetchone()[0]
                peak_backlog = max(peak_backlog, pending)
                if now >= next_progress:
                    print(json.dumps({'event': 'soak_progress', 'elapsed_seconds': round(now - start, 1),
                                      'messages': len(expected), 'pending': pending, 'restarts': len(stack.restarts)}), flush=True)
                    next_progress = now + 30
                time.sleep(.1)
            stack.await_condition(lambda: all((stack.gateway_record(key) or {}).get('state') == 'stored' for key in expected),
                                  'all soak messages acknowledged')
            duration = time.monotonic() - start
            counts = stack.database_counts()
            with stack.connection('migrator') as conn:
                actual = dict(conn.execute('SELECT message_id,raw_payload FROM ingest_reliability.facts'))
                duplicate_rows = conn.execute('SELECT count(*)-count(DISTINCT message_id) FROM public.ingest_messages').fetchone()[0]
                samples = conn.execute('SELECT count(*) FROM public.telemetry_samples').fetchone()[0]
            exact = set(actual) == set(expected) and all(bytes(actual[key]) == payload for key, payload in expected.items())
            exact = exact and all(stack.gateway_record(key)['payload'] == value for key, value in expected.items())
            if samples != len(expected) or counts['receipts'] != len(expected):
                exact = False
            with sqlite_queue(stack.spool) as conn:
                pending = conn.execute("SELECT count(*) FROM uplink_queue_v2 WHERE state<>'stored'").fetchone()[0]
                stored = conn.execute("SELECT count(*) FROM uplink_queue_v2 WHERE state='stored'").fetchone()[0]
            byte_count = sum(map(len, expected.values()))
            return {'duration_seconds': duration, 'messages': len(expected), 'bytes': byte_count, 'stored': stored,
                    'messages_per_second': len(expected) / duration, 'bytes_per_second': byte_count / duration,
                    'peak_backlog': peak_backlog, 'restarts': len(stack.restarts), 'restart_events': stack.restarts,
                    'failure_count': faults, 'recoveries': recovery, 'duplicates_in_database': duplicate_rows,
                    'duplicate_observations': int(stack.duplicate_observations()), 'pending': pending,
                    'exact_payloads': exact, 'capacity_exhausted': False,
                    'scope': 'local software experiment; no physical device or field throughput claim'}
    finally:
        fixture.doCleanups()


def sqlite_queue(path):
    import sqlite3
    from contextlib import closing
    return closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--soak-seconds', type=int, default=1800)
    parser.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.soak_seconds <= 86400:
        parser.error('soak duration must be 1..86400; runs shorter than 1800 never qualify')
    report_path = Path(args.report).resolve()
    if not report_path.is_relative_to(ROOT / 'logs') or report_path.exists():
        parser.error('report must be a new file under this worktree logs directory')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ['OILWELL_INGEST_TEST_CONFIG'] = str(Path(args.config).resolve())
    os.environ['OILWELL_INGEST_EVIDENCE_DIR'] = str(report_path.parent / (report_path.stem + '-evidence'))
    from tests.ingest_support import TEST_DATABASE
    result_report = {'code_revision': git('rev-parse', 'HEAD'), 'worktree_status': git('status', '--porcelain=v1'),
                     'database_identity': {'host': '127.0.0.1', 'port': 3822, 'database': TEST_DATABASE},
                     'deployed': False, 'status': 'failed'}
    before = protected_snapshot()
    result_report['protected_artifacts'] = {'before': before}
    try:
        with tempfile.TemporaryDirectory(prefix='wp03-verify-') as temporary:
            os.environ.update(NO_BROWSER='1', PLATFORM_DATABASE_PATH=str(Path(temporary) / 'platform.db'),
                              PLATFORM_LOG_PATH=str(Path(temporary) / 'logs'), PYTHONUTF8='1')
            suite = unittest.defaultTestLoader.loadTestsFromNames(['tests.test_ingest_end_to_end', 'tests.test_ingest_failure_recovery'])
            with report_path.with_suffix('.tests.log').open('x', encoding='utf-8') as stream:
                result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CaseResults).run(suite)
            counts = {'testsRun': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped)}
            result_report.update(test_counts=counts, failure_cases=result.cases)
            print(json.dumps({'event': 'fault_tests_complete', **counts}), flush=True)
            if not result.wasSuccessful() or result.skipped:
                raise AssertionError('Fault verification failed or skipped; no passing soak report')
            measurement = soak(args.soak_seconds)
            after = protected_snapshot()
            result_report['protected_artifacts']['after'] = after
            unchanged = before == after
            result_report['protected_artifacts']['unchanged'] = unchanged
            reasons = qualify(measurement, counts, unchanged)
            result_report.update(soak=measurement, soak_duration=measurement['duration_seconds'],
                                 qualification_failures=reasons, status='passed' if not reasons else 'failed')
    except Exception as error:
        result_report['error_type'] = type(error).__name__
        # Test details are in the safe scoped test log; never serialize secrets/DSNs from exceptions.
        print(json.dumps({'event': 'verification_failed', 'error_type': type(error).__name__}), flush=True)
    with report_path.open('x', encoding='utf-8') as stream:
        json.dump(result_report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'event': 'report_written', 'status': result_report['status'], 'report': str(report_path)}, ensure_ascii=False), flush=True)
    return 0 if result_report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
