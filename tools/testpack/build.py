"""Offline-only package assembly. No services or external database access."""
from pathlib import Path
from .config import config_dict
from .contracts import PackConfig
from .storage import check_budget,generator_fingerprint,safe_path,write_pack
from .cycles import generate_cycles,generate_shape_cases
from .currents import generate_currents
from .history import generate_history
from .study_inputs import generate_studies
from .waveforms import encode_cycle,waveform_negative_cases


def build(config:PackConfig,target:Path):
    config_dict(config)
    target=safe_path(target,missing=True)
    if target.exists():
        raise ValueError('output already exists')
    # Conservative encoded+metadata upper bound, checked before waveform/history allocation.
    check_budget([16*1024*1024 if config.profile=='smoke' else 96*1024*1024],config.max_bytes)
    generator=generator_fingerprint()
    cycles=generate_cycles(config)
    waves=tuple(encode_cycle(c,config) for c in cycles)
    currents=generate_currents(config)
    history=generate_history(config)
    studies=generate_studies(config,cycles,history,currents)
    cases=(*waves,*currents,*history,*studies,*generate_shape_cases(config),*waveform_negative_cases(waves[0]))
    if generator_fingerprint()!=generator:
        raise ValueError('generator changed during build; rerun against a fixed source tree')
    return write_pack(target,config,cases,generator)
