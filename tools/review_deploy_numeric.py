"""Pinned numerical capability of a newly owned v2 installation, not execution success."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from .provision_inversion_v2 import WHEEL_NAME,WHEEL_SHA256,probe_python,inspect_wheel,verify_installed,_run
from .review_deploy_paths import verify_target,write_json_private,private_acl
from .review_deploy_source import verify_export,run_exported_backend


def compare_inventory(installed,actual):
    expected=installed['inventory']
    if expected['wheel_sha256']!=WHEEL_SHA256 or actual['approved_wheel_sha256']!=WHEEL_SHA256:
        raise ValueError('unapproved numerical wheel')
    for key,field in (('native','native_files'),('licenses','licenses')):
        rows=actual[field]
        if (not expected[key] or len(rows)!=len(expected[key]) or
            {row['name']:row['sha256'] for row in rows}!=expected[key]):
            raise ValueError('installed numerical inventory differs')
    return {'installed_wheel_sha256':WHEEL_SHA256,'verified':True,'inventory':actual}


def install(root):
    root=Path(root);marker=verify_target(root)
    if marker.get('inversion_v2') is not True or marker['state']!='claimed':
        raise ValueError('only a newly claimed v2 installation may install native dependencies')
    verify_export(root/'code',marker['source'])
    venv=root/'venv';python=venv/'Scripts/python.exe'
    probe=probe_python(python)
    if probe!={'version':'3.13.2','implementation':'CPython','system':'Windows','bits':64,'gil':1}:
        raise ValueError('CPython 3.13.2 Windows x64 GIL required')
    owned=root/'private/inversion-v2';owned.mkdir();private_acl(owned)
    wheels=owned/'wheels';wheels.mkdir()
    requirements=root/'code/requirements-inversion-v2.txt'
    common=[python,'-I','-B','-m','pip','--isolated','--disable-pip-version-check']
    _run([*common,'download','--index-url','https://pypi.org/simple','--only-binary=:all:',
        '--require-hashes','--no-deps','-r',requirements,'--dest',wheels],owned/'download.log')
    inventory=inspect_wheel(wheels/WHEEL_NAME)
    write_json_private(owned/'wheel-inventory.json',inventory)
    _run([*common,'install','--no-index','--find-links',wheels,'--no-deps','--only-binary=:all:',
        '--require-hashes','-r',requirements],owned/'install.log')
    _run([*common,'check'],owned/'pip-check.log')
    verify_installed(venv,inventory)
    actual=run_exported_backend(root,'numeric_inventory')
    manifest={'schema':'inversion.owned-numeric-installation/1','instance_id':marker['instance_id'],
        'probe':probe,'inventory':inventory,'actual':actual,
        'requirements_sha256':sha256(requirements.read_bytes()).hexdigest()}
    compare_inventory(manifest,actual)
    write_json_private(owned/'installation-manifest.json',manifest)
    return manifest


def verify(root):
    root=Path(root);marker=verify_target(root)
    if marker.get('inversion_v2') is not True:raise ValueError('not an owned v2 instance')
    verify_export(root/'code',marker['source'])
    path=root/'private/inversion-v2/installation-manifest.json'
    if path.is_symlink() or path.stat().st_nlink!=1:raise ValueError('linked installation manifest')
    installed=json.loads(path.read_text(encoding='utf-8'))
    if (installed['instance_id']!=marker['instance_id'] or installed['requirements_sha256']!=
        sha256((root/'code/requirements-inversion-v2.txt').read_bytes()).hexdigest()):
        raise ValueError('numeric installation provenance changed')
    inventory=inspect_wheel(root/'private/inversion-v2/wheels'/WHEEL_NAME)
    if inventory!=installed['inventory']:raise ValueError('wheel inventory changed')
    verify_installed(root/'venv',inventory)
    actual=run_exported_backend(root,'numeric_inventory')
    if actual!=installed['actual']:raise ValueError('numerical runtime changed')
    return compare_inventory(installed,actual)
