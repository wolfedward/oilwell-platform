"""Export a checked commit's application bytes into a separate review tree."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import subprocess


EXACT = {'server.py', 'app.js', 'styles.css', 'index.html', 'requirements-production.txt',
         'assets/heneng-logo.png', 'assets/heneng-logo-original.png'}
PREFIXES = ('backend/', 'frontend/')
TOOL_NAMES = {'review_deploy.py', 'review_deploy_paths.py', 'review_deploy_config.py',
              'review_deploy_source.py', 'review_deploy_pg.py', 'review_deploy_process.py',
              'review_deploy_backup.py'}


def _included(name):
    return (name in EXACT or name in {'backend', 'frontend', 'assets', 'tools'} or name.startswith(PREFIXES) or
            name.startswith('tools/') and name.split('/')[-1] in TOOL_NAMES)


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        raise ValueError('committed source cannot be verified')
    return result.stdout


def _safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('unsafe source path')
    return path


def _tracked(repo):
    rows = _git(repo, 'ls-tree', '-rz', 'HEAD')
    files = {}
    for entry in rows.split(b'\x00'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, blob = metadata.split(b' ')
        name = raw_name.decode('utf-8')
        if not _included(name):
            continue
        _safe_name(name)
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError('release tree contains a linked or non-file entry')
        files[name] = blob.decode('ascii')
    if 'server.py' not in files or 'requirements-production.txt' not in files:
        raise ValueError('incomplete committed release')
    return files


def export_committed_source(repo, destination):
    repo, destination = Path(repo).resolve(strict=True), Path(destination).absolute()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('export destination must be new')
    commit = _git(repo, 'rev-parse', '--verify', 'HEAD').decode('ascii').strip()
    tracked = _tracked(repo)
    status = _git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    for entry in status.split(b'\x00'):
        if not entry:
            continue
        name = entry[3:].decode('utf-8')
        # A rename emits a second path entry, which is conservatively refused.
        if _included(name) or entry[:2] in (b'R ', b' R', b'RM', b'MR'):
            raise ValueError('release paths must be committed and clean')
    destination.mkdir()
    hashes = {}
    for name, blob in sorted(tracked.items()):
        target = destination.joinpath(*_safe_name(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = _git(repo, 'cat-file', 'blob', blob)
        if len(content) > 32 * 1024 * 1024:
            raise ValueError('release blob exceeds 32 MiB')
        with target.open('xb') as stream:
            stream.write(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
        os.chmod(target, 0o444)
    return {'commit': commit, 'files': hashes}


def verify_export(destination, manifest):
    destination = Path(destination).absolute()
    expected = manifest['files']
    for entry in destination.rglob('*'):
        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
            raise ValueError('release tree contains a link')
    found = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
    if found != set(expected):
        raise ValueError('release file inventory changed')
    for name, digest in expected.items():
        path = destination.joinpath(*_safe_name(name).parts)
        if path.is_symlink() or path.stat().st_nlink != 1 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('release bytes changed')
