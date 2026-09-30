"""Authenticated strict HTTP adapter with a commit-before-output boundary."""
from dataclasses import replace
import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit,unquote_to_bytes
from . import routes
from .commands import (Actor,Command,import_request_hash,evidence_request_hash,quality_request_hash,
                       pair_request_hash,dataset_request_hash,export_request_hash)
from .contracts import CaptureKey,require_fields
from .errors import ContractError,WaveformError
from .permissions import required_permissions
from .queries import public
from backend.auth import AuthError,PermissionDenied
from backend.ingest_reliability.config import _origin


def strict_json(data):
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result: raise ValueError()
            result[k]=v
        return result
    def reject(value): raise ValueError()
    def number(value):
        result=float(value)
        if not math.isfinite(result): raise ValueError()
        return result
    try:
        text=data.decode('utf-8'); depth=0; quoted=False; escaped=False
        for c in text:
            if quoted:
                if escaped: escaped=False
                elif c=='\\': escaped=True
                elif c=='"': quoted=False
            elif c=='"': quoted=True
            elif c in '[{':
                depth+=1
                if depth>16: raise ValueError()
            elif c in ']}': depth-=1
        result=json.loads(text,object_pairs_hook=unique,parse_constant=reject,parse_float=number)
        if type(result) is not dict: raise ValueError()
        return result
    except (ValueError,UnicodeError,RecursionError,OverflowError): raise routes.invalid('Strict JSON object required') from None


