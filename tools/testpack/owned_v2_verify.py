"""Explicit opt-in integration entry; never discovers or reuses a business DSN."""
import os
from pathlib import Path

from tools.review_deploy import _marker as component_marker
from tools.review_deploy_process import inspect_pid,owned_process
from .lab import checked_pack,load_lab
from .storage import load_pack,safe_path


def live_paths(env=None):
    env=os.environ if env is None else env
    if env.get('OILWELL_TESTPACK_INVERSION_V2_LIVE')!='1':return None
    names=('OILWELL_TESTPACK_LIVE_ROOT','OILWELL_TESTPACK_LIVE_PACK','OILWELL_TESTPACK_LIVE_DELIVERY')
    if any(not env.get(name) for name in names):raise ValueError('explicit live mode requires all owned paths')
    return tuple(Path(env[name]) for name in names)


def verify_owned_runtime(lab,pack):
    pack=checked_pack(lab,pack)
    for root in (lab.monitoring_root,lab.review_root):
        marker=component_marker(root)
        if marker['state']!='running' or not marker['processes']:raise ValueError('owned runtime is not running')
        required={'http','broker','center','reliability_publisher','wave_receiver','wave_assembler','wave_publisher','inversion'}
        if not required<=set(marker['processes']):raise ValueError('owned runtime components missing')
        for expected in marker['processes'].values():
            if not owned_process(expected,inspect_pid(expected['pid'])):raise ValueError('owned process identity changed')
    return pack


def run(root:Path,pack:Path,delivery:Path):
    from .inversion_run_v2 import run_inversion_cases
    lab=load_lab(root);package=verify_owned_runtime(lab,load_pack(pack))
    destination=safe_path(delivery,missing=True)
    if destination.parent!=lab.root or destination.exists():raise ValueError('new owned run directory required')
    outcomes=run_inversion_cases(lab,package,destination)
    return {'outcomes':outcomes,'passed':bool(outcomes) and all(o.assertion_status=='passed' for o in outcomes)}
