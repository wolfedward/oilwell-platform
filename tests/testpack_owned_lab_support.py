"""Live integration requires explicit caller-supplied owned paths, never DB fixtures."""
from tools.testpack.owned_v2_verify import live_paths,run


def run_explicit_live():
    paths=live_paths()
    if paths is None:raise ValueError('explicit owned live opt-in required')
    return run(*paths)
