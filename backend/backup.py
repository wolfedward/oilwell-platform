"""Verified SQLite backup and restore helpers for the local platform."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import uuid

from backend.database import Database


SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class BackupResult:
    path: Path
    integrity: str
    size_bytes: int
    created_at: str


def _verified(connection: sqlite3.Connection) -> str:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise sqlite3.DatabaseError(f"backup integrity check failed: {integrity}")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.DatabaseError("backup foreign key check failed")
    return integrity


def backup_database(database: Database, destination: Path, *, retain: int = 10) -> BackupResult:
    if not isinstance(database, Database):
        raise TypeError("database must be a Database instance")
    if isinstance(retain, bool) or not isinstance(retain, int) or not 1 <= retain <= 100:
        raise ValueError("retain must be between 1 and 100")
    directory = Path(destination).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source_path = Path(database.path).resolve()
    stamp = datetime.now(SHANGHAI)
    final_path = directory / f"platform-{stamp:%Y%m%d-%H%M%S-%f}.db"
    temporary_path = directory / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
    if final_path.resolve() == source_path or temporary_path.resolve() == source_path:
        raise ValueError("backup destination must differ from the live database")
    try:
        with database.connection() as source:
            with closing(sqlite3.connect(temporary_path)) as target:
                source.backup(target)
                target.commit()
                integrity = _verified(target)
        os.replace(temporary_path, final_path)
        backups = sorted(
            (path for path in directory.glob("platform-*.db") if path.is_file()),
            key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
        )
        for expired in backups[retain:]:
            expired.unlink()
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return BackupResult(
        path=final_path, integrity=integrity, size_bytes=final_path.stat().st_size,
        created_at=stamp.isoformat(),
    )


def restore_database(source: Path, destination: Path) -> None:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path == destination_path:
        raise ValueError("restore source and destination must differ")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.parent / f".{destination_path.name}.{uuid.uuid4().hex}.restore"
    try:
        with closing(sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)) as backup:
            _verified(backup)
            with closing(sqlite3.connect(temporary_path)) as restored:
                backup.backup(restored)
                restored.commit()
                _verified(restored)
        os.replace(temporary_path, destination_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
