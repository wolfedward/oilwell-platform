"""Cross-process ownership primitives for the single local platform instance."""

import json
import os
from pathlib import Path
import tempfile
import time


class AlreadyRunningError(RuntimeError):
    """Raised when another process owns the platform lock."""


class InstanceLock:
    """An OS-released exclusive lock rooted at the platform log directory."""

    def __init__(self, root: Path):
        self.path = Path(root) / "platform.lock"
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            try:
                if self.path.stat().st_size < 1:
                    lock_file.seek(0)
                    lock_file.write(b"0")
                    lock_file.flush()
            except OSError as error:
                raise AlreadyRunningError("platform already running") from error
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise AlreadyRunningError("platform already running") from error
            else:
                import fcntl
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise AlreadyRunningError("platform already running") from error
            self._file = lock_file
            return self
        except BaseException:
            lock_file.close()
            raise

    def release(self):
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def write_runtime(path: Path, runtime: dict) -> None:
    """Atomically replace runtime state so readers never observe a partial JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".runtime-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(runtime, temporary, ensure_ascii=False, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
        for attempt in range(100):
            try:
                os.replace(temporary_name, path)
                break
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.02)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def remove_runtime_if_owner(path: Path, instance_id: str, shutdown_token: str) -> bool:
    """Delete only a runtime record still owned by this exact server instance."""
    path = Path(path)
    try:
        runtime = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if runtime.get("instance_id") != instance_id or runtime.get("shutdown_token") != shutdown_token:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
