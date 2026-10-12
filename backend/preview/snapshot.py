"""Consistent private backups and credential-free, immutable business snapshots."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid
from .snapshot_policy import SCHEMA, EXCLUDED_TABLES, NULL_FIELDS, validate_schema, table_columns, sanitize_row


def secure_directory(path: Path):
    path=Path(path)
    for parent in (path,*path.parents):
        if parent.exists() and (parent.is_symlink() or parent.is_junction()):
            raise ValueError('私有目录不能经过链接')
    path.mkdir(parents=True,exist_ok=False)
    if os.name=='nt':
        identity=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,check=True,text=True).stdout
        import csv
        sid=list(csv.reader([identity.strip()]))[0][1]
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],check=True,capture_output=True)
        escaped=str(path).replace("'","''")
        script=f"$a=Get-Acl -LiteralPath '{escaped}'; $a.Access | ForEach-Object {{$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value}}"
        acl=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,check=True)
        if set(acl.stdout.split())-{sid,'S-1-5-18'}:
            raise ValueError('私有目录权限检查失败')
    else:
        path.chmod(0o700)


def file_hash(path):
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def _value(value):
    return {'blob':value.hex()} if isinstance(value,bytes) else value


def describe(path: Path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        violations=[list(r) for r in db.execute('PRAGMA foreign_key_check')]
        if integrity!='ok': raise ValueError('快照完整性失败')
        if violations: raise ValueError('快照外键检查失败')
        tables={}
        for table,columns in table_columns(db).items():
            keys=[r[1] for r in sorted(db.execute(f'PRAGMA table_info("{table}")'),key=lambda r:r[5]) if r[5]]
            order=','.join('"'+k+'"' for k in keys) or 'rowid'
            digest=hashlib.sha256(); count=0
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
                digest.update(json.dumps([_value(v) for v in row],ensure_ascii=False,separators=(',',':')).encode('utf-8')+b'\n'); count+=1
            tables[table]={'rows':count,'primary_key':keys,'columns':columns,'sha256':digest.hexdigest()}
        return {'integrity':integrity,'foreign_key_violations':violations,'tables':tables,'schema_version':db.execute('PRAGMA user_version').fetchone()[0]}


def _manifest(path: Path, extra):
    result={**extra,**describe(path),'path':str(path.resolve()),'file_sha256':file_hash(path)}
    path.with_suffix('.manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def capture_snapshot(source: Path, private_root: Path) -> dict:
    source=Path(source).resolve(strict=True)
    secure_directory(Path(private_root))
    target=Path(private_root)/'source.db'
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=5)) as src, closing(sqlite3.connect(target)) as dst:
        src.execute('PRAGMA query_only=ON')
        src.backup(dst,pages=256,sleep=.01)
    return _manifest(target,{'snapshot_id':str(uuid.uuid4()),'captured_at':datetime.now(timezone.utc).isoformat(),'source_path':str(source)})


def build_business_snapshot(raw_snapshot: Path, target: Path) -> dict:
    raw_snapshot=Path(raw_snapshot).resolve(strict=True); target=Path(target)
    if target.exists() or target.with_suffix('.manifest.json').exists(): raise FileExistsError(target)
    if not target.parent.is_dir() or target.is_symlink(): raise ValueError('目标父目录不存在或无效')
    with closing(sqlite3.connect(raw_snapshot.as_uri()+'?mode=ro',uri=True)) as src:
        tables=validate_schema(src)
        describe(raw_snapshot)
        with closing(sqlite3.connect(target)) as dst:
            dst.executescript(SCHEMA.read_text(encoding='utf-8'))
            for table in EXCLUDED_TABLES: dst.execute(f'DROP TABLE "{table}"')
            for table,columns in tables.items():
                if table in EXCLUDED_TABLES: continue
                names=','.join('"'+c+'"' for c in columns)
                placeholders=','.join('?' for _ in columns)
                dst.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                                (sanitize_row(table,columns,row) for row in src.execute(f'SELECT {names} FROM "{table}"')))
            dst.commit()
            # Owned, audited query indexes only; never execute source database DDL.
            for name,expression in {
                'telemetry_latest':'telemetry(well_id,measured_at DESC,id DESC)',
                'telemetry_days':'telemetry(well_id,substr(measured_at,1,10),measured_at,id)',
                'telemetry_channels':'telemetry(device_id,metric_code,id DESC)',
                'telemetry_energy':'telemetry(well_id,metric_code,status,datetime(measured_at),id)',
                'raw_status':'raw_messages(status)',
                'raw_device':'raw_messages(device_id,id DESC)',
                'heartbeat_latest':'device_heartbeats(device_id,reported_at DESC,id DESC)',
                'dynagraph_latest':'dynagraphs(well_id,captured_at DESC,id DESC)',
                'diagnosis_latest':'diagnoses(well_id,created_at DESC,id DESC)',
            }.items():dst.execute('CREATE INDEX preview_'+name+' ON '+expression)
            dst.commit()
            date_range=dst.execute('SELECT min(day),max(day) FROM production_daily').fetchone()
            actors={str(r[0]):f'preview-actor-{r[0]}' for r in dst.execute('SELECT id FROM users')}
    raw_manifest=json.loads(raw_snapshot.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    return _manifest(target,{'snapshot_id':raw_manifest['snapshot_id'],'captured_at':raw_manifest['captured_at'],
                            'source_sha256':raw_manifest['file_sha256'],'exclusions':{'tables':sorted(EXCLUDED_TABLES),'fields':{k:sorted(v) for k,v in NULL_FIELDS.items()},'json_keys':'credential keys redacted'},
                            'actor_map':actors,'data_from':date_range[0],'data_to':date_range[1]})


def verify_manifest(root: Path) -> dict:
    path=Path(root)
    if path.is_dir(): path=path/'business.db'
    manifest=json.loads(path.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    if file_hash(path)!=manifest['file_sha256']: raise ValueError('快照摘要不匹配')
    actual=describe(path)
    if actual['tables']!=manifest['tables']: raise ValueError('逐表摘要不匹配')
    return manifest
