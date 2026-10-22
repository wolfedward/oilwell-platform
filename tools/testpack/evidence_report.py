"""Render actual escaped report content, never a magic-byte placeholder image."""
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import shutil
import subprocess

from .storage import canonical_bytes, safe_path


def evidence_html(summary):
    raw=json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)
    if len(raw.encode('utf-8'))>50000:
        raise ValueError('evidence summary too large')
    template=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    return template.replace('__SUMMARY__',html.escape(raw))


def render_evidence(summary:dict,target:Path)->Path:
    return _render(summary,target,'.png')


def render_evidence_pdf(summary:dict,target:Path)->Path:
    """Print the escaped evidence source as a real searchable PDF."""
    return _render(summary,target,'.pdf')


def _render(summary,target,suffix):
    target=safe_path(target,missing=True)
    if target.exists() or target.suffix!=suffix:
        raise ValueError('new bounded evidence target required')
    source=target.with_suffix('.html'); data=target.with_suffix('.json')
    page=evidence_html(summary)
    if suffix=='.pdf':
        page=page.replace('</style>','@page{size:A4;margin:18mm}body{margin:0;font-size:11px}h1{font-size:20px}pre{font-size:9px;line-height:1.4;overflow-wrap:anywhere;padding:10px}footer{margin-top:12px}</style>')
        page=page.replace('本页截图','本页 PDF')
    for path,raw in ((source,page.encode('utf-8')),(data,canonical_bytes(summary))):
        with path.open('xb') as stream:stream.write(raw)
    runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node=os.environ.get('OWP_TESTPACK_NODE') or shutil.which('node') or str(runtime/'bin/node.exe')
    env=dict(os.environ)
    if 'NODE_PATH' not in env and (runtime/'node_modules').is_dir():
        env['NODE_PATH']=str(runtime/'node_modules')
    result=subprocess.run([node,str(Path(__file__).with_name('render_evidence.cjs')),str(source),str(target)],
                          capture_output=True,timeout=60,env=env)
    if result.returncode or not target.is_file():
        raise RuntimeError('evidence rendering failed; source retained')
    payload=target.read_bytes()
    signature=b'%PDF-' if suffix=='.pdf' else b'\x89PNG\r\n\x1a\n'
    if not payload.startswith(signature) or len(payload)>4194304:
        raise ValueError('evidence outside attachment contract')
    record={p.name:sha256(p.read_bytes()).hexdigest() for p in (source,data,target)}
    with target.with_suffix('.hashes.json').open('xb') as stream:stream.write(canonical_bytes(record))
    return target
