"""Pure synthetic parser fixtures; no approvals, database or installed case proof."""
import json
from pathlib import Path


def fixture_body():
    return json.loads((Path(__file__).parent/'fixtures/inversion_v2_contract.json').read_bytes())
