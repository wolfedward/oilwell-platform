"""Finite route/query grammar. No database access or caller-defined SQL."""
import re
from urllib.parse import parse_qsl
from uuid import UUID
from .contracts import require_name
from .errors import WaveformError,ContractError

PREFIX='/api/waveforms'
PAGE={'page','page_size','snapshot_revision','received_before','received_after'}
FILTERS={
    'captures':PAGE|{'gateway_id','device_id','state','stalled','well_id'},
    'capture':PAGE|{'view','status','reason_code','transport','source_ref'},
    'evidence':PAGE|{'kind','state'},'evaluations':PAGE|{'status','purpose'},
    'pairs':PAGE|{'state'},'datasets':PAGE|{'state','purpose'},
    'dataset':PAGE|{'view','revision','version_id'},
    'audit':PAGE|{'family','action','actor_domain','actor_id','subject_id'},
    'window':{'start','count','mode','budget'},
}

def invalid(message='Invalid waveform request'):
    return WaveformError('waveform.request_invalid',message,422)

def identifier(value):
    try:
        result=UUID(value)
        if str(result)!=value: raise ValueError()
        return result
    except (ValueError,TypeError,AttributeError): raise invalid('Canonical UUID required') from None

def decimal(value,maximum=9223372036854775807,minimum=0,signed=False):
    if type(value) is not str or len(value)>20 or not re.fullmatch(r'(?:0|-?[1-9][0-9]*)' if signed else r'(?:0|[1-9][0-9]*)',value): raise invalid('Canonical decimal required')
    number=int(value)
    if not minimum<=number<=maximum: raise invalid('Number outside supported range')
    return number

def route(method,path,*,_checking=False):
    if '%' in path or '\\' in path: raise invalid('Encoded paths are not accepted')
    p=path[len(PREFIX):].split('/')[1:]; ids={}
    if any(not s for s in p): raise WaveformError('waveform.not_found','Unknown route',404)
    action=None
    if len(p)==1:
        action=({'health':'health','captures':'captures','evidence':'evidence','pairs':'pairs','datasets':'datasets','audit':'audit'} if method=='GET' else
                {'imports':'import.create','attachments':'attachment.upload','evidence':'evidence.create','pairs':'pair.create','datasets':'dataset.create'} if method=='POST' else {}).get(p[0])
    elif len(p)>=2:
        ids['id']=identifier(p[1])
        if len(p)==2 and method=='GET': action={'captures':'capture','imports':'import.get','evidence':'evidence.get','pairs':'pair','datasets':'dataset'}.get(p[0])
        if len(p)==3:
            key=(p[0],p[2])
            action=({('facts','raw'):'fact.raw',('attachments','raw'):'attachment.raw',('captures','evaluations'):'evaluations',('pairs','preview'):'pair.preview',('exports','manifest'):'export.manifest'} if method=='GET' else
                    {('imports','seal'):'import.seal',('imports','cancel'):'import.cancel',('evidence','review'):'evidence.review',('evidence','revoke'):'evidence.revoke',('captures','evaluations'):'evaluation.create',('pairs','review'):'pair.review',('datasets','update'):'dataset.update',('datasets','submit'):'dataset.submit',('datasets','freeze'):'dataset.freeze',('dataset-versions','exports'):'export.create'} if method=='POST' else {}).get(key)
        if len(p)==4 and p[0]=='imports' and p[2]=='lines' and method=='POST':
            ids['line']=decimal(p[3],512); action='import.append'
        if len(p)==5 and method=='GET':
            if p[0]=='captures' and p[2]=='streams' and p[4]=='window':
                try: require_name(p[3])
                except ContractError: raise invalid() from None
                ids['stream']=p[3]; action='window'
            if p[0]=='exports' and p[2]=='captures' and p[4]=='file': ids['capture']=identifier(p[3]); action='export.capture'
    if action is None:
        if not _checking:
            for candidate in ('GET','POST'):
                try: route(candidate,path,_checking=True)
                except WaveformError: continue
                raise WaveformError('waveform.method_not_allowed','Unsupported method for this resource',405)
        raise WaveformError('waveform.not_found','Unknown route',404)
    return action,ids

def query(source,action):
    try:
        if len(source)>8192 or re.search(r'%(?![0-9a-fA-F]{2})',source): raise ValueError()
        pairs=parse_qsl(source,keep_blank_values=True,strict_parsing=True,encoding='utf-8',errors='strict',max_num_fields=20)
        if len(dict(pairs))!=len(pairs) or set(dict(pairs))-FILTERS.get(action,set()): raise ValueError()
        out=dict(pairs)
    except (ValueError,UnicodeError): raise invalid('Unknown, duplicate or malformed query') from None
    for key in ('page','page_size','revision'):
        if key in out: out[key]=decimal(out[key],{'page':100001,'page_size':200,'revision':9223372036854775807}[key],1)
    if (out.get('page',1)-1)*out.get('page_size',50)>100000: raise invalid('Page offset too large')
    for key in ('well_id','source_ref','version_id','subject_id'):
        if key in out: out[key]=identifier(out[key])
    for key in ('gateway_id','device_id'):
        if key in out:
            try: require_name(out[key])
            except ContractError: raise invalid() from None
    if action=='window':
        if not {'start','count','mode'}<=out.keys() or out['mode'] not in ('raw','summary'): raise invalid()
        out['start']=decimal(out['start'],minimum=-9223372036854775808,signed=True)
        out['count']=decimal(out['count'],20000 if out['mode']=='raw' else 2147483647,1)
        if out['mode']=='summary': out['budget']=decimal(out.get('budget','2000'),2000,1)
        elif 'budget' in out: raise invalid()
    return out
