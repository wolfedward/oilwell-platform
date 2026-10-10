"""Strict authenticated production API; no worker-only lifecycle routes."""
import json
import re
import time
from urllib.parse import urlsplit
from backend.auth import AuthError, PermissionDenied
from backend.ingest_reliability.config import _origin
from backend.waveforms.commands import Actor
from . import routes
from .contracts import fields, strict_json, canonical_bytes, identity, text, InversionCommand, VersionPrecondition
from .errors import InversionContractError
from .permissions import required_permissions, RUN, SessionAuthority

# Audited service refusal sites occur before their mutation commits. Never
# infer this from status alone: postcommit reads can also raise conflicts.
PRECOMMIT_ACTIONS = {
    'attachment_mismatch': frozenset(('assemblies.create','parameters.submit','parameters.review')),
    'self_review': frozenset(('parameters.review','models.review','results.review')),
    'parent_conflict': frozenset(('assemblies.create','parameters.create')),
    'review_transition': frozenset(('parameters.submit','parameters.review','parameters.revoke',
        'models.review','models.revoke','results.review','results.revoke')),
    'version_conflict': frozenset(('assemblies.create','assignments.create','parameters.create',
        'parameters.submit','parameters.review','parameters.revoke','models.create','models.review',
        'models.revoke','selections.create','selections.rollback','runs.create','runs.retry',
        'runs.cancel','results.review','results.revoke','exports.create')),
}


def noncommit_details(action,status,code):
    if status==409 and code.startswith('inversion.') and action in PRECOMMIT_ACTIONS.get(code[10:],()):
        return dict(commit_outcome='not_committed',action=action)
    return {}


