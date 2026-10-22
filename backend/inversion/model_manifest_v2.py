"""Server-owned v2 artifact identity. Native inspection is lazy and read-only."""
import base64
from hashlib import sha256
from importlib.metadata import distribution,version
from pathlib import Path
import platform
import sys

from .contracts import canonical_bytes
from .transactions import InversionError

SOURCE_FILES=tuple('''contracts.py parameter_contracts.py series.py policy.py geometry.py mechanics.py
    small_linalg.py motion.py losses.py solver.py comparison.py display_fill.py policy_v2.py
    parameter_contracts_v2.py coverage_contracts.py interval_runtime.py motion_bounds.py
    coverage_sources.py coverage_physics.py synthetic_registry.py solver_v2.py model_manifest_v2.py'''.split())


def installed_v2():
    from .models import InstalledModel
    root=Path(__file__).parent
    return InstalledModel('beam_energy_balance/2',2,tuple(root/name for name in SOURCE_FILES))


def numerical_inventory():
    from .interval_runtime import require_runtime
    runtime=dict(require_runtime())
    package=distribution('python-flint')
    entries=package.files
    if entries is None or not 1<=len(entries)<=1024:
        raise InversionError('proof_runtime_unavailable')
    native=[];licenses=[]
    for entry in entries:
        # RECORD itself and installer-generated files have no wheel digest.
        if entry.hash is None: continue
        path=Path(package.locate_file(entry))
        if entry.hash.mode!='sha256' or not path.is_file() or path.is_symlink():
            raise InversionError('proof_runtime_unavailable')
        raw=path.read_bytes(); digest=sha256(raw).digest()
        if base64.urlsafe_b64encode(digest).decode().rstrip('=')!=entry.hash.value:
            raise InversionError('proof_runtime_unavailable')
        item={'name':entry.as_posix(),'sha256':digest.hex()}
        if path.suffix.lower() in ('.dll','.pyd'): native.append(item)
        if '/licenses/' in entry.as_posix(): licenses.append(item)
    if not native or not licenses: raise InversionError('proof_runtime_unavailable')
    return dict(runtime=runtime,native_files=sorted(native,key=lambda v:v['name']),
        licenses=sorted(licenses,key=lambda v:v['name']),
        approved_wheel_sha256='1f3a3672d589e376d47288dcebcb712997dc719880a4dbf1779bf0f2b20d0293')


def snapshot_v2(model):
    from .coverage_sources import checker_sha256,installed_generator_sha256
    from .synthetic_registry import installed_config
    try:
        files=[{'name':path.name,'sha256':sha256(path.read_bytes()).hexdigest()} for path in model.files]
        policy=sha256(Path(__file__).with_name('policy_v2.py').read_bytes()).hexdigest()
        numeric=numerical_inventory()
    except (OSError,ValueError,ImportError) as error:
        raise InversionError('artifact_unavailable') from error
    return dict(installed_id=model.installed_id,algorithm='beam_energy_balance/2',
        parameter_schema='inversion.parameter-set/2',files=files,policy_contract_sha256=policy,
        python=dict(implementation=platform.python_implementation(),version=sys.version,
                    platform=platform.platform(),machine=platform.machine()),
        dependencies={'psycopg':version('psycopg'),'finite_input_runtime':numeric},
        checker_sha256=checker_sha256(),generator_sha256=installed_generator_sha256(),
        registry={profile:sha256(canonical_bytes(installed_config(profile))).hexdigest() for profile in ('smoke','extended')},
        numerical_assumptions=[
            'Finite registered synthetic inputs only; not field validated',
            'Actual native codes and seven-point nominal QR; reference channels excluded from prediction',
            'Independent 128-bit physical and numerical enclosures; fixed original load and coverage gates',
            'Approval does not certify hardware, field accuracy, or unseen dynamics'])
