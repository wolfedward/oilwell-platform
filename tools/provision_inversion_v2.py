"""Install the pinned numerical runtime into a new, owned directory only.

No database connections, service changes, deletion, upgrade-in-place or global
package installation. An interrupted installation is retained for inspection.
"""
import argparse
import base64
import csv
from email.parser import BytesParser
from hashlib import sha256
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

from tools.review_deploy_paths import safe_target, private_acl, write_json_private

WHEEL_NAME = 'python_flint-0.9.0-cp313-cp313-win_amd64.whl'
WHEEL_SHA256 = '1f3a3672d589e376d47288dcebcb712997dc719880a4dbf1779bf0f2b20d0293'
DIST_INFO = 'python_flint-0.9.0.dist-info'
REPO = Path(__file__).resolve().parents[1]
_PROBE = '''import json,platform,struct,sysconfig
print(json.dumps({'version':platform.python_version(),
'implementation':platform.python_implementation(),'system':platform.system(),
'bits':struct.calcsize('P')*8,'gil':int(not sysconfig.get_config_var('Py_GIL_DISABLED'))}))'''


def probe_python(path):
    output = subprocess.check_output([str(path), '-I', '-B', '-c', _PROBE],
                                    timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return json.loads(output)


def inspect_wheel(path):
    if path.name != WHEEL_NAME or sha256(path.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise ValueError('Unapproved wheel digest')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(PurePosixPath(n).is_absolute() or
                '..' in PurePosixPath(n).parts or '\\' in n for n in names):
            raise ValueError('Unsafe wheel paths')
        record_name = DIST_INFO + '/RECORD'
        recorded = {}
        for name, signature, size in csv.reader(io.StringIO(archive.read(record_name).decode('utf-8'))):
            if name in recorded:
                raise ValueError('Duplicate wheel RECORD')
            data = archive.read(name)
            if name != record_name:
                actual = base64.urlsafe_b64encode(sha256(data).digest()).rstrip(b'=').decode('ascii')
                if signature != 'sha256=' + actual or str(len(data)) != size:
                    raise ValueError('Wheel RECORD integrity failure')
            elif signature or size:
                raise ValueError('Invalid self RECORD')
            recorded[name] = sha256(data).hexdigest()
        file_names = {item.filename for item in archive.infolist() if not item.is_dir()}
        if any(item.is_dir() and item.file_size for item in archive.infolist()):
            raise ValueError('Nonempty wheel directory entry')
        if file_names != set(recorded):
            raise ValueError('Unrecorded wheel files')
        wheel = BytesParser().parsebytes(archive.read(DIST_INFO + '/WHEEL'))
        metadata = BytesParser().parsebytes(archive.read(DIST_INFO + '/METADATA'))
        if wheel.get_all('Tag') != ['cp313-cp313-win_amd64'] or metadata['Version'] != '0.9.0':
            raise ValueError('Unapproved wheel ABI/version')
        native = {n: h for n, h in recorded.items() if n.lower().endswith(('.dll', '.pyd'))}
        licenses = {n: h for n, h in recorded.items() if n.startswith(DIST_INFO + '/licenses/')}
        if not native or not metadata['License-Expression'] or not licenses:
            raise ValueError('Incomplete native/license inventory')
        for component in ('flint-3.6.0', 'gmp-6.3.0', 'mpfr-4.2.2'):
            if not any('/' + component + '/' in n for n in licenses):
                raise ValueError('Missing native license: ' + component)
        # Record every file, not merely the executable extension modules.
        return {'wheel': WHEEL_NAME, 'wheel_sha256': WHEEL_SHA256,
                'abi': 'cp313-cp313-win_amd64', 'files': recorded,
                'native': native, 'licenses': licenses,
                'license_expression': metadata['License-Expression']}


def _run(argv, log):
    with log.open('xb') as stream:
        subprocess.run([str(arg) for arg in argv], stdout=stream, stderr=subprocess.STDOUT,
                       check=True, timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def verify_installed(venv, inventory):
    site = venv / 'Lib' / 'site-packages'
    for name, expected in inventory['files'].items():
        # pip rewrites RECORD, so validate all immutable wheel members separately.
        if name == DIST_INFO + '/RECORD':
            continue
        installed = site.joinpath(*PurePosixPath(name).parts)
        if not installed.is_file() or installed.is_symlink() or sha256(installed.read_bytes()).hexdigest() != expected:
            raise ValueError('Installed wheel differs: ' + name)


def provision(root: Path, *, base_python: Path) -> Path:
    root = safe_target(root, forbidden=(REPO,))
    probe = probe_python(base_python)
    if probe != {'version': '3.13.2', 'implementation': 'CPython', 'system': 'Windows', 'bits': 64, 'gil': 1}:
        raise ValueError('CPython 3.13.2 Windows x64 GIL runtime required')
    root.mkdir()  # exclusive claim; all subsequent writes remain under this root
    private_acl(root)
    write_json_private(root / 'owner.json', {'schema': 'inversion.owned-runtime/1',
                       'root': str(root), 'base_python': str(base_python), 'probe': probe})
    venv = root / 'venv'
    _run([base_python, '-I', '-B', '-m', 'venv', venv], root / '01-venv.log')
    python = venv / 'Scripts' / 'python.exe'
    wheelhouse = root / 'wheels'
    wheelhouse.mkdir()
    common = [python, '-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check']
    _run([*common, 'download', '--index-url', 'https://pypi.org/simple', '--only-binary=:all:',
          '--require-hashes', '--no-deps', '-r', REPO / 'requirements-inversion-v2.txt',
          '--dest', wheelhouse], root / '02-download.log')
    inventory = inspect_wheel(wheelhouse / WHEEL_NAME)
    write_json_private(root / 'wheel-inventory.json', inventory)
    _run([*common, 'install', '--no-index', '--find-links', wheelhouse, '--no-deps',
          '--only-binary=:all:', '--require-hashes', '-r', REPO / 'requirements-inversion-v2.txt'],
         root / '03-native-install.log')
    verify_installed(venv, inventory)
    _run([*common, 'install', '--index-url', 'https://pypi.org/simple', '--only-binary=:all:',
          '-r', REPO / 'requirements-production.txt'], root / '04-production-install.log')
    _run([*common, 'check'], root / '05-pip-check.log')
    # Import only AFTER every pinned native byte/license has been verified.
    native_probe = subprocess.check_output([str(python), '-I', '-B', '-c',
        "import flint,json; from importlib.metadata import version; print(json.dumps({'python_flint':version('python-flint'),'flint':str(flint.__FLINT_VERSION__)}))"],
        timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    native_versions = json.loads(native_probe)
    if native_versions != {'python_flint': '0.9.0', 'flint': '3.6.0'}:
        raise ValueError('Native import/version mismatch')
    verify_installed(venv, inventory)
    packages = json.loads(subprocess.check_output([*map(str, common), 'list', '--format=json'], timeout=30,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    manifest = {'schema': 'inversion.numeric-installation/1', 'root': str(root),
                'python': str(python), 'probe': probe, 'versions': native_versions,
                'wheel_inventory_sha256': sha256((root / 'wheel-inventory.json').read_bytes()).hexdigest(),
                'inventory': inventory, 'packages': packages, 'precision': 128, 'threads': 1,
                'requirements_sha256': {name: sha256((REPO / name).read_bytes()).hexdigest()
                    for name in ('requirements-production.txt', 'requirements-inversion-v2.txt')}}
    write_json_private(root / 'installation-manifest.json', manifest)
    return python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps({'python': str(provision(args.root, base_python=Path(sys.executable)))}))


if __name__ == '__main__':
    main()
