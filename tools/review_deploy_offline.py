"""Closed, publisher-hashed wheel set for owned Windows CPython installations.

An explicit offline selection never consults an index, inherited pip config or
cache. The complete bundle is admitted before a target is claimed, then copied
from the verified bytes to a new private snapshot. No resume/upgrade in place.
"""
from dataclasses import dataclass
from email.parser import BytesParser
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from zipfile import ZipFile

from .provision_inversion_v2 import WHEEL_NAME, WHEEL_SHA256
from .review_deploy_paths import _check_links, private_acl, verify_target, write_json_private
from .review_deploy_source import verify_export


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    filename: str
    sha256: str
    tags: tuple[str, ...]


# SHA256 values verified against the corresponding version-specific PyPI JSON.
# tzdata is the sole unpinned transitive dependency in the original production
# requirements. Pin its already deployed version explicitly for this closure.
WHEELS = (
    Wheel('paho-mqtt', '2.1.0', 'paho_mqtt-2.1.0-py3-none-any.whl',
          '6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee', ('py3-none-any',)),
    Wheel('pymodbus', '3.15.0', 'pymodbus-3.15.0-py3-none-any.whl',
          '74d8dee961f23845f18a8481253183504dca05c4fcd102890cd28f4d7ce09267', ('py3-none-any',)),
    Wheel('psycopg', '3.3.5', 'psycopg-3.3.5-py3-none-any.whl',
          'ce5aa5cdb4f9379f00f487590e5890bfa7df9a164648c969ffa628505e21af4e', ('py3-none-any',)),
    Wheel('psycopg-binary', '3.3.5', 'psycopg_binary-3.3.5-cp313-cp313-win_amd64.whl',
          'ba466011569297114449df9d523438e1adeedf3e4f31ffb78e897ec3fef3076b', ('cp313-cp313-win_amd64',)),
    Wheel('tzdata', '2026.5', 'tzdata-2026.5-py2.py3-none-any.whl',
          'b683bd1b6659ddcd810ff02ad09ba821d4bf1065072805063eb35c49617905ac', ('py2-none-any', 'py3-none-any')),
    Wheel('python-flint', '0.9.0', WHEEL_NAME, WHEEL_SHA256, ('cp313-cp313-win_amd64',)),
)
TARGET = {'version': '3.13.2', 'implementation': 'CPython', 'system': 'Windows',
          'bits': 64, 'gil': 1, 'platform': 'win-amd64'}
_PROBE = '''import json,platform,struct,sysconfig
print(json.dumps({'version':platform.python_version(),
'implementation':platform.python_implementation(),'system':platform.system(),
'bits':struct.calcsize('P')*8,'gil':int(not sysconfig.get_config_var('Py_GIL_DISABLED')),
'platform':sysconfig.get_platform()}))'''