class InversionApi:
    def __init__(self, handler): self.h=handler; self.started=False

    def _headers(self,status,kind,length=None):
        self.started=True
        self.h.send_response(status)
        self.h.send_header('Content-Type',kind)
        self.h.send_header('Cache-Control','no-store')
        self.h.send_header('X-Content-Type-Options','nosniff')
        if length is not None: self.h.send_header('Content-Length',str(length))
        else:
            self.h.send_header('Connection','close'); self.h.close_connection=True
        self.h.end_headers()

    def _json(self,status,value):
        payload=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        self._headers(status,'application/json; charset=utf-8',len(payload))
        if self.h.command!='HEAD': self.h.wfile.write(payload)

    def _body(self):
        length=self.h.headers.get('Content-Length','')
        if not re.fullmatch(r'0|[1-9][0-9]{0,9}',length) or not 1<=int(length)<=262144: raise InversionContractError()
        if self.h.headers.get('Content-Type','').lower() not in ('application/json','application/json; charset=utf-8'): raise InversionContractError()
        value=self.h.rfile.read(int(length))
        if len(value)!=int(length): raise InversionContractError()
        return strict_json(value)

    def _security(self, runtime):
        h=self.h
        for name in ('Authorization','Cookie','Host','Origin','Content-Length','Content-Type'):
            if len(h.headers.get_all(name,[]))>1: raise InversionContractError()
        if h.headers.get_all('Transfer-Encoding'): raise InversionContractError()
        try:
            if _origin('http://'+h.headers.get('Host','')) not in runtime.allowed_origins: raise ValueError()
            origin=h.headers.get('Origin')
            if origin is not None and _origin(origin) not in runtime.allowed_origins: raise ValueError()
            if h.command=='POST' and origin is None and not h.headers.get('Authorization','').startswith('Bearer '): raise ValueError()
        except ValueError: raise PermissionDenied('Host or Origin refused') from None

    def dispatch(self):
        h=self.h; path=h.path
        # Raw URI recognition precedes urlsplit normalization. Absolute-form
        # requests targeting this namespace are refused, never routed elsewhere.
        try: parsed=urlsplit(path)
        except ValueError:
            if routes.PREFIX not in path: return False
            parsed=None
        if parsed is not None and not (path.startswith(routes.PREFIX) or parsed.path.startswith(routes.PREFIX)): return False
        previous=h.connection.gettimeout(); action=None
        try:
            h.connection.settimeout(3)
            if parsed is None or not path.startswith(routes.PREFIX) or parsed.scheme or parsed.netloc or parsed.fragment or '#' in path or re.search(r'[\x00-\x20\x7f]',path): raise InversionContractError()
            runtime=h.server.inversion_runtime
            self._security(runtime)
            action, identifier=routes.route(h.command,parsed.path)
            if h.command!='POST' and h.headers.get('Content-Length','0')!='0': raise InversionContractError()
            if h.command=='POST' and '?' in path: raise InversionContractError()
            filters=routes.query(parsed.query,action)
            # Capture uses the same existing identity file read-only. Every
            # service callback checks this request's session anew.
            permissions=required_permissions(filters['action'] if action=='requests.get' else action)
            if action=='results.get' and filters.get('include_input_metadata'): permissions |= {'waveform.read'}
            if action=='requests.get' and filters.get('intent')=='evidence': permissions |= {'inversion.audit.read'}
            ref=h.server.auth.capture_background_authority(h._token(),permissions)
            user=h.server.auth.refresh_background_authority(ref,permissions)
            actor=Actor('platform-local',str(user.id),user.username,user.role_code)
            value=self._body() if h.command=='POST' else None
            command,body=self._envelope(action,value,actor) if value is not None else (None,None)
            if action=='health':
                data=runtime.queries.health() if runtime.status=='ready' else dict(status=runtime.status,code=runtime.code,accounting_scope='run_input_and_results',field_accuracy_certified=False)
                data['missing_run_permissions']=sorted(RUN-user.permissions)
                self._json(200,dict(ok=True,data=data)); return True
            # Parse fixed run body even when disabled, so malformed wire never
            # masquerades as an operational failure.
            if action in ('runs.create','runs.preflight'):
                from .input_contracts import parse_run_request
                body=parse_run_request(canonical_bytes(body))
            if runtime.status!='ready' or runtime.stopped:
                self._json(503,dict(code=runtime.code or 'inversion.unavailable',message='Inversion service unavailable',details={})); return True
            if action=='runs.preflight':
                self._json(200,dict(ok=True,data=runtime.queries.preflight(body))); return True
            if action=='requests.get':
                result=runtime.queries.recover(identifier,filters['action'],actor,intent=filters.get('intent'))
                if result['state']=='committed' and filters['action'].startswith('runs.') and 'inversion.read' in user.permissions:
                    # The projection has its own fresh read check, not an old grant snapshot.
                    h.server.auth.refresh_background_authority(ref,frozenset({'inversion.read'}))
                    from .queries import run_view
                    from uuid import UUID
                    run=runtime.services_for(ref)['runs'].get(UUID(result['subject_id']))
                    result['run']=run_view(run,runtime.queries.result_id(run.id))
                self._json(200,dict(ok=True,data=result)); return True
            services=runtime.services_for(ref)
            if action=='exports.get': self._stream(runtime,services['exports'],identifier,actor); return True
            if h.command=='POST':
                result,status=self._post(action,identifier,body,command,ref,user,services,runtime)
            else: result,status=self._get(action,identifier,filters,actor,services,runtime),200
            self._json(status,dict(ok=True,data=result))
        except Exception as error:
            h.close_connection=True
            if self.started: return True
            if isinstance(error,PermissionDenied): status,code,message=403,'forbidden','Permission denied'
            elif isinstance(error,AuthError): status,code,message=401,'unauthenticated','Authentication required'
            elif isinstance(error,InversionContractError): status,code,message=400,'inversion.contract_invalid','Invalid inversion request'
            elif isinstance(error,routes.RouteError): status,code,message=error.status,'inversion.route_refused','Unknown route or unsupported method'
            else:
                from .transactions import InversionError
                if isinstance(error,InversionError):
                    status=503 if error.code in ('inversion.capacity_exhausted','inversion.slot_exhausted') else error.status
                    code,message=error.code,'Inversion operation refused'
                else: status,code,message=503,'inversion.unavailable','Inversion operation unavailable; no durable success confirmed'
            try: self._json(status,dict(code=code,message=message,details=noncommit_details(action,status,code)))
            except (OSError,ValueError): pass
        finally: h.connection.settimeout(previous)
        return True

    @staticmethod
    def _envelope(action,value,actor):
        has_body=action in ('assemblies.create','assignments.create','parameters.create','parameters.review','models.create','models.review','selections.create','runs.create','runs.preflight','results.review','exports.create')
        if action=='runs.preflight': fields(value,('body',)); return None,value['body']
        fields(value,('command','body') if has_body else ('command',))
        raw=value['command']; fields(raw,('key','expected','reason'))
        expected=raw['expected']
        if type(expected) is not dict: raise InversionContractError()
        fields(expected,('kind',) if expected.get('kind')=='absent' else ('kind','version'))
        command=InversionCommand(actor,identity(raw['key']),VersionPrecondition(**expected),raw['reason'])
        body=value.get('body')
        if has_body and type(body) is not dict: raise InversionContractError()
        if action.endswith('.review'):
            fields(body,('decision',))
            if body['decision'] not in ('approve','reject'): raise InversionContractError()
        if action=='models.create':
            fields(body,('installed_id',)); text(body['installed_id'],maximum=128)
        if action=='exports.create':
            fields(body,('result_id','intent','format')); identity(body['result_id'])
            if body['intent'] not in ('quantitative','evidence') or body['format'] not in ('json','csv'): raise InversionContractError()
        return command,body

    def _get(self,action,id,filters,actor,services,runtime):
        from .queries import governance_view, result_view, point_page, run_view, public_value, wire
        if action=='parameters.get':
            from .transactions import InversionError
            try: return governance_view(services['parameters'].get(id))
            except InversionError as error:
                # Public resource absence is not an internal dependency conflict.
                if error.code=='inversion.parameter_missing':
                    raise InversionError('parameter_missing',status=404) from None
                raise
        if action=='runs.get':
            run=services['runs'].get(id)
            return run_view(run,runtime.queries.result_id(id)) | {'terminal_diagnostics':runtime.queries.run_diagnostics(run)}
        if action.startswith('results.'):
            view=services['results'].get(id,actor=actor,**({'include_input_metadata':True} if action=='results.get' and filters.get('include_input_metadata') else {}))
            if action=='results.points': return point_page(view,**filters)
            if action=='results.comparison': return dict(result_id=str(view.id),content_sha256=view.content_sha256,**{k:public_value(wire(view.body.get(k))) for k in ('comparison','filled_comparison')})
            return result_view(view)
        return runtime.queries.fetch(action,id,**filters)

    def _post(self,action,id,body,command,ref,user,s,runtime):
        from .queries import wire, run_view, public_value
        if action in ('runs.cancel','runs.retry'):
            if action=='runs.cancel':
                user=self.h.server.auth.refresh_background_authority(ref,frozenset({'inversion.run'}))
                run=s['runs'].get(id)
                if (run.actor.domain,run.actor.user_id)!=(command.actor.domain,command.actor.user_id) and user.role_code!='administrator': raise PermissionDenied()
                result=s['runs'].cancel(id,command)
            else: result=s['runs'].retry(id,command,ref)
            return run_view(result,runtime.queries.result_id(result.id)),202 if action=='runs.retry' else 200
        if action=='runs.create': return run_view(s['runs'].create(body,command,ref)),202
        if action=='assemblies.create': result=s['parameters'].create_assembly(body,command)
        elif action=='assignments.create': result=s['parameters'].assign(body,command)
        elif action=='parameters.create': result=s['parameters'].create_version(body,command)
        elif action=='models.create': result=s['models'].register(body['installed_id'],command)
        elif action=='selections.create': result=s['selections'].select(body,command)
        elif action=='selections.rollback': result=s['selections'].rollback(id,command)
        elif action=='exports.create':
            result=s['exports'].create(identity(body['result_id']),body['intent'],body['format'],command)
            return dict(id=str(result.id),result_id=str(result.result_id),result_sha256=result.result_sha256,intent=result.intent,format=result.format,manifest=public_value(wire(result.manifest)),manifest_sha256=result.manifest_sha256),201
        else:
            group,operation=action.split('.')
            if operation=='review': result=s[group].review(id,body['decision'],command)
            elif operation=='submit': result=s[group].submit(id,command)
            elif operation=='revoke': result=s[group].revoke(id,command)
            else: raise InversionContractError()
        return wire(result),200 if result.replayed or not action.endswith('.create') else 201

    def _stream(self,runtime,exporter,id,actor):
        context=iterator=None; registration=None
        try:
            context=exporter.open(id,actor=actor)
            with runtime.lock:
                if runtime.stopped: raise ConnectionAbortedError()
                registration=(context,self.h.connection); runtime.streams.add(registration)
            iterator=iter(context)
            first=next(iterator)  # durable access audit and fresh identity BEFORE 200
            self._headers(200,'application/json' if context.format=='json' else 'text/csv; charset=utf-8')
            deadline=time.monotonic()+180
            self.h.wfile.write(first)
            for chunk in iterator:
                if runtime.stopped or time.monotonic()>=deadline: raise TimeoutError()
                self.h.wfile.write(chunk)
        finally:
            try:
                if iterator is not None: iterator.close()
            finally:
                if context is not None: context.close()
                if registration is not None:
                    with runtime.lock: runtime.streams.discard(registration)
