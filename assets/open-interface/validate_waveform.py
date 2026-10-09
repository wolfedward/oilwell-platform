"""Standalone exact-byte OWP-WAVE/1.0 capture validator (Python 3.13+)."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from waveform_errors import ContractError
from waveform_files import scan_file


def main(argv=None):
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
