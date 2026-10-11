"""Filesystem ownership gates for an isolated local review installation."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import uuid


def _check_links(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise ValueError('linked path refused')
    return path


def safe_target(path, *, forbidden):
    if not Path(path).is_absolute():
        raise ValueError('target must be an absolute directory')
    path = _check_links(path)
    if path == Path(path.anchor) or path.exists():
        raise ValueError('target must be a new absolute directory')
    for item in forbidden:
        protected = _check_links(item)
        if path == protected or path in protected.parents:
            raise ValueError('target encompasses protected directory')
    if not path.parent.is_dir():
        raise ValueError('target parent must already exist')
    return path


def _owner_sid():
    output = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'],
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).decode('ascii', errors='replace')
    row = next(csv.reader(io.StringIO(output)))
    if len(row) != 2 or not row[1].startswith('S-1-'):
        raise RuntimeError('cannot establish owner SID')
    return row[1]


def private_acl(path):
    if os.name != 'nt':
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
        return
    sid = _owner_sid()
    # icacls applies switches in an order that can discard a grant when both
    # are supplied together. Grant first, then strip inherited entries.
    access = f'*{sid}:(OI)(CI)F' if Path(path).is_dir() else f'*{sid}:F'
    for option in (['/grant:r', access], ['/inheritance:r']):
        result = subprocess.run(['icacls', str(path), *option], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('cannot apply private owner ACL')


def write_json_private(path, value):
    path = _check_links(path)
    if path.exists():
        raise ValueError('private file already exists')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
    private_acl(path)


def claim_target(path, *, forbidden):
    path = safe_target(path, forbidden=forbidden)
    path.mkdir()
    private_acl(path)
    private = path / 'private'
    private.mkdir()
    private_acl(private)
    return {'format': 1, 'instance_id': str(uuid.uuid4()), 'root': str(path), 'state': 'claimed'}


def verify_target(path, *, expected_ports=None):
    path = _check_links(path)
    marker_file = path / 'private' / 'marker.json'
    _check_links(marker_file)
    if not path.is_dir() or not marker_file.is_file() or marker_file.stat().st_nlink != 1:
        raise ValueError('unowned or incomplete target')
    marker = json.loads(marker_file.read_text(encoding='utf-8'))
    try:
        uuid.UUID(marker['instance_id'])
        if marker['format'] != 1 or marker['root'] != str(path) or marker['state'] not in {'ready', 'running', 'stopped', 'claimed', 'starting', 'start_failed'}:
            raise ValueError()
        if expected_ports is not None and marker['ports'] != expected_ports:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('target marker mismatch') from error
    return marker


def replace_json_private(path, value):
    path = _check_links(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('private file missing or linked')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    write_json_private(temporary, value)
    os.replace(temporary, path)
