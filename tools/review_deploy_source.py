"""Export a checked commit's application bytes into a separate review tree."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import uuid
from zipfile import ZipFile

from .review_deploy_paths import private_acl


EXACT = {'server.py', 'app.js', 'styles.css', 'index.html', 'workbench.html', 'requirements-production.txt',
         'assets/heneng-logo.png', 'assets/heneng-logo-original.png'}
PREFIXES = ('backend/', 'frontend/')
TOOL_NAMES = {'review_deploy.py', 'review_deploy_paths.py', 'review_deploy_config.py',
              'review_deploy_source.py', 'review_deploy_pg.py', 'review_deploy_process.py',
              'review_deploy_backup.py', 'review_deploy_child.py', 'review_deploy_ready.py',
              'review_deploy_worker.py'}
TESTPACK_NAMES = {'__init__.py', '__main__.py', 'contracts.py', 'storage.py', 'config.py',
                 'mechanics.py', 'cycles.py', 'currents.py', 'waveforms.py', 'history.py',
                 'study_inputs.py', 'build.py', 'validate.py', 'lab.py', 'lab_http.py',
                 'modbus_device.py', 'gateway_adapter.py', 'telemetry_run.py', 'waveform_run.py',
                 'inversion_binding.py', 'software_budget.py', 'evidence_report.py', 'evidence_report.html',
                 'render_evidence.cjs', 'study_run.py', 'report.py'}


def _included(name):
    return (name in EXACT or name in {'backend', 'frontend', 'assets', 'tools'} or name.startswith(PREFIXES) or
            name in {'tools/testpack/' + item for item in TESTPACK_NAMES} or
            name in {'config/testpack/smoke.json', 'config/testpack/extended.json'} or
            name in {'tools/' + item for item in TOOL_NAMES})


def _public_files(repo):
    """Read the committed builder's literal whitelist without executing it."""
    source = _git(repo, 'show', 'HEAD:tools/build_open_interface_kit.py')
    tree = ast.parse(source.decode('utf-8'))
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'PUBLIC_FILES' for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError('committed public kit inventory unavailable')
    names = ast.literal_eval(assignments[0].value)
    if (type(names) is not tuple or len(names) != 30 or len(set(names)) != 30 or
            any(type(name) is not str or not name or '/' in name or '\\' in name or name in ('.', '..')
                for name in names)):
        raise ValueError('committed public kit inventory unsafe')
    return names


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
    public_files = _public_files(repo)
    kit_names = {'assets/open-interface/' + name for name in public_files}
    kit_names.add('assets/open-interface/open-interface-r1.zip')
    rows = _git(repo, 'ls-tree', '-rz', 'HEAD')
    files = {}
    for entry in rows.split(b'\x00'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, blob = metadata.split(b' ')
        name = raw_name.decode('utf-8')
        if not (_included(name) or name in kit_names):
            continue
        _safe_name(name)
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError('release tree contains a linked or non-file entry')
        files[name] = blob.decode('ascii')
    if 'server.py' not in files or 'requirements-production.txt' not in files:
        raise ValueError('incomplete committed release')
    if not kit_names.issubset(files):
        raise ValueError('committed public kit incomplete')
    archive = _git(repo, 'cat-file', 'blob', files['assets/open-interface/open-interface-r1.zip'])
    with ZipFile(io.BytesIO(archive)) as zipped:
        if zipped.namelist() != list(public_files):
            raise ValueError('committed public archive inventory mismatch')
        for name in public_files:
            if zipped.read(name) != _git(repo, 'cat-file', 'blob', files['assets/open-interface/' + name]):
                raise ValueError('committed public archive differs from sources')
    return files


def export_committed_source(repo, destination):
    repo, destination = Path(repo).resolve(strict=True), Path(destination).absolute()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('export destination must be new')
    commit = _git(repo, 'rev-parse', '--verify', 'HEAD').decode('ascii').strip()
    status = _git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    for entry in status.split(b'\x00'):
        if not entry:
            continue
        name = entry[3:].decode('utf-8')
        # A rename emits a second path entry, which is conservatively refused.
        if _included(name) or name.startswith('assets/open-interface/') or entry[:2] in (b'R ', b' R', b'RM', b'MR'):
            raise ValueError('release paths must be committed and clean')
    tracked = _tracked(repo)
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


def run_exported_backend(root, action, *, python=None, timeout=180):
    """Run backend work only from the exported code; exchange a private receipt."""
    root = Path(root).absolute()
    code = root / 'code'
    executable = Path(python) if python is not None else root / 'venv' / 'Scripts' / 'python.exe'
    nonce = uuid.uuid4().hex
    receipt = root / 'private' / ('backend-' + nonce + '.json')
    with receipt.open('xb'):
        pass
    private_acl(receipt)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('REVIEW_', 'PLATFORM_')) and key not in ('PYTHONPATH', 'PYTHONHOME', 'PGPASSWORD')}
    environment['PYTHONUTF8'] = '1'
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    command = [str(executable), '-B', '-m', 'tools.review_deploy_child',
               '--root', str(root), '--action', action, '--receipt', str(receipt), '--nonce', nonce]
    log = root / 'private' / 'backend-child.log'
    with log.open('ab') as output:
        result = subprocess.run(command, cwd=code, env=environment, stdout=output,
                                stderr=subprocess.STDOUT, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        payload = json.loads(receipt.read_text(encoding='utf-8'))
        if payload.get('nonce') != nonce or payload.get('action') != action or result.returncode or not payload.get('ok'):
            raise ValueError()
        data = payload['data']
        receipt.unlink()
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('exported backend operation failed; inspect private target artifacts') from None
