"""Windows local NTFS ownership; fixed-byte OS lock, never PID/stale deletion.

The identity sidecar guards accidental foreign-file recovery, not local tampering.
Other storage forms require a separately verified implementation.
"""
import ctypes
import json
import os
from pathlib import Path

from .errors import WaveformError

APPLICATION_ID = 0x4F575053


def failure(code):
    return WaveformError('waveform.sender_' + code, 'Local waveform sender operation was not confirmed')


def canonical_path(value):
    raw = str(value)
    if os.name != 'nt' or raw.startswith(('\\\\', '//')):
        raise failure('path_unsupported')
    path = Path(value).absolute()
    if any(':' in part or part.endswith((' ', '.')) for part in path.parts[1:] if part not in ('.', '..')):
        raise failure('path_unsupported')
    path = path.resolve()
    if str(path).startswith('\\\\') or not path.parent.is_dir() or os.path.isreserved(path):
        raise failure('path_unsupported')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    if kernel.GetDriveTypeW(path.anchor) != 3:
        raise failure('path_unsupported')
    fs = ctypes.create_unicode_buffer(64)
    kernel.GetVolumeInformationW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    if not kernel.GetVolumeInformationW(path.anchor, None, 0, None, None, None, fs, len(fs)) or fs.value != 'NTFS':
        raise failure('path_unsupported')
    return Path(os.path.normcase(path))


def identity(path):
    info = path.lstat()
    if not path.is_file() or path.is_symlink() or info.st_nlink != 1:
        raise failure('path_unsupported')
    return [info.st_dev, info.st_ino]


class Ownership:
    def __init__(self, path):
        import msvcrt
        self.path = canonical_path(path)
        self.marker = Path(str(self.path) + '.identity')
        self.lock_path = Path(str(self.path) + '.owner')
        self.handle = None
        # Never follow an alternate lock/marker/journal name.
        for suffix in ('', '.identity', '.owner', '-journal', '-wal', '-shm'):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists() or candidate.is_symlink():
                identity(candidate)
        try:
            self.handle = self.lock_path.open('a+b', buffering=0)
            os.set_inheritable(self.handle.fileno(), False)
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            self.lock_identity = identity(self.lock_path)
        except BaseException as error:
            if self.handle:
                self.handle.close()
                self.handle = None
            if isinstance(error, OSError):
                raise failure('busy') from None
            raise

    def read(self, source_hash):
        if any(Path(str(self.path) + suffix).exists() for suffix in ('-wal', '-shm')):
            raise failure('ownership_invalid')
        if not self.path.exists() or not self.marker.exists() or self.marker.stat().st_size > 2048:
            raise failure('ownership_invalid')
        marker = json.loads(self.marker.read_bytes())
        if (set(marker) != {'file', 'path', 'token', 'source'} or marker['file'] != identity(self.path)
                or marker['path'] != str(self.path) or marker['source'] != source_hash):
            raise failure('ownership_invalid')
        with self.path.open('rb') as handle:
            header = handle.read(100)
        if (len(header) != 100 or header[:16] != b'SQLite format 3\x00'
                or header[18:20] != b'\x01\x01'
                or int.from_bytes(header[60:64], 'big') != 1
                or int.from_bytes(header[68:72], 'big') != APPLICATION_ID):
            raise failure('ownership_invalid')
        self.database_identity = identity(self.path)
        self.marker_identity = identity(self.marker)
        return marker

    def establish(self, source_hash, token):
        record = {'file': identity(self.path), 'path': str(self.path), 'token': token, 'source': source_hash}
        with self.marker.open('xb') as handle:
            handle.write(json.dumps(record, separators=(',', ':')).encode('ascii'))
            handle.flush()
            os.fsync(handle.fileno())
        self.read(source_hash)

    def check(self):
        if (identity(self.path) != self.database_identity or identity(self.marker) != self.marker_identity
                or identity(self.lock_path) != self.lock_identity):
            raise failure('ownership_invalid')

    def close(self):
        if self.handle:
            self.handle.close()  # OS releases the lock even after a process crash.
            self.handle = None
