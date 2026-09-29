"""Validate an exact-byte OWP-WAVE file without external connections."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.waveforms.errors import ContractError
from backend.waveforms.files import scan_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate one .owpwave capture file")
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.input.suffix != ".owpwave":
            raise ContractError("contract_invalid")
        with args.input.open("rb") as source:
            result = scan_file(source)
    except (ContractError, OSError) as error:
        code = error.code if isinstance(error, ContractError) else "input_unavailable"
        print(json.dumps({"ok": False, "reason_code": code}, separators=(",", ":")))
        return 2
    print(json.dumps({"ok": True, "sha256": result.sha256,
                      "manifest_sha256": result.manifest_sha256,
                      "size_bytes": result.size_bytes, "final_lf": result.final_lf,
                      "lines": len(result.lines),
                      "capture_id": str(result.manifest.key.capture_id)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
