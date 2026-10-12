"""Explicit schema-based export policy. New columns require a policy review."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

SCHEMA = Path(__file__).resolve().parents[1] / 'schema.sql'
EXCLUDED_TABLES = {'sessions','auth_bootstrap_state'}
NULL_FIELDS = {'users':{'password_hash','password_salt','last_login_at'}, 'device_commands':{'signature','idempotency_key'}}
SENSITIVE = re.compile(r'password|secret|token|authorization|cookie|credential|signature|api[_-]?key|private[_-]?key',re.I)


def table_columns(db):
    return {row[0]:[r[1] for r in db.execute('PRAGMA table_info("'+row[0]+'")')]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}


def validate_schema(db):
    with closing(sqlite3.connect(':memory:')) as known:
        known.executescript(SCHEMA.read_text(encoding='utf-8'))
        allowed = table_columns(known)
    actual = table_columns(db)
    for table, columns in actual.items():
        if table not in allowed or set(columns)-set(allowed[table]):
            raise ValueError('快照含未审核的表或字段: '+table)
    if set(allowed)-set(actual):
        raise ValueError('快照缺少业务表')
    return actual


def redact_json(value):
    """Keep original serialization byte-for-byte unless sensitive keys occur."""
    if not isinstance(value,(str,bytes)):
        return value
    try:
        text = value.decode('utf-8') if isinstance(value,bytes) else value
        data = json.loads(text)
    except (ValueError, UnicodeError):
        return value
    changed = False
    def visit(obj):
        nonlocal changed
        if isinstance(obj,dict):
            result={}
            for key,item in obj.items():
                if SENSITIVE.search(key):
                    result[key]='[redacted]'; changed=True
                else:
                    result[key]=visit(item)
            return result
        if isinstance(obj,list): return [visit(x) for x in obj]
        return obj
    result=visit(data)
    if not changed: return value
    serialized=json.dumps(result,ensure_ascii=False,separators=(',',':'))
    return serialized.encode('utf-8') if isinstance(value,bytes) else serialized


def sanitize_row(table, columns, row):
    values=[]
    identity=row[columns.index('id')] if 'id' in columns else None
    for column, value in zip(columns,row):
        if column in NULL_FIELDS.get(table,set()): value=None
        elif table=='users' and column=='username': value=f'preview-actor-{identity}'
        elif table=='users' and column=='display_name': value=f'历史操作者 {identity}'
        else: value=redact_json(value)
        values.append(value)
    return values
