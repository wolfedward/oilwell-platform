"""Read-only loopback viewer with a fixed file/data allowlist; no API proxy."""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .contracts import Artifact,Case
from .report import validate_public
from .storage import canonical_bytes,load_pack,safe_path,strict_json
from .waveforms import decode_channels


def case_data(pack,case):
    entries={a['path']:a for a in pack.manifest['artifacts']}
    artifacts=[]
    for name in case['artifact_paths']:
        raw=safe_path(pack.root/name).read_bytes()
        if sha256(raw).hexdigest()!=entries[name]['sha256']:raise ValueError('package changed')
        artifacts.append(Artifact(name,entries[name]['media_type'],raw))
    result={'case_id':case['case_id'],'category':case['category'],'facts':case['facts'],'source_kind':'synthetic'}
    kind=case['facts'].get('data_kind')
    if kind in ('cycle','current'):
        model=Case(case['case_id'],case['category'],tuple(artifacts),case['facts'],tuple(case['expected']))
        channels=decode_channels(model)
        meta=strict_json(next(a.content for a in artifacts if a.path.endswith('.json')))
        result.update(kind=kind,metadata=meta,channels=channels,prediction=[])
        if kind=='cycle':
            result['reference']=[{'power_index':str(i),'time_ns':str(i*7812500),
                'load_value_si':None if channels['load']['values'][i] is None else channels['load']['values'][i]*1000,
                'displacement_value_si':channels['displacement']['values'][i],
                'load_reason':None if channels['load']['quality'][i]==0 else 'invalid_quality',
                'displacement_reason':None if channels['displacement']['quality'][i]==0 else 'invalid_quality',
                'reference_edge_from_previous':i>0 and all(channels[r]['quality'][j]==0 for r in ('load','displacement') for j in (i-1,i)),
                'valid':False} for i in range(meta['frame_count'])]
            result['inversion_status']='blocked_no_approved_prediction'
    elif kind=='history':
        result.update(kind='history',metadata=strict_json(next(a.content for a in artifacts if a.path.endswith('.json'))))
    elif kind=='study':
        template=strict_json(next(a.content for a in artifacts if a.path.startswith('templates/')))
        result.update(kind='study',title=template['envelope']['title'],study_kind=template['envelope']['kind'],
            source=template['envelope']['source'],expected=case['expected'],dependencies=template['dependencies'])
    elif case['category']=='shape_only':result.update(kind='shape_only',shape=strict_json(artifacts[0].content))
    else:result.update(kind='protocol_fault',expected=case['expected'])
    result['downloads']=[{'path':a.path,'sha256':entries[a.path]['sha256'],'bytes':len(a.content)} for a in artifacts]
    return result


def serve_viewer(pack,public_report=None,*,port=0):
    if type(port) is not int or port not in range(65536) or port in (8765,8770,8772,8774):raise ValueError('unprotected loopback port required')
    pack=load_pack(pack.root);repo=Path(__file__).resolve().parents[2]
    report=None
    if public_report is not None:
        path=safe_path(Path(public_report))
        if path.stat().st_size>8*1024*1024:raise ValueError('public report too large')
        report=validate_public(strict_json(path.read_bytes()))
        if report['pack_id']!=pack.manifest['pack_id'] or report['pack_sha256']!=sha256((pack.root/'manifest.json').read_bytes()).hexdigest():
            raise ValueError('report belongs to another package')
    assets={'/':('frontend/testpack/index.html','text/html; charset=utf-8'),
        '/frontend/testpack/viewer.js':('frontend/testpack/viewer.js','text/javascript; charset=utf-8'),
        '/styles.css':('frontend/testpack/styles.css','text/css; charset=utf-8'),
        '/frontend/inversion/charts.js':('frontend/inversion/charts.js','text/javascript; charset=utf-8'),
        '/frontend/inversion/forms.js':('frontend/inversion/forms.js','text/javascript; charset=utf-8')}
    cases={c['case_id']:c for c in pack.manifest['cases']};downloads={'/pack/'+a['path']:a for a in pack.manifest['artifacts']}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.0'
        def log_message(self,*args):pass
        def do_POST(self):self.send_error(405)
        do_PUT=do_DELETE=do_PATCH=do_OPTIONS=do_POST
        def do_HEAD(self):self.do_GET()
        def do_GET(self):
            if self.headers.get_all('Host',[])!=[f'127.0.0.1:{self.server.server_port}']:
                self.send_error(403);return
            parsed=urlsplit(self.path)
            if parsed.query or parsed.fragment or '%' in self.path or '\\' in self.path or '..' in self.path.split('/'):
                self.send_error(400);return
            path=parsed.path
            try:
                if path in assets:
                    name,media=assets[path];raw=safe_path(repo/name).read_bytes()
                elif path=='/data/manifest.json':raw=canonical_bytes(pack.manifest);media='application/json'
                elif path=='/data/report.json':raw=canonical_bytes(report);media='application/json'
                elif path.startswith('/data/cases/') and path.endswith('.json') and path[12:-5] in cases:
                    raw=canonical_bytes(case_data(pack,cases[path[12:-5]]));media='application/json'
                elif path in downloads:
                    item=downloads[path];raw=safe_path(pack.root/item['path']).read_bytes()
                    if len(raw)!=item['size_bytes'] or sha256(raw).hexdigest()!=item['sha256']:raise ValueError('package changed')
                    media='application/octet-stream'
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type',media);self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                if self.command!='HEAD':self.wfile.write(raw)
            except (ValueError,OSError,KeyError):self.send_error(409,'Artifact integrity or availability failure')
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def get_request(self):
            sock,address=super().get_request();sock.settimeout(5);return sock,address
    server=Server(('127.0.0.1',port),Handler)
    thread=threading.Thread(target=server.serve_forever,name='testpack-read-only-viewer',daemon=True);thread.start()
    server.viewer_thread=thread
    return server
