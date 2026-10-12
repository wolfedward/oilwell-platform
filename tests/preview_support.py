"""Temporary-only fixtures. Never reads installed database configuration."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def make_source(root: Path) -> Path:
    path = root / 'source.db'
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript((ROOT / 'backend/schema.sql').read_text(encoding='utf-8'))
        db.executescript("""
        INSERT INTO units(id,name) VALUES ('U1','测试采油区');
        INSERT INTO well_groups(id,unit_id,name) VALUES ('G1','U1','一队');
        INSERT INTO roles(id,code,name) VALUES (1,'admin','系统管理员');
        INSERT INTO users(id,role_id,username,display_name,password_hash,password_salt)
          VALUES (1,1,'original-user','原身份','TEST-SECRET-HASH','TEST-SECRET-SALT');
        INSERT INTO sessions(user_id,token_hash,expires_at) VALUES(1,'TEST-SECRET-TOKEN','2099-01-01');
        INSERT INTO wells(id,group_id,code,name,is_low_output) VALUES('W1','G1','ST-001','一号井',1),('W2','G1','ST-002','二号井',0);
        INSERT INTO devices(id,well_id,device_type) VALUES('D1','W1','DSP+MCU'),('D2','W2','DSP+MCU');
        """)
        for well, scale in [('W1',1),('W2',2)]:
            for day, fluid, power in [('2026-09-10',12.0,210.0),('2026-09-11',13.4,234.2),('2026-09-12',12.8,225.3)]:
                db.execute('INSERT INTO production_daily(well_id,day,daily_oil,daily_fluid,power_kwh,pump_efficiency,stroke_m,frequency_hz,runtime_hours) VALUES(?,?,?,?,?,?,?,?,?)',
                           (well,day,fluid*.7*scale,fluid*scale,power*scale,.62,3.6,35,20))
        db.execute("INSERT INTO raw_messages(id,device_id,received_at,payload) VALUES(1,'D1','2026-09-12T10:00:00+08:00',?)", ('{"power_kw":9.5}',))
        db.execute("INSERT INTO telemetry(well_id,device_id,measured_at,metric_code,value,unit,raw_message_id) VALUES('W1','D1','2026-09-12T10:00:00+08:00','active_power',9.5,'kW',1)")
        db.execute("INSERT INTO dynagraphs(id,well_id,device_id,captured_at,sample_data) VALUES('DG1','W1','D1','2026-09-12T10:00:00+08:00',?)", (json.dumps({'sensor_samples':[[0,32.12345],[.2,62],[3.5,64],[3.6,34],[0,32.12345]],'samples':[[0,1],[90,12],[180,2],[270,-3],[360,1]]}),))
    return path


def read_source_rows(path: Path) -> dict:
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table:[dict(row) for row in db.execute('SELECT * FROM "'+table+'"')] for table in tables}


def find_secrets_in_served_snapshot(manifest: dict) -> list[str]:
    content = str(read_source_rows(Path(manifest['path'])))
    return [token for token in ('TEST-SECRET-HASH','TEST-SECRET-SALT','TEST-SECRET-TOKEN') if token in content]


def run_browser(case: str, *, width: int = 1366, height: int = 768) -> dict:
    fixture = ROOT / 'tests/fixtures' / ('preview_'+case+'.html')
    result = subprocess.run(['node',str(ROOT/'tests/fixtures/preview_runner.cjs'),fixture.as_uri(),str(width),str(height)],
                            cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)
