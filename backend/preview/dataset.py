"""Read-only SQLite compatibility surface; writes are denied at the engine boundary."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


def _authorize(action, arg1, arg2, database, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() not in {'load_extension','writefile','readfile'}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in {'table_info','foreign_key_list','foreign_key_check','integrity_check'}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class PreviewDataset:
    def __init__(self, path: Path):
        self.path = Path(path).resolve(strict=True)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.set_authorizer(_authorize)
        try:
            yield db
        finally:
            db.close()

    def query_all(self, sql: str, parameters: tuple = ()) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def query_one(self, sql: str, parameters: tuple = ()) -> dict | None:
        with self.connection() as db:
            row = db.execute(sql, parameters).fetchone()
            return dict(row) if row is not None else None

    def scalar(self, sql: str, parameters: tuple = ()):
        with self.connection() as db:
            row = db.execute(sql, parameters).fetchone()
            return row[0] if row is not None else None

    def column(self, sql: str, parameters: tuple = ()) -> list:
        with self.connection() as db:
            return [row[0] for row in db.execute(sql, parameters)]