class WaveformApi:
    def __init__(self,handler): self.h=handler; self.started=False
    def get(self,path): return self._dispatch('GET',path)
    def post(self,path): return self._dispatch('POST',path)
    def unsupported(self,path): return self._dispatch('unsupported',path)

    def _json(self,status,body):
        payload=json.dumps(public(body),ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        self._headers(status,len(payload),'application/json; charset=utf-8')
        if self.h.command!='HEAD': self._write(payload)

    def _headers(self,status,length,content_type,extra=()):
        self.started=True
        previous=self.h.connection.gettimeout()
        try:
            self.h.connection.settimeout(5)
            self.h.send_response(status); self.h.send_header('Content-Length',str(length)); self.h.send_header('Content-Type',content_type)
            self.h.send_header('Cache-Control','no-store'); self.h.send_header('X-Content-Type-Options','nosniff')
            for name,value in extra: self.h.send_header(name,value)
            self.h.end_headers()
        finally: self.h.connection.settimeout(previous)

    def _write(self,data,deadline=None):
        previous=self.h.connection.gettimeout()
        try:
            for offset in range(0,len(data),65536):
                remaining=(deadline-time.monotonic()) if deadline else 5
                if remaining<=0: raise TimeoutError()
                self.h.connection.settimeout(min(5,remaining)); self.h.wfile.write(data[offset:offset+65536])
        finally: self.h.connection.settimeout(previous)

    def _security(self,method,runtime):
        h=self.h
        try:
            host=h.headers.get('Host','')
            if _origin('http://'+host) not in runtime.allowed_origins: raise ValueError()
            origin=h.headers.get('Origin')
            if origin is not None and _origin(origin) not in runtime.allowed_origins: raise ValueError()
        except Exception: raise WaveformError('waveform.origin_denied','Host or Origin is not allowed',403) from None
        authorization=h.headers.get('Authorization','')
        # Match the inherited extractor's Bearer branch, not its cookie fallback.
        bearer=authorization.startswith('Bearer ') and bool(authorization[7:].strip())
        if method=='POST' and origin is None and not bearer:
            raise WaveformError('waveform.origin_required','Origin or Bearer authentication required',403)

    def _body(self,limit,content_type):
        h=self.h; length=h.headers.get('Content-Length')
        if length is None or not re.fullmatch(r'0|[1-9][0-9]{0,9}',length): raise routes.invalid('Single Content-Length required')
        length=int(length)
        if length>limit: raise WaveformError('waveform.body_too_large','Request exceeds body limit',413)
        if h.headers.get('Content-Type','').split(';')[0].strip().lower()!=content_type: raise routes.invalid('Unsupported Content-Type')
        previous=h.connection.gettimeout()
        try:
            h.connection.settimeout(3); value=h.rfile.read(length)
            if len(value)!=length: raise routes.invalid('Incomplete request body')
            return value
        finally: h.connection.settimeout(previous)

    def _dispatch(self,method,path):
        if not path.startswith(routes.PREFIX): return False
        h=self.h
        try:
            parsed=urlsplit(path)
            if parsed.path!=routes.PREFIX and not parsed.path.startswith(routes.PREFIX+'/'): return False
            if parsed.scheme or parsed.netloc or parsed.fragment or '#' in path: raise routes.invalid()
            for name in ('Authorization','Cookie','Host','Origin','Content-Length','Content-Type','If-Match','Idempotency-Key','X-Waveform-Reason','X-Content-SHA256'):
                if len(h.headers.get_all(name,[]))>1: raise routes.invalid('Duplicate request header')
            if h.headers.get_all('Transfer-Encoding'): raise routes.invalid('Transfer-Encoding is unsupported')
            user=h.server.auth.require(h._token(),'waveform.read')
            action,ids=routes.route(method,parsed.path)
            if not required_permissions(action)<=user.permissions: raise PermissionDenied()
            runtime=h.server.waveform_runtime; self._security(method,runtime)
            if method!='POST' and h.headers.get('Content-Length','0')!='0': raise routes.invalid('Read requests cannot have a body')
            if method=='POST' and ('?' in path): raise routes.invalid('Write requests cannot have query parameters')
            filters=routes.query(parsed.query,action) if method=='GET' else {}
            if runtime.status!='ready': raise WaveformError(runtime.code or 'waveform.unavailable','Waveform management disabled or unavailable')
            actor=Actor('platform-local',str(user.id),user.username,user.role_code)
            if action.startswith('export.') and method=='GET': self._stream(runtime,action,ids,actor); return True
            compound=action in {'evaluation.create','pair.create','pair.review','pair.preview','dataset.freeze','export.create'}
            if action=='dataset' and filters.get('view','summary')=='summary' and 'revision' not in filters:
                # Only current frozen metadata needs the guarded nested reader.
                # Pin both parts from one snapshot: a concurrent freeze must not
                # combine a new draft with an old version or grow admission.
                selection=runtime.queries.dataset_selection(ids['id'],filters.get('version_id'))
                filters['_draft_revision']=selection['revision']
                filters['_version_id']=selection['version_id']
                compound=filters['_version_id'] is not None
            with runtime.connections.reserve(2 if compound else 1):
                if method=='POST': result=self._post(runtime,action,ids,actor)
                elif action=='fact.raw':
                    payload,meta=runtime.queries.raw(ids['id'],actor); result=None
                    binary=(payload,'application/octet-stream',str(ids['id'])+'.bin',meta['payload_sha256'])
                elif action=='attachment.raw':
                    item=runtime.services['evidence'].download_attachment(ids['id'],actor=actor); result=None
                    binary=(item['payload'],item['media_type'],str(ids['id']),item['sha256'])
                else: result=self._get(runtime,action,ids,filters,actor)
            if action in ('fact.raw','attachment.raw'):
                payload,kind,name,sha=binary
                self._headers(200,len(payload),kind,(('Content-Disposition',f'attachment; filename="{name}"'),('X-Content-SHA256',sha))); self._write(payload)
            else: self._json(200,dict(ok=True,data=result))
        except Exception as error:
            h.close_connection=True
            if self.started: return True
            if isinstance(error,PermissionDenied): status,code,message=403,'forbidden','Permission denied'
            elif isinstance(error,AuthError): status,code,message=401,'unauthenticated','Authentication required'
            elif isinstance(error,WaveformError): status,code,message=error.status,error.code,error.message
            elif isinstance(error,ContractError): status,code,message=422,'waveform.request_invalid','Invalid waveform input'
            else: status,code,message=503,'waveform.unavailable','Waveform operation unavailable; no durable success confirmed'
            try: self._json(status,h._error(code,message))
            except (OSError,ValueError): pass
        return True

    def _get(self,rt,action,ids,f,actor):
        id=ids.get('id'); s=rt.services
        if action=='window': raise WaveformError('waveform.window_not_ready','Engineering windows are not implemented yet')
        if action=='import.get': return s['imports'].get(id)
        if action=='evidence.get': return s['evidence'].get(id)
        if action=='pair': return s['pairs'].get(id)
        if action=='pair.preview': return s['pairs'].inspect(id,actor=actor)
        if action=='dataset' and f.get('view','summary')=='summary':
            if set(f)-{'view','revision','version_id','_version_id','_draft_revision'} or 'revision' in f and 'version_id' in f: raise routes.invalid()
            draft=s['datasets'].get_draft(id,revision=f.get('revision',f.get('_draft_revision')))
            version=f.get('_version_id')
            return dict(draft=draft,version=s['datasets'].get(version) if version else None)
        return rt.queries.fetch(action,f|ids)

    def _post(self,rt,action,ids,actor):
        h=self.h; id=ids.get('id'); s=rt.services
        if action in ('import.append','attachment.upload'):
            kind='application/octet-stream' if action=='import.append' else h.headers.get('Content-Type','')
            if action=='attachment.upload' and kind not in ('application/pdf','image/png','image/jpeg'): raise routes.invalid()
            payload=self._body(262144 if action=='import.append' else 4194304,kind)
            reason=h.headers.get('X-Waveform-Reason','')
            if len(reason)>6144 or re.search(r'%(?![0-9a-fA-F]{2})',reason): raise routes.invalid()
            try: reason=unquote_to_bytes(reason).decode('utf-8')
            except UnicodeError: raise routes.invalid() from None
            command=Command(actor,routes.identifier(h.headers.get('Idempotency-Key')),routes.decimal(h.headers.get('If-Match')),reason,'0'*64)
            if action=='import.append':
                command=replace(command,request_hash=import_request_hash('append',id,command,line=ids['line'],payload=payload))
                return s['imports'].append(id,ids['line'],payload,command)
            sha=hashlib.sha256(payload).hexdigest()
            if h.headers.get('X-Content-SHA256')!=sha: raise routes.invalid('Attachment digest mismatch')
            command=replace(command,request_hash=evidence_request_hash('upload',None,command,attachment=dict(media_type=kind,payload_bytes=len(payload),payload_sha256=sha)))
            return s['evidence'].upload_attachment(payload,media_type=kind,command=command)
        value=strict_json(self._body(65536,'application/json')); require_fields(value,('idempotency_key','expected_version','reason','body'))
        command=Command(actor,routes.identifier(value['idempotency_key']),routes.decimal(value['expected_version']),value['reason'],'0'*64)
        body=value['body']
        if type(body) is not dict: raise routes.invalid()
        if action.startswith('import.'):
            op=action.split('.')[1]
            if op!='create': require_fields(body,())
            command=replace(command,request_hash=import_request_hash(op,id,command,metadata=body if op=='create' else None))
            return s['imports'].create(body,command) if op=='create' else getattr(s['imports'],op)(id,command)
        if action.startswith('evidence.'):
            op=action.split('.')[1]; kwargs={}
            if op=='create': require_fields(body,('kind','body')); kwargs=body
            elif op=='review': require_fields(body,('decision',)); kwargs=body
            else: require_fields(body,())
            command=replace(command,request_hash=evidence_request_hash(op,id,command,**kwargs))
            if op=='create': return s['evidence'].create(body['kind'],body['body'],command)
            if op=='review': return s['evidence'].review(id,decision=body['decision'],command=command)
            return s['evidence'].revoke(id,command)
        if action=='evaluation.create':
            key=rt.queries.capture_key(id); command=replace(command,request_hash=quality_request_hash(key,command,body=body))
            return s['quality'].record(key,body,command)
        if action.startswith('pair.'):
            op='propose' if action=='pair.create' else 'review'
            if op=='review': require_fields(body,('decision',))
            command=replace(command,request_hash=pair_request_hash(op,id,command,**({'body':body} if op=='propose' else {'decision':body['decision']})))
            return s['pairs'].propose(body,command) if op=='propose' else s['pairs'].review(id,decision=body['decision'],command=command)
        if action.startswith('dataset.'):
            op=action.split('.')[1]
            if op!='create':
                require_fields(body,('changes',) if op=='update' else ()); body=dict(target_id=str(id),**body)
            command=replace(command,request_hash=dataset_request_hash(op,id,command,body=body))
            return getattr(s['datasets'],op)(body,command)
        if action=='export.create':
            require_fields(body,('purpose',)); body=dict(version_id=str(id),**body)
            command=replace(command,request_hash=export_request_hash(id,command,body=body))
            result=s['exports'].create(body,command); current=s['exports'].describe(id,purpose=body['purpose'],actor=actor)
            if not current.allowed: raise WaveformError('waveform.export_denied','Export intent is not currently allowed',409)
            return dict(export=result,description=current)
        raise routes.invalid()

    def _stream(self,rt,action,ids,actor):
        context=iterator=None; registration=(None,self.h.connection); deadline=time.monotonic()+120
        with rt.lock:
            if rt.connections.stopped: raise WaveformError('waveform.stopping','Waveform service is stopping')
            rt.streams.add(registration)
        try:
            with rt.connections.reserve(2):
                context=rt.services['exports'].open(ids['id'],actor=actor)
                with rt.lock:
                    rt.streams.discard(registration); registration=(context,self.h.connection); rt.streams.add(registration)
                if action=='export.capture':
                    descriptor=rt.queries.export_file(ids['id'],ids['capture'],actor); key=descriptor['key']
                    iterator=context.stream_capture(CaptureKey(key['gateway_id'],key['device_id'],routes.identifier(key['capture_id'])))
                    length,sha,name=descriptor['content_length'],descriptor['file_sha256'],descriptor['filename']
                else:
                    iterator=context.stream_manifest(); meta=context.metadata
                    length,sha,name=meta.manifest_bytes,meta.dataset_content_sha256,'dataset.json'
                first=next(iterator)
            meta=context.metadata
            headers=(('Content-Disposition',f'attachment; filename="{name}"'),('X-Content-SHA256',sha),
                     ('X-Waveform-Dataset-Purpose',meta.dataset_purpose),('X-Waveform-Export-Intent',meta.intent),
                     ('X-Waveform-Restriction-State',meta.restriction_state),('X-Waveform-Restriction-Reasons',','.join(meta.restriction_reasons)))
            if sum(len(k)+len(v)+4 for k,v in headers)>16384: raise WaveformError('waveform.metadata_limit','Export headers exceed bound')
            if not re.fullmatch(r'dataset\.json|capture-[0-9]{4}\.owpwave',name): raise WaveformError('waveform.filename_invalid','Unsafe export filename')
            self._headers(200,length,'application/json' if action=='export.manifest' else 'application/octet-stream',headers)
            sent=0; piece=first
            while True:
                if sent+len(piece)>length: raise WaveformError('waveform.length_invalid','Export exceeds fixed length')
                self._write(piece,deadline); sent+=len(piece)
                if time.monotonic()>=deadline: raise TimeoutError()
                with rt.connections.reserve(1):
                    try: piece=next(iterator)
                    except StopIteration: break
            if sent!=length: raise WaveformError('waveform.length_invalid','Incomplete export')
        finally:
            try:
                if iterator is not None: iterator.close()
            finally:
                if context is not None: context.close()
                with rt.lock:
                    rt.streams.discard(registration); rt.lock.notify_all()
