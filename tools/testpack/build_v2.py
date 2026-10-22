"""New owned v2 packages; old packages/configuration remain untouched."""
from pathlib import Path

from .config_v2 import config_dict_v2
from .cycles_v2 import generate_cycles_v2
from .cycles import generate_shape_cases
from .currents import generate_currents
from .history import generate_history
from .study_inputs import generate_studies
from .storage import check_budget,generator_fingerprint_v2,safe_path,write_pack
from .waveforms import encode_cycle,waveform_negative_cases


def build_v2(config,target:Path):
    config_dict_v2(config)
    target=safe_path(target,missing=True)
    if target.exists(): raise ValueError('output already exists')
    check_budget([16*1024*1024 if config.profile=='smoke' else 96*1024*1024],config.max_bytes)
    generator=generator_fingerprint_v2()
    cycles=generate_cycles_v2(config)
    waves=tuple(encode_cycle(c,config) for c in cycles)
    currents=generate_currents(config); history=generate_history(config)
    studies=generate_studies(config,cycles,history,currents)
    cases=(*waves,*currents,*history,*studies,*generate_shape_cases(config),*waveform_negative_cases(waves[0]))
    if generator_fingerprint_v2()!=generator:
        raise ValueError('generator changed during build; rerun against a fixed source tree')
    return write_pack(target,config,cases,generator)
