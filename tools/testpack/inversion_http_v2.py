"""Bounded client for the existing inversion protocol, not a test approval bypass."""
from dataclasses import dataclass
from collections.abc import Mapping
import http.client
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from backend.inversion.contracts import canonical_bytes, freeze, identity, VersionPrecondition
from backend.inversion.result_codec import POINT_COLUMNS
from backend.inversion.result_codec_v2 import PROOF_COLUMNS
from backend.inversion.routes import PREFIX, route
from .lab_http import LabHttpError
from .storage import safe_path


@dataclass(frozen=True)
class InversionOutcome:
    case_id: str
    assertion_status: str
    expected_outcome: str
    observed_outcome: str
    phase: str
    reason_code: str
    approved_execution_status: str
    facts: Mapping

    def __post_init__(self):
        if (not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}',self.case_id)
            or self.assertion_status not in ('passed','failed','blocked')
            or self.approved_execution_status not in ('passed','refused','not_run','failed')
            or (self.approved_execution_status=='passed' and
                (self.expected_outcome!='completed' or self.observed_outcome!='completed' or self.assertion_status!='passed'))
            or not isinstance(self.facts,Mapping)):
            raise ValueError('invalid assertion/execution classification')
        for value in (self.expected_outcome,self.observed_outcome,self.phase,self.reason_code):
            if type(value) is not str or len(value)>128:raise ValueError('bounded outcome required')
        object.__setattr__(self,'facts',freeze(dict(self.facts)))


class InversionSession:
    def __init__(self,http,*,journal_root=None):
        self.http=http
        self.journal_root=None if journal_root is None else safe_path(Path(journal_root))
        if self.journal_root is not None and not self.journal_root.is_dir():raise ValueError('journal directory required')

    def get(self,path,*,actor='algorithm_engineer'):
        return self.http.request('GET',PREFIX+path,actor=actor)['data']

    def post(self,path,body=None,*,actor,expected_version,key,expected_status=200):
        """A transport-unknown write is replayed at most once, with identical bytes/key."""
        action,_=route('POST',PREFIX+path)
        if action=='runs.preflight':raise ValueError('preflight is not a command')
        identity(key)
        expected={'kind':'absent'} if expected_version is None else {'kind':'match','version':expected_version}
        VersionPrecondition(**expected)
        value={'command':{'key':key,'expected':expected,
            'reason':'Synthetic software workflow verification; not human sign-off or field certification'}}
        if body is not None:value['body']=body
        # Freeze a caller-owned mutable body before the first transmission.
        from backend.inversion.contracts import strict_json
        value=strict_json(canonical_bytes(value))
        if self.journal_root is not None:
            target=safe_path(self.journal_root/(key+'.json'),missing=True)
            intent=canonical_bytes(dict(action=action,actor=actor,path=path,envelope=value,expected_status=expected_status))
            if len(intent)>1048576:raise ValueError('command intent too large')
            if target.exists():
                if target.stat().st_size!=len(intent) or target.read_bytes()!=intent:
                    raise ValueError('command key intent changed')
            else:
                with target.open('xb') as stream:
                    stream.write(intent);stream.flush();os.fsync(stream.fileno())
        try:
            return self.http.request('POST',PREFIX+path,value,actor=actor,expected=expected_status)['data']
        except (OSError,http.client.HTTPException,LabHttpError) as error:
            if isinstance(error,LabHttpError) and (error.status,error.code)!=(503,'inversion.unavailable'):raise
            query={'action':action}
            if action=='exports.create':query['intent']=value['body']['intent']
            recovered=self.get('/requests/'+key+'?'+urlencode(query),actor=actor)
            if recovered.get('state') not in ('unknown','committed'):
                raise ValueError('unrecognized command recovery state')
            # Recovery is actor/action scoped. The original write returns its
            # exact receipt on replay; never invent a new resource from GET data.
            return self.http.request('POST',PREFIX+path,value,actor=actor,
                                     expected=tuple(sorted({200,expected_status})))['data']

    def preflight(self,body,*,actor='algorithm_engineer'):
        return self.http.request('POST',PREFIX+'/runs/preflight',{'body':body},actor=actor)['data']

    def read_all_points(self,result_id,*,kind='prediction',actor='algorithm_engineer'):
        identity(result_id)
        columns={'prediction':POINT_COLUMNS,'proof':PROOF_COLUMNS}.get(kind)
        if columns is None:raise ValueError('native prediction or proof required')
        metadata=self.get('/results/'+result_id,actor=actor)
        digest=metadata.get('content_sha256')
        if metadata.get('id')!=result_id or type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError('result identity unavailable')
        output=[];total=None;previous_time=None
        while total is None or len(output)<total:
            offset=len(output)
            page=self.get(f'/results/{result_id}/points?'+urlencode({'kind':kind,'offset':offset,'limit':1000}),actor=actor)
            count=page.get('total')
            if (page.get('result_id')!=result_id or page.get('content_sha256')!=digest or
                type(page.get('offset')) is not int or page['offset']!=offset or page.get('limit')!=1000 or
                type(count) is not int or not 1<=count<=32768 or total is not None and count!=total):
                raise ValueError('page identity changed')
            total=count; rows=page.get('rows')
            if type(rows) is not list or len(rows)!=min(1000,total-offset):raise ValueError('truncated page')
            for row in rows:
                if (type(row) is not dict or set(row)!={'kind','columns','values'} or row['kind']!=kind or
                    row['columns']!=list(columns) or type(row['values']) is not list or len(row['values'])!=len(columns)):
                    raise ValueError('unknown native row shape')
                point=dict(zip(columns,row['values']))
                if point['power_index']!=str(len(output)):raise ValueError('duplicate or missing native index')
                from fractions import Fraction
                instant=Fraction(point['time_ns'])
                if previous_time is not None and instant<=previous_time:raise ValueError('nonmonotone native time')
                previous_time=instant;output.append(point)
        return tuple(output)

    def wait_run(self,run_id,*,deadline,actor='algorithm_engineer'):
        identity(run_id)
        if not time.monotonic()<deadline<=time.monotonic()+240:raise ValueError('bounded run deadline required')
        while True:
            current=self.get('/runs/'+run_id,actor=actor)
            if current.get('run_id')!=run_id:raise ValueError('run identity changed')
            if current['state'] not in ('queued','running'):return current
            if time.monotonic()>=deadline:raise TimeoutError('owned run did not become terminal')
            time.sleep(.25)

    def download(self,export_id,*,actor='algorithm_engineer'):
        identity(export_id)
        return self.http.request('GET',PREFIX+'/exports/'+export_id,actor=actor,raw=True)