def _regular(path, limit):
    path = _check_links(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise ValueError('offline artifact must be a bounded unlinked regular file')
    return path.read_bytes()


def _normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def read_bundle(directory):
    directory = _check_links(directory)
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != {w.filename for w in WHEELS}:
        raise ValueError('offline bundle must contain exactly the six approved wheels')
    verified = {}
    for wheel in WHEELS:
        data = _regular(directory / wheel.filename, 32 * 1024 ** 2)
        if sha256(data).hexdigest() != wheel.sha256:
            raise ValueError('offline wheel digest mismatch: ' + wheel.filename)
        with ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(PurePosixPath(n).is_absolute() or
                    '..' in PurePosixPath(n).parts or '\\' in n or ':' in n for n in names):
                raise ValueError('unsafe offline wheel members')
            folder = wheel.filename.split('-')[0] + '-' + wheel.version + '.dist-info'
            metadata = BytesParser().parsebytes(archive.read(folder + '/METADATA'))
            headers = BytesParser().parsebytes(archive.read(folder + '/WHEEL'))
            if (len(metadata.get_all('Name', [])) != 1 or
                    _normalize(metadata['Name']) != wheel.name or
                    metadata.get_all('Version') != [wheel.version] or
                    headers.get_all('Tag') != list(wheel.tags)):
                raise ValueError('offline wheel identity/version/ABI mismatch')
        verified[wheel.filename] = data
    return verified


def check_source(code):
    expected = {'requirements-production.txt': ('paho-mqtt==2.1.0', 'pymodbus==3.15.0', 'psycopg[binary]==3.3.5'),
                'requirements-inversion-v2.txt': ('python-flint==0.9.0 --hash=sha256:' + WHEEL_SHA256,)}
    hashes = {}
    for name, requirements in expected.items():
        # The checkout may use CRLF while the immutable Git export uses LF.
        data = (Path(code) / name).read_bytes()
        lines = tuple(line.strip() for line in data.decode('utf-8').splitlines()
                      if line.strip() and not line.lstrip().startswith('#'))
        if lines != requirements:
            raise ValueError('offline closure differs from source requirements')
        hashes[name] = sha256(data).hexdigest()
    return hashes


def probe_target(python):
    actual = json.loads(subprocess.check_output([str(python), '-I', '-B', '-c', _PROBE],
                        timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    if actual != TARGET:
        raise ValueError('offline dependencies require CPython 3.13.2 Windows x64 GIL')
    return actual


def preflight(directory, code, python):
    read_bundle(directory)
    check_source(code)
    return probe_target(python)


def run_pip(python, args, log):
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('PIP_', 'PYTHON', 'PLATFORM_', 'REVIEW_'))
                   and key.upper() not in ('PGPASSWORD', 'VIRTUAL_ENV')}
    # pip --isolated alone does not disable global/site configuration files.
    environment['PIP_CONFIG_FILE'] = os.devnull
    command = [str(python), '-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check',
               '--no-cache-dir', *map(str, args)]
    with Path(log).open('xb') as output:
        subprocess.run(command, env=environment, stdout=output, stderr=subprocess.STDOUT,
                       check=True, timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _production_lock():
    return ''.join(f'{w.name}{"[binary]" if w.name == "psycopg" else ""}=={w.version} '
                   f'--hash=sha256:{w.sha256}\n' for w in WHEELS if w.name != 'python-flint')


def stage_snapshot(root, directory, python):
    root = Path(root).absolute()
    marker = verify_target(root)
    if marker['state'] != 'claimed':
        raise ValueError('offline installation requires a newly claimed target')
    verify_export(root / 'code', marker['source'])
    snapshot = root / 'private/offline-dependencies'
    if snapshot.exists():
        raise ValueError('offline snapshot already exists; retain failed attempt')
    payloads = read_bundle(directory)
    requirements = check_source(root / 'code')
    probe = probe_target(python)
    snapshot.mkdir(); private_acl(snapshot)
    wheels = snapshot / 'wheels'; wheels.mkdir()
    for name, data in payloads.items():
        # Never reopen the external source after hashing it.
        with (wheels / name).open('xb') as output:
            output.write(data)
    lock = _production_lock().encode('ascii')
    with (snapshot / 'production.lock').open('xb') as output:
        output.write(lock)
    write_json_private(snapshot / 'admitted.json', {
        'schema': 'owp.offline-dependencies/1', 'instance_id': marker['instance_id'],
        'source_commit': marker['source']['commit'], 'target': probe,
        'requirements_sha256': requirements, 'wheels': {w.filename: w.sha256 for w in WHEELS},
        'lock_sha256': sha256(lock).hexdigest(), 'network_fallback': False})
    verify_snapshot(root, snapshot)
    return snapshot


def verify_snapshot(root, snapshot):
    root = Path(root).absolute(); snapshot = _check_links(snapshot)
    if snapshot != root / 'private/offline-dependencies':
        raise ValueError('offline snapshot is not owned by this installation')
    marker = verify_target(root)
    verify_export(root / 'code', marker['source'])
    if marker['state'] != 'claimed':
        raise ValueError('offline install cannot resume a completed target')
    expected = {'schema': 'owp.offline-dependencies/1', 'instance_id': marker['instance_id'],
        'source_commit': marker['source']['commit'], 'target': TARGET,
        'requirements_sha256': check_source(root / 'code'),
        'wheels': {w.filename: w.sha256 for w in WHEELS},
        'lock_sha256': sha256(_production_lock().encode('ascii')).hexdigest(), 'network_fallback': False}
    admitted = json.loads(_regular(snapshot / 'admitted.json', 16384))
    if admitted != expected or _regular(snapshot / 'production.lock', 8192) != _production_lock().encode('ascii'):
        raise ValueError('offline snapshot ownership, source or lock changed')
    read_bundle(snapshot / 'wheels')
    return admitted


def install_production(root, snapshot):
    root = Path(root); snapshot = Path(snapshot)
    verify_snapshot(root, snapshot)
    python = root / 'venv/Scripts/python.exe'
    run_pip(python, ['install', '--no-input', '--no-index', '--find-links', snapshot / 'wheels',
                    '--no-deps', '--only-binary=:all:', '--require-hashes', '--no-compile',
                    '-r', snapshot / 'production.lock'], snapshot / 'production-install.log')
    run_pip(python, ['check'], snapshot / 'production-check.log')


def finish_install(root, snapshot, *, inversion_v2):
    root = Path(root); snapshot = Path(snapshot)
    admitted = verify_snapshot(root, snapshot)
    python = root / 'venv/Scripts/python.exe'
    probe_target(python)
    # pip is the bootstrap tool bundled with the approved base interpreter,
    # not an application dependency. Everything else must be the closed set.
    script = """import importlib.metadata as m,json,re
print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version
for d in m.distributions() if d.metadata['Name'].lower()!='pip'}))"""
    packages = json.loads(subprocess.check_output([str(python), '-I', '-B', '-c', script],
                          timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    expected = {w.name: w.version for w in WHEELS if inversion_v2 or w.name != 'python-flint'}
    if packages != expected:
        raise ValueError('installed dependency versions differ from offline closure')
    write_json_private(snapshot / 'installed.json', {**admitted, 'packages': packages,
                       'status': 'dependencies_installed'})
