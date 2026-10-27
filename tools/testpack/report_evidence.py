"""Lossless private report storage; limits apply before publication, not afterwards.

Like storage.py, these local files assume no hostile concurrent owner replaces
directory entries. A partial write is retained and cannot be exported/resumed.
"""
from hashlib import sha256
from pathlib import Path
import re

from .storage import canonical_bytes, safe_path, strict_json

MAX_FILE_BYTES=32*1024*1024
MAX_TOTAL_BYTES=256*1024*1024
MAX_CHECKS=10000
INCOMPLETE='.report-incomplete'
PARTS='report-evidence'


def write_private(root, public, evidence):
    root=safe_path(Path(root))
    for name in ('report.json',PARTS,INCOMPLETE):
        if (root/name).exists():raise ValueError('report artifacts already exist; preserve history')
    checks=evidence['checks']
    if type(checks) is not list or len(checks)>MAX_CHECKS:raise ValueError('private check count')
    pieces=[];total=0
    for check in checks:
        raw=canonical_bytes(check)
        total+=len(raw)
        if len(raw)>MAX_FILE_BYTES or total>MAX_TOTAL_BYTES:raise ValueError('private evidence byte budget')
        pieces.append(raw)
    record={'schema':'owp.testpack.private-report/1','public':public,'evidence':evidence}
    raw=canonical_bytes(record) if total<=MAX_FILE_BYTES else None
    sharded=raw is None or len(raw)>MAX_FILE_BYTES
    if sharded:
        refs=[{'path':f'{PARTS}/check-{i:05d}.json','bytes':len(part),'sha256':sha256(part).hexdigest()}
              for i,part in enumerate(pieces)]
        record={'schema':'owp.testpack.private-report/2','public':public,
                'evidence':evidence|{'checks':refs}}
        raw=canonical_bytes(record)
    if len(raw)>MAX_FILE_BYTES or len(raw)+(total if sharded else 0)>MAX_TOTAL_BYTES:
        raise ValueError('private report aggregate byte budget')
    # All budgets are checked before the first durable write. On failure the
    # marker and partial bytes remain, making a failed attempt distinguishable.
    marker=root/INCOMPLETE
    with marker.open('xb') as stream:stream.write(b'private report write in progress\n')
    if sharded:
        (root/PARTS).mkdir()
        for ref,part in zip(refs,pieces):
            with (root/ref['path']).open('xb') as stream:stream.write(part)
    path=root/'report.json'
    with path.open('xb') as stream:stream.write(raw)
    marker.unlink()
    return path


def _read(path):
    path=safe_path(path)
    if not path.is_file() or path.stat().st_size>MAX_FILE_BYTES:raise ValueError('private report too large or not regular')
    with path.open('rb') as stream:raw=stream.read(MAX_FILE_BYTES+1)
    if len(raw)>MAX_FILE_BYTES:raise ValueError('private report too large')
    return raw


def read_private(root):
    root=safe_path(Path(root))
    if (root/INCOMPLETE).exists():raise ValueError('private report write incomplete')
    record=strict_json(_read(root/'report.json'))
    if type(record) is not dict or set(record)!={'schema','public','evidence'} or record['schema'] not in (
            'owp.testpack.private-report/1','owp.testpack.private-report/2'):
        raise ValueError('private report fields')
    return record


def iter_checks(root,record):
    root=safe_path(Path(root));checks=record['evidence']['checks']
    if type(checks) is not list or len(checks)>MAX_CHECKS:raise ValueError('private check count')
    if record['schema']=='owp.testpack.private-report/1':
        yield from checks
        return
    directory=safe_path(root/PARTS)
    if not directory.is_dir():raise ValueError('private evidence directory')
    expected=set();total=(root/'report.json').stat().st_size
    for i,ref in enumerate(checks):
        name=f'check-{i:05d}.json';expected.add(name)
        if type(ref) is not dict or set(ref)!={'path','bytes','sha256'} or ref['path']!=f'{PARTS}/{name}':
            raise ValueError('private evidence reference')
        if type(ref['bytes']) is not int or not 0<ref['bytes']<=MAX_FILE_BYTES or not isinstance(ref['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',ref['sha256']):
            raise ValueError('private evidence bounds/digest')
        total+=ref['bytes']
        if total>MAX_TOTAL_BYTES:raise ValueError('private report aggregate byte budget')
    if {p.name for p in directory.iterdir()}!=expected:raise ValueError('private evidence inventory mismatch')
    for ref in checks:
        raw=_read(root/ref['path'])
        if len(raw)!=ref['bytes'] or sha256(raw).hexdigest()!=ref['sha256']:raise ValueError('private evidence shard changed')
        yield strict_json(raw)


def evidence_digest(checks,evidence):
    """Hash the original canonical object, with only one check encoded at a time."""
    digest=sha256();digest.update(b'{"checks":[')
    for index,check in enumerate(checks):
        if index:digest.update(b',')
        digest.update(canonical_bytes(check)[:-1])
    digest.update(b']')
    if 'inversion_outcomes' in evidence:
        digest.update(b',"inversion_outcomes":');digest.update(canonical_bytes(evidence['inversion_outcomes'])[:-1])
    digest.update(b',"run_metadata":');digest.update(canonical_bytes(evidence['run_metadata'])[:-1])
    digest.update(b'}\n')
    return digest.hexdigest()
