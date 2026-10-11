"""Explicit study schema migration."""
from pathlib import Path


def migrate_studies(database):
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    with database.connection() as connection:
        connection.executescript("BEGIN IMMEDIATE;\n" + schema)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("study schema foreign key violation")
