"""Installed finite case constants, never an uploaded expression or model.

Counter/variant rules are sealed before evaluating inverse success. Historical
binary64 variant selection is retained, then its decimal text defines the exact
registered constant. Physical proof arithmetic must not use binary64.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction as Q
from hashlib import sha256
import json
import math

from .contracts import canonical_bytes, freeze
from .transactions import InversionError


@dataclass(frozen=True)
class CaseDefinition:
    case_id: str
    config_sha256: str
    body: Mapping


def installed_config(profile):
    if profile not in ('smoke', 'extended'): raise ValueError('Unregistered profile')
    return {'schema':'owp.testpack.config/2','profile':profile,'seed':20261022,
            'start_utc':'2026-09-30T16:00:00.000000Z','timezone':'Asia/Shanghai',
            'max_bytes':268435456,'generator_version':'finite-input/2'}


_CONFIGS = {sha256(canonical_bytes(installed_config(p))).hexdigest(): p for p in ('smoke','extended')}
_BASE_CASES = tuple('cycle-'+name for name in ('base','balance','friction','speed','noise','missing','clipped','shifted'))


def case_ids(config_sha256):
    profile = _CONFIGS.get(config_sha256)
    if profile is None: raise InversionError('coverage_basis_mismatch')
    return _BASE_CASES + (tuple(f'cycle-variant-{i:02d}' for i in range(40)) if profile=='extended' else ())


def counter_fraction(case_id, channel, counter):
    # Same exact preimage and UINT64->binary64 rule as sha256-counter/1.
    raw=(json.dumps([20261022,case_id,channel,counter],ensure_ascii=False,separators=(',',':'))+'\n').encode()
    integer=int.from_bytes(sha256(raw).digest()[:8],'big')
    return Q.from_float(min(integer/2**64,math.nextafter(1.,0.)))


def _text(value):
    text=format(Decimal(str(value)),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def case_definition(config_sha256: str, case_id: str) -> CaseDefinition:
    if case_id not in case_ids(config_sha256): raise InversionError('coverage_basis_mismatch')
    body={'geometry':{'pivot_x_m':'3','pivot_y_m':'2','crank_radius_m':'0.5','rear_arm_m':'2',
          'pitman_length_m':'3','horsehead_radius_m':'6','branch':1,'rotation_sign':1,'displacement_sign':1},
          'bodies':{'crank_inertia_kg_m2':'200','crank_first_moment_x_kg_m':'1800',
          'crank_first_moment_y_kg_m':'0','beam_inertia_kg_m2':'0','beam_first_moment_x_kg_m':'0',
          'beam_first_moment_y_kg_m':'0','pitman_mass_kg':'0','pitman_com_fraction':'0.5',
          'pitman_inertia_kg_m2':'0','gravity_m_s2':'9.80665'},
          'drive':{'upstream_inertia_kg_m2':'30','ratio':'100','crank_friction_Nm':'300',
                   'viscous_friction_Nm_s':'100','loss_W':'1200','loss_error_W':'0.02'},
          'load':{'mean_N':'46000','half_span_N':'14000','transition_speed_m_s':'0.05'},
          'motion':{'period_s':'8','epsilon':'0','power_window_ns':1000000},
          'sample_rate_hz':128,'sample_count':1025,'source_kind':'synthetic',
          'scope':'finite_synthetic_input/1','variant':case_id.removeprefix('cycle-')}
    variant=body['variant']
    if variant=='balance': body['bodies']['crank_first_moment_x_kg_m']='1440'
    if variant=='friction': body['drive']['crank_friction_Nm']='450'
    if variant=='speed': body['motion']['epsilon']='0.1'
    if variant.startswith('variant-'):
        u=lambda key:float(counter_fraction(case_id,key,0))
        body['load'].update(mean_N=_text(42000+8000*u('mean')),half_span_N=_text(10000+6000*u('span')))
        body['drive']['crank_friction_Nm']=_text(250+300*u('friction'))
        body['bodies']['crank_first_moment_x_kg_m']=_text(1400+600*u('balance'))
    return CaseDefinition(case_id,config_sha256,freeze(body))
