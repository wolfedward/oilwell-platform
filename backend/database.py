"""SQLite persistence boundary for the offline platform."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence


class Database:
    """Open short-lived SQLite connections with consistent platform settings."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one configured database connection and finish its transaction."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        """Create the base schema; statements are safe to apply repeatedly."""
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        with self.connection() as connection:
            connection.executescript(schema)

    def table_names(self) -> set[str]:
        return set(
            self.column("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        )

    def query_one(self, statement: str, parameters: Sequence = ()) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(statement, parameters).fetchone()

    def query_all(self, statement: str, parameters: Sequence = ()) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(statement, parameters).fetchall()

    def scalar(self, statement: str, parameters: Sequence = ()):
        row = self.query_one(statement, parameters)
        return None if row is None else row[0]

    def column(self, statement: str, parameters: Sequence = ()) -> list:
        return [row[0] for row in self.query_all(statement, parameters)]

    def execute(self, statement: str, parameters: Sequence = ()) -> int:
        with self.connection() as connection:
            return connection.execute(statement, parameters).rowcount

    def executemany(self, statement: str, parameter_sets: Sequence[Sequence]) -> int:
        with self.connection() as connection:
            return connection.executemany(statement, parameter_sets).rowcount

    @contextmanager
    def transaction(
        self, actor_id: int | None, action: str, object_type: str, object_id: str | None
    ) -> Iterator[sqlite3.Connection]:
        """Commit business changes and their audit entry as one transaction."""
        with self.connection() as connection:
            yield connection
            connection.execute(
                """
                INSERT INTO audit_logs(actor_id, action, object_type, object_id)
                VALUES(?, ?, ?, ?)
                """,
                (actor_id, action, object_type, object_id),
            )
