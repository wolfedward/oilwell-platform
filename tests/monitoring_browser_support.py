"""Headless workbench component tests; no installed services or preview adapters."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run_browser(case, *, width=1366, height=768, data=None, screenshot_dir=None):
    fixture = ROOT / 'tests/fixtures' / ('monitoring_'+case+'.html')
    runtime = Path(os.environ['USERPROFILE'])/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    env = {**os.environ, 'NODE_PATH': str(runtime/'node_modules')}
    with tempfile.TemporaryDirectory(prefix='monitoring-browser-') as temporary:
        if data is not None:
            content = fixture.read_text(encoding='utf-8').replace('../../', ROOT.as_uri()+'/')
            content = content.replace('MONITORING_DATA', json.dumps(data,ensure_ascii=False).replace('<','\\u003c'))
            fixture = Path(temporary)/'fixture.html'
            fixture.write_text(content, encoding='utf-8')
        result = subprocess.run([str(runtime/'bin/node.exe'),str(ROOT/'tests/fixtures/preview_runner.cjs'),
            fixture.as_uri(),str(width),str(height),'',str(screenshot_dir or '')],cwd=ROOT,env=env,
            capture_output=True,text=True,encoding='utf-8',timeout=60)
    if result.returncode: raise AssertionError(result.stderr)
    return json.loads(result.stdout)
