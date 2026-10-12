"""Standalone loopback HTTP server. No production bootstrap, transport or worker."""
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit,unquote,parse_qs
from .auth import PreviewAuth
from .catalog import load_catalog
from .dataset import PreviewDataset
from .errors import PreviewError
from .repository import PreviewRepository
from .snapshot import verify_manifest

ROOT=Path(__file__).resolve().parents[2]
COOKIE='oilwell_page_preview'


class PreviewHandler(BaseHTTPRequestHandler):
    server_version='OilWellPreview/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self,*args): pass

    def _json(self,status,data,headers=None,error=False):
        payload={'ok':False,'error':data} if error else {'ok':True,'data':data}
        body=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length',str(len(body)))
        for key,value in (headers or {}).items(): self.send_header(key,value)
        self.end_headers(); self.wfile.write(body)

    def _body(self):
        if self.headers.get('Transfer-Encoding'): raise PreviewError('invalid_body','不支持分块请求',400)
        try: size=int(self.headers.get('Content-Length','0'))
        except ValueError: raise PreviewError('invalid_body','无效请求长度')
        if size<0: raise PreviewError('invalid_body','无效请求长度')
        if size>1024*1024:
            # Drain a bounded oversize request so Windows does not reset the
            # socket with unread body bytes before the client receives 413.
            if size<=8*1024*1024:
                remaining=size
                while remaining:
                    chunk=self.rfile.read(min(65536,remaining))
                    if not chunk: break
                    remaining-=len(chunk)
            raise PreviewError('body_limit','请求超过 1 MiB',413)
        try:
            body=json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(body,dict): raise ValueError()
            return body
        except (ValueError,UnicodeError): raise PreviewError('invalid_json','请求必须为 JSON 对象')

    def _token(self):
        try:
            cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
            return cookie[COOKIE].value if COOKIE in cookie else ''
        except Exception: return ''

    def _handle(self):
        try:
            host=self.headers.get('Host','')
            expected=f'127.0.0.1:{self.server.server_port}'
            if host!=expected: raise PreviewError('invalid_host','仅允许本机预览地址',403)
            parsed=urlsplit(self.path)
            path=unquote(parsed.path)
            if parsed.scheme or parsed.netloc or '\\' in path or '%' in path or any(x in {'.','..'} for x in path.split('/')):
                raise PreviewError('invalid_path','无效路径',404)
            query={key:values[-1] for key,values in parse_qs(parsed.query,keep_blank_values=True).items()}
            write=self.command!='GET'
            if write and (not path.startswith('/api/preview/') or path.startswith('/api/preview/legacy/')):
                raise PreviewError('control_disabled','预览禁止实际控制和旧指令重放',403)
            if write and self.headers.get('Origin')!='http://'+expected:
                raise PreviewError('invalid_origin','请求来源不匹配',403)
            if self.command=='GET' and path=='/api/preview/health':
                return self._json(200,{**self.server.repository.meta,'status':'ready','service':'页面预览'})
            if path=='/api/preview/session' and self.command=='POST':
                body=self._body(); token,session=self.server.auth.login(body.get('username'),body.get('password'))
                return self._json(200,session,{'Set-Cookie':f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/api/preview/'})
            if path.startswith('/api/'):
                session=self.server.auth.session(self._token())
                if write and not hmac.compare_digest(self.headers.get('X-Preview-CSRF',''),session['csrf_token']):
                    raise PreviewError('invalid_csrf','会话验证失败',403)
                if path=='/api/preview/session':
                    if self.command=='DELETE':
                        self.server.auth.logout(self._token())
                        return self._json(200,{'signed_out':True},{'Set-Cookie':f'{COOKIE}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/api/preview/'})
                    if self.command=='GET': return self._json(200,session)
                if write: raise PreviewError('control_disabled','该预览动作尚未开放',403)
                if path=='/api/preview/catalog': return self._json(200,self.server.catalog)
                if path=='/api/preview/meta': return self._json(200,self.server.repository.meta)
                if path.startswith('/api/preview/'):
                    if 'page_size' in query:
                        try: query['page_size']=str(min(100,max(1,int(query['page_size']))))
                        except ValueError: raise PreviewError('invalid_page_size','分页大小无效')
                    return self._json(200,self.server.repository.read(path,query,session['actor']))
                raise PreviewError('not_found','预览只支持独立接口',404)
            if write: raise PreviewError('control_disabled','禁止写入',403)
            return self._static(path)
        except PreviewError as error:
            self._json(error.status,{'code':error.code,'message':str(error),'details':error.details},error=True)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception:
            self._json(500,{'code':'preview_error','message':'预览处理失败，请检查本机日志','details':None},error=True)

    def _static(self,path):
        if path in ('/','/preview.html'): path='/preview.html'
        allowed=path in {'/preview.html','/styles.css','/assets/heneng-logo.png'} or (path.startswith('/frontend/') and Path(path).suffix in {'.js','.json','.css'}) or path.startswith('/assets/open-interface/')
        target=self.server.code_root/path.lstrip('/')
        if not allowed or not target.is_file() or not target.resolve().is_relative_to(self.server.code_root):
            raise PreviewError('not_found','文件不存在',404)
        current=target
        while current!=self.server.code_root:
            if current.is_symlink() or current.is_junction(): raise PreviewError('not_found','链接文件不可访问',404)
            current=current.parent
        body=target.read_bytes()
        self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(body))); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body)

    do_GET=_handle
    do_POST=_handle
    do_PATCH=_handle
    do_DELETE=_handle


def create_preview_server(root: Path,host: str='127.0.0.1',port: int=8772):
    if host!='127.0.0.1': raise ValueError('预览只允许 127.0.0.1')
    root=Path(root).resolve(strict=True)
    manifest=verify_manifest(root)
    auth=PreviewAuth(root/'private/accounts.json')
    meta={'mode':'preview','control_enabled':False,'snapshot_id':manifest['snapshot_id'],'snapshot_at':manifest['captured_at'],
          'data_from':manifest['data_from'],'data_to':manifest['data_to'],'source_kind':'software_fixture','page_catalog_version':'1'}
    repository=PreviewRepository(PreviewDataset(root/'business.db'),meta)
    server=ThreadingHTTPServer((host,port),PreviewHandler)
    server.daemon_threads=True
    server.auth=auth; server.repository=repository; server.catalog=load_catalog()
    server.code_root=(root/'code').resolve() if (root/'code').is_dir() else ROOT
    return server
