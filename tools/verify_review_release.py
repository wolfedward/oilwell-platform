"""Explicit loopback SOFTWARE workflow verification; writes labelled study records.

Run only against an isolated review installation. Credentials are read privately,
never included in reports. This is not a field acceptance or load test.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


PREFIX = '/api/production-studies'
KINDS = ('condition_training', 'condition_prediction', 'recovery_fit', 'energy_schedule',
         'energy_effects', 'motor_spectrum', 'protection_replay')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ReviewClient:
    def __init__(self, base_url, accounts):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError('Only an explicit loopback review URL is allowed')
        self.port = parsed.port
        self.accounts = {row['username']: row for row in accounts}
        self.cookies = {}

    def request(self, method, path, body=None, actor='administrator', expected=200, raw=False):
        if not path.startswith('/api/'):
            raise ValueError('Only platform API paths are allowed')
        if actor is not None and actor not in self.cookies:
            row = self.accounts[actor]
            status, _, headers = self._send('POST', '/api/auth/login',
                                           {'username': actor, 'password': row['password']}, None)
            if status != 200 or 'Set-Cookie' not in headers:
                raise RuntimeError('Review account login failed')
            self.cookies[actor] = headers['Set-Cookie'].split(';')[0]
        status, payload, _ = self._send(method, path, body, self.cookies.get(actor))
        if status != expected:
            code = payload.get('error', {}).get('code', 'unavailable') if isinstance(payload, dict) else 'non_json'
            raise RuntimeError(f'{method} {path}: expected {expected}, received {status} ({code})')
        if expected >= 400 or raw:
            return payload
        return payload['data']

    def _send(self, method, path, body, cookie):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}'}
        if cookie:
            headers['Cookie'] = cookie
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
        try:
            connection.request(method, path, encoded, headers)
            response = connection.getresponse()
            if response.getheader('Content-Length') and int(response.getheader('Content-Length')) > 16 * 1024 * 1024:
                raise RuntimeError('Unexpected oversized review response')
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise RuntimeError('Unexpected oversized review response')
            return response.status, json.loads(raw), dict(response.getheaders())
        finally:
            connection.close()

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, body, **kwargs):
        return self.request('POST', path, body, **kwargs)


def verify_workflows(client, vectors):
    """Explicit caller-owned verification; no device writes or demo seeding."""
    if type(vectors) is not dict or set(vectors) != set(KINDS):
        raise ValueError('Exactly seven explicit software study vectors required')
    health = client.get('/api/health', actor=None)
    if health.get('runtime_mode') != 'review' or health.get('control_enabled') is not False:
        raise RuntimeError('Refusing writes outside non-controlling review runtime')
    checks, records = ['review_runtime_no_control'], {}
    def check(name, condition):
        if not condition:
            raise RuntimeError('Workflow verification failed: ' + name)
        checks.append(name)

    def body(kind, payload=None):
        return {'request_id': str(uuid4()), 'kind': kind,
                'title': '软件链路验证 / ' + kind,
                'source': {'kind': 'software_fixture', 'description': 'Explicit isolated software workflow verification; not field data',
                           'references': ['integrated-review-release/1']},
                'payload': copy.deepcopy(vectors[kind] if payload is None else payload)}

    def create(kind, payload=None):
        submitted = body(kind, payload)
        record = client.post(PREFIX, submitted, expected=201)
        replay = client.post(PREFIX, submitted, expected=201)
        check(kind + '_idempotent', replay == record)
        receipt = client.get(PREFIX + '/requests/' + submitted['request_id'])
        check(kind + '_receipt', receipt == record)
        records[kind] = record
        return record, submitted

    def review(record, decision='approved', expected=200, actor='reviewer', version=None):
        current = client.get(PREFIX + '/' + record['id'])
        return client.post(PREFIX + '/' + record['id'] + '/reviews', {
            'request_id': str(uuid4()), 'expected_version': current['version'] if version is None else version,
            'decision': decision, 'comment': '软件链路复核；非现场批准或设备执行许可'},
            actor=actor, expected=expected)

    def select(record):
        current = client.get(PREFIX + '/' + record['id'])
        return client.post(PREFIX + '/selections/' + record['kind'], {
            'request_id': str(uuid4()), 'expected_version': current['selection_version'],
            'record_id': record['id'], 'comment': '软件影子选用；无设备下发'})

    training, original = create('condition_training')
    check('training_actual_engine', training['status'] == 'completed')
    review(training, actor='administrator', expected=403)
    checks.append('self_review_refused')
    approved = review(training)
    check('review_version_advanced', approved['version'] == 2)
    review(training, version=1, expected=409)
    checks.append('stale_review_refused')
    select(training)
    changed = copy.deepcopy(original)
    changed['title'] += ' changed'
    client.post(PREFIX, changed, expected=409)
    checks.append('same_request_changed_body_refused')
    prediction_payload = copy.deepcopy(vectors['condition_prediction'])
    prediction_payload['model_record_id'] = training['id']
    prediction, _ = create('condition_prediction', prediction_payload)
    check('prediction_pinned_model', prediction['dependencies'][0]['record_id'] == training['id'])
    promoted = body('condition_prediction', prediction_payload)
    promoted['source']['kind'] = 'submitted_observation'
    client.post(PREFIX, promoted, expected=422)
    checks.append('software_source_promotion_refused')

    recovery, _ = create('recovery_fit')
    check('recovery_actual_fit', recovery['status'] == 'fitted')
    review(recovery)
    schedule_payload = copy.deepcopy(vectors['energy_schedule'])
    for well in schedule_payload['wells']:
        well['recovery_record_id'] = recovery['id']
    schedule, _ = create('energy_schedule', schedule_payload)
    check('schedule_actual_feasible', schedule['status'] == 'feasible')
    review(schedule)
    select(schedule)
    effects, _ = create('energy_effects')
    check('effects_not_causal', effects['status'] == 'descriptive')
    review(effects)
    spectrum, _ = create('motor_spectrum')
    check('spectrum_actual_engine', spectrum['status'] == 'completed')
    protection, _ = create('protection_replay')
    check('protection_actual_engine', protection['status'] == 'completed')

    for kind, record in records.items():
        exported = client.get(PREFIX + '/' + record['id'] + '/export', raw=True)
        check(kind + '_export_original_result', _hash(exported['result']) == record['result_sha256'])
        check(kind + '_source_and_boundary', exported['source']['kind'] == 'software_fixture'
              and exported['control_eligible'] is False and exported['field_validation'] is False)

    malformed = body('energy_effects')
    malformed['payload']['baseline']['intervals'][0]['quality'] = []
    client.post(PREFIX, malformed, expected=422)
    client.get(PREFIX + '/requests/' + malformed['request_id'], expected=404)
    checks.append('rejected_calculation_no_receipt')
    impossible = copy.deepcopy(schedule_payload)
    impossible['site_power_limit_kw'] = 1e-9
    rejected = client.post(PREFIX, body('energy_schedule', impossible), expected=201)
    check('no_feasible_is_explicit', rejected['status'] == 'no_feasible_candidate')
    review(rejected, expected=409)
    checks.append('no_feasible_cannot_be_approved')
    review(recovery, 'revoked')
    stale = client.get(PREFIX + '/' + schedule['id'])
    check('recovery_revocation_restricts_schedule', not stale['dependency_current'] and not stale['eligible'] and not stale['is_selected'])
    review(training, 'revoked')
    stale = client.get(PREFIX + '/' + prediction['id'])
    check('model_revocation_restricts_prediction', not stale['dependency_current'])
    client.post(PREFIX, body('condition_prediction', prediction_payload), expected=409)
    checks.append('revoked_model_cannot_predict')
    client.get(PREFIX, actor=None, expected=401)
    client.post('/api/commands', {}, expected=404)
    checks.extend(['anonymous_read_refused', 'legacy_control_route_absent'])
    listing = client.get(PREFIX + '?limit=100')
    check('list_is_metadata_only', all('result' not in row and 'payload' not in row for row in listing))
    return {'status': 'passed', 'verified_at': datetime.now(timezone.utc).isoformat(),
            'checks': checks, 'records': records, 'negative_record_id': rejected['id'],
            'vector_sha256': _hash(vectors), 'control_enabled': False, 'field_validation': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--accounts', type=Path, required=True, help='Private JSON with accounts array')
    parser.add_argument('--vectors', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--allow-software-fixtures', action='store_true', help='Explicitly create labelled software studies')
    args = parser.parse_args()
    if not args.allow_software_fixtures or args.report.exists():
        parser.error('Explicit software fixture permission and a new report path are required')
    accounts = json.loads(args.accounts.read_text(encoding='utf-8'))['accounts']
    vectors = json.loads(args.vectors.read_text(encoding='utf-8'))
    report = verify_workflows(ReviewClient(args.base_url, accounts), vectors)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'status': report['status'], 'checks': len(report['checks']), 'report': str(args.report)}))


if __name__ == '__main__':
    main()
