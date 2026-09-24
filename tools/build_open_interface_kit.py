"""Build a deterministic public kit, never discovering local files by glob."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PUBLIC_FILES = ('index.html', 'portal.css', 'INTERFACE.md', 'HARDWARE.md', 'ROADMAP.md',
                'LICENSE.txt', 'telemetry.schema.json', 'telemetry-example.json',
                'validate_telemetry.py', 'hardware-overview.svg', 'terminal-interfaces.svg',
                'ingest-ack.schema.json', 'ingest-ack-example.json', 'validate_ingest_ack.py')
KIT = Path(__file__).resolve().parents[1] / 'assets/open-interface'


def build(root=KIT):
    root = Path(root).resolve(strict=True)
    payloads = {}
    for name in PUBLIC_FILES:
        source = root / name
        if source.is_symlink() or not source.is_file() or source.resolve().parent != root:
            raise ValueError(f'missing or nonlocal public source: {name}')
        payloads[name] = source.read_bytes()
    destination = root / 'open-interface-r1.zip'
    if destination.is_symlink():
        raise ValueError('archive must not be a symbolic link')
    descriptor, temporary = tempfile.mkstemp(prefix='.public-kit-', suffix='.zip', dir=root)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as archive:
            for name, payload in payloads.items():
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, payload)
        with ZipFile(temporary) as archive:
            if archive.namelist() != list(PUBLIC_FILES):
                raise ValueError('archive whitelist mismatch')
            for name, payload in payloads.items():
                if archive.read(name) != payload or (root / name).read_bytes() != payload:
                    raise ValueError(f'archive/source verification failed: {name}')
        report = {'sha256': sha256(temporary.read_bytes()).hexdigest(),
                  'files': {name: sha256(payload).hexdigest() for name, payload in payloads.items()}}
        os.replace(temporary, destination)
        return report
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=KIT)
    args = parser.parse_args()
    print(json.dumps(build(args.root), sort_keys=True))
