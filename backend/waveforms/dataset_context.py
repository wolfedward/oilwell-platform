"""Compact joint certificates and sequential audited file preparation.

No decoded capture buffers survive a raw chunk. Guarded methods only reread
mutable metadata and fresh registry projections, never raw bytes or sample scans.
"""
from collections import defaultdict
from fractions import Fraction
from hashlib import sha256
import json
from uuid import UUID

from .capacity import row
from .contracts import CaptureKey,ManifestFrame,ChunkFrame
from .dataset_contracts import fail,snapshot_hash,reason_codes
from .evidence_contracts import canonical_bytes,thaw
from .pair_contracts import capture_key,key_dict,pair_content,public,bounded
from .quality_context import ContextBuilder
from .registry_context import RegistryContext
from .samples import iter_samples
from .wire import parse_frame


def applicable(snapshot):
    return {k:v for k,v in snapshot.items() if k!='revision'} if snapshot else None


class DatasetContext(ContextBuilder):
    def certificate(self,cur,member,*,quality_rows=None):
        pair=None; review=None
        if member['kind']=='pair':
            cur.execute('SELECT * FROM waveforms.pair_versions WHERE pair_version_id=%s',(UUID(member['reference_id']),)); pair=row(cur)
            if pair is None: fail('member_missing','Pair does not exist')
            ids=list(dict.fromkeys((pair['left_quality_run_id'],pair['right_quality_run_id'])))
            if UUID(member['quality_run_id']) not in ids or pair_content(pair['body'],pair['method_version'])!=pair['content_sha256']: fail('member_identity','Pair identity differs')
            cur.execute('SELECT version,decision,actor FROM waveforms.pair_reviews WHERE pair_version_id=%s ORDER BY version DESC LIMIT 1',(pair['pair_version_id'],)); review=row(cur)
        else: ids=[UUID(member['quality_run_id'])]
        runs=[]
        for id in ids:
            if quality_rows is None:
                cur.execute('SELECT * FROM waveforms.quality_runs WHERE quality_run_id=%s',(id,)); run=row(cur)
            else: run=quality_rows.get(id)
            if run is None: fail('member_missing','Quality certificate does not exist')
            runs.append(run)
        first=runs[0]; selection=first['parameters']['selection']
        if member['kind']=='capture' and str(first['capture_pk'])!=member['reference_id']: fail('member_identity','Quality anchor differs')
        for run in runs:
            if any(run[k]!=first[k] for k in ('parameters','result','evidence','registry_snapshot','purpose','status')): fail('joint_identity','Joint certificates disagree')
        if pair:
            body=pair['body']
            if (body['selection']!=selection or body['selection_sha256']!=first['parameters']['selection_sha256']
                or body['joint_result']!=first['result'] or body['physical_cycles']!=first['parameters']['physical_cycles']
                or body['quality_run_ids']!=[str(pair['left_quality_run_id']),str(pair['right_quality_run_id'])]
                or {capture_key(r['source_key']) for r in runs}!={capture_key(c['key']) for c in selection['captures']}): fail('joint_identity','Pair does not bind full joint certificates')
        pins=[]
        for run in sorted(runs,key=lambda r:str(r['quality_run_id'])):
            pins.append(dict(id=str(run['quality_run_id']),source_key=run['source_key'],manifest_sha256=run['manifest_sha256'],
                profile_id=str(run['profile_id']) if run['profile_id'] else None,profile_sha256=run['profile_sha256'],purpose=run['purpose'],status=run['status'],
                validator_version=run['validator_version'],selection_sha256=run['parameters']['selection_sha256'],
                parameters_sha256=snapshot_hash('parameters',run['parameters']),result_sha256=snapshot_hash('result',run['result']),
                evidence_sha256=snapshot_hash('evidence',run['evidence']),registry_sha256=snapshot_hash('registry',run['registry_snapshot']) if run['registry_snapshot'] else None))
        refs=self.references(selection)
        pinned={e['id']:e for e in first['evidence']}
        for ref in refs:
            actual=pinned.get(ref['id'])
            if actual is None or any(actual[k]!=ref[k] for k in ('id','content_sha256','version')): fail('joint_identity','Joint evidence pin differs')
        pair_pin=None if pair is None else dict(id=str(pair['pair_version_id']),content_sha256=pair['content_sha256'],method_version=pair['method_version'],
            review_version=str(review['version']) if review else '1',review_decision=review['decision'] if review else 'proposed',review_actor=review['actor'] if review else None,
            left_quality_run_id=str(pair['left_quality_run_id']),right_quality_run_id=str(pair['right_quality_run_id']))
        snapshot=dict(pair=pair_pin,quality=pins,evidence=sorted(first['evidence'],key=lambda e:e['id']),
                      registry=dict(historical=first['registry_snapshot'],freeze=None),physical_cycles=first['parameters']['physical_cycles'],payload_fingerprints=[],restrictions=[])
        return dict(member=dict(member),selection=selection,refs=refs,snapshot=snapshot,status=first['status'],purpose=first['purpose'],historical_reasons=first['result']['reasons'])

    def registry_now(self,certificate):
        old=certificate['snapshot']['registry']['historical']
        if not old or not old['sources']: return old
        sources=tuple(dict.fromkeys(capture_key(source['capture']) for source in old['sources']))
        return public(RegistryContext(self._read_connect).resolve(sources,utc_start=old['utc_start'],start_ns=Fraction(old['start_ns']),
            end_ns=Fraction(old['end_ns']),uncertainty_ns=Fraction(old['uncertainty_ns']) if old['uncertainty_ns'] is not None else None,revision=None))

    def current(self,cur,certificate,captures,*,registry,metadata=None):
        reasons=[]; snap=certificate['snapshot']; member=certificate['member']
        if certificate['status']!='eligible': reasons.append('quality_'+certificate['status'])
        if member['kind']=='pair':
            cur.execute('SELECT version,decision FROM waveforms.pair_reviews WHERE pair_version_id=%s ORDER BY version DESC LIMIT 1',(UUID(member['reference_id']),))
            review=cur.fetchone(); pin=snap['pair']
            if not review or review[1]!='approve': reasons.append('pair_not_approved')
            if review and str(review[0])!=pin['review_version']: reasons.append('pair_review_changed')
        for ref in certificate['refs']:
            evidence=self._get(cur,UUID(ref['id']))
            if evidence.content_sha256!=ref['content_sha256']: fail('member_identity','Immutable evidence digest differs')
            if evidence.version!=int(ref['version']): reasons.append('evidence_changed')
            if evidence.review_state!='approved': reasons.append('evidence_'+evidence.review_state)
            if evidence.kind=='origin':
                body=evidence.body; p=body['provenance']; key=capture_key(body['capture']); capture=captures.get(key)
                cur.execute('SELECT * FROM waveforms.provenance_metadata WHERE observation_id=%s AND outcome_id=%s AND fact_id=%s AND source_ref=%s',tuple(UUID(p[k]) for k in ('observation_id','outcome_id','fact_id','source_ref')))
                proof=row(cur)
                if (not proof or not capture or proof['status']!='stored' or proof['reason_code']!='accepted' or proof['kind']!='manifest'
                    or (proof['gateway_id'],proof['device_id'],str(proof['capture_id']))!=(key.gateway_id,key.device_id,str(key.capture_id))
                    or proof['payload_sha256']!=p['manifest_sha256'] or capture['manifest_sha256']!=p['manifest_sha256'] or proof['transport']!=p['transport']): reasons.append('unknown_origin')
                elif p['transport']=='file' and (str(proof['import_id'])!=p['import_id'] or proof['import_state']!='complete' or proof['line_number'] is None): reasons.append('import_not_sealed')
        for chosen in certificate['selection']['captures']:
            key=capture_key(chosen['key']); capture=self._capture(cur,key) if metadata is None else metadata.get(key)
            if not capture or capture['manifest_sha256']!=chosen['manifest_sha256']: fail('member_identity','Capture manifest changed')
            if metadata is None:
                completion=self._completion(cur,capture['capture_pk'])
                complete=completion is not None and completion['status']=='complete'
                cur.execute('SELECT EXISTS(SELECT 1 FROM waveforms.capture_restrictions WHERE subject_id=%s)',(capture['capture_pk'],))
                restricted=cur.fetchone()[0]
            else: complete,restricted=capture['completion_status']=='complete',capture['restricted']
            if capture['state']!='complete' or not complete: reasons.append('capture_not_complete')
            if restricted: reasons.append('capture_conflict')
        historical=snap['registry']['historical']
        if applicable(registry)!=applicable(historical): reasons.append('registry_applicability_changed')
        if registry is None or registry['status']=='pending': reasons.append('registry_unresolved')
        if certificate['purpose']=='field_pair_input' and (registry is None or registry['status']!='resolved'): reasons.append('registry_ineligible')
        return reason_codes(reasons)

    def lock_captures(self,cur,captures):
        ids=[c['capture_pk'] for c in captures.values()]
        if len(ids)>2000: fail('member_limit','Too many physical captures')
        cur.execute('SELECT capture_pk FROM waveforms.capture_metadata WHERE capture_pk=ANY(%s) ORDER BY gateway_id,device_id,capture_id',(ids,))
        ordered=[r[0] for r in cur.fetchall()]
        if set(ordered)!=set(ids): fail('member_missing','Capture union changed')
        for start in range(0,len(ordered),32): cur.execute('SELECT waveforms.lock_business_captures(%s)',(ordered[start:start+32],))

    def restriction_snapshot(self,cur,certificate,captures,reasons,*,expected=None):
        """Pin selected immutable events, bounded by the existing snapshot byte limit.

        Page at the SQL boundary, not merely fetchmany over an unbounded client
        result. Guarded comparison uses preparation's bounded record count and
        plain equality; JSON sizing/serialization is preparation-only.
        """
        records=[dict(reason_code=code,subject_kind='member',subject_id=certificate['member']['reference_id'],
                      restriction_id=None,source_event_id=None) for code in reasons]
        size=len(json.dumps(records,ensure_ascii=False).encode('utf-8')) if expected is None else 0
        refs=[UUID(ref['id']) for ref in certificate['refs']]
        ids=[captures[capture_key(chosen['key'])]['capture_pk'] for chosen in certificate['selection']['captures']]
        offset=0
        if reasons:
            while True:
                cur.execute("SELECT * FROM (SELECT restriction_id,evidence_id AS subject_id,'evidence' AS subject_kind,reason_code,source_event_id FROM waveforms.restrictions WHERE evidence_id=ANY(%s) UNION ALL SELECT restriction_id,subject_id,subject_kind,reason_code,source_event_id FROM waveforms.capture_restrictions WHERE subject_id=ANY(%s)) pins ORDER BY subject_kind,subject_id,restriction_id LIMIT 100 OFFSET %s",(refs,ids,offset))
                page=cur.fetchall()
                for restriction,subject,kind,code,event in page:
                    record=dict(reason_code=code,subject_kind=kind,subject_id=str(subject),restriction_id=str(restriction),source_event_id=str(event))
                    records.append(record)
                    if expected is None:
                        size+=len(json.dumps(record,ensure_ascii=False).encode('utf-8'))+2
                        if size>262144: fail('snapshot_limit','Restriction events exceed the member snapshot bound')
                    elif len(records)>len(expected): fail('stale_preparation','Restriction events changed after preparation')
                if len(page)<100: break
                offset+=100
        records.sort(key=lambda r:tuple(r[k] or '' for k in ('reason_code','subject_kind','subject_id','source_event_id','restriction_id')))
        if expected is not None and records!=expected: fail('stale_preparation','Restriction events changed after preparation')
        return records

    def recheck_dataset(self,cur,prepared,*,strict):
        self.lock_captures(cur,prepared['captures']); reasons=[]
        # Call-local metadata is read only after all existing capture locks.
        metadata=self.capture_rows(cur,prepared['captures'],current=True)
        for certificate in prepared['certificates']:
            registry=self.registry_now(certificate)
            if strict and registry!=certificate['snapshot']['registry']['freeze']: fail('stale_preparation','Registry changed after manifest preparation')
            current=self.current(cur,certificate,prepared['captures'],registry=registry,metadata=metadata)
            if strict: self.restriction_snapshot(cur,certificate,prepared['captures'],current,expected=certificate['snapshot']['restrictions'])
            reasons.extend(current)
        return reason_codes(reasons)

    def file_prepare(self,key,capture,requests,actor,*,connection):
        payload=self._raw(capture['manifest_fact_id'],actor,connection=connection); frame=parse_frame(payload)
        if type(frame) is not ManifestFrame or frame.key!=key or frame.raw_sha256!=capture['manifest_sha256']: fail('file_identity','Manifest differs')
        plan=[dict(fact_id=str(capture['manifest_fact_id']),raw_sha256=frame.raw_sha256,raw_length=len(payload))]
        digest=sha256(payload+b'\n'); length=len(payload)+1; fingerprints=[]
        slots=connection.execute('SELECT stream_id,chunk_index,fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s ORDER BY stream_id,chunk_index LIMIT 513',(capture['capture_pk'],)).fetchall()
        connection.commit()  # End the slots read before the next audited fact.
        if len(slots)>512: fail('file_limit','Too many chunks')
        slots={(s,i):id for s,i,id in slots}
        for stream in frame.streams:
            selected=[]
            for request in requests:
                if request['stream_id']!=stream.stream_id: continue
                position=next((n for n,c in enumerate(stream.channels) if c.channel_id==request['channel_id']),None)
                if position is None: fail('member_identity','Selected channel is absent')
                lo,hi=int(request['first_index']),int(request['last_index'])
                if not stream.first_sample_index<=lo<=hi<stream.first_sample_index+stream.frame_count: fail('member_identity','Selected original indices differ')
                encoding=stream.channels[position].encoding
                h=sha256(canonical_bytes(dict(protocol='waveform.dataset-payload/1',encoding=encoding,count=hi-lo+1)))
                selected.append((request,position,lo,hi,encoding,h))
            stream_hash=sha256(); size=0
            for index in range(stream.chunk_count):
                fact=slots.get((stream.stream_id,index))
                if fact is None: fail('file_identity','Sealed slot is absent')
                raw=self._raw(fact,actor,connection=connection); chunk=parse_frame(raw)
                count=min(stream.chunk_frames,stream.frame_count-index*stream.chunk_frames)
                if (type(chunk) is not ChunkFrame or chunk.key!=key or chunk.manifest_sha256!=frame.raw_sha256 or chunk.stream_id!=stream.stream_id
                    or chunk.chunk_index!=index or chunk.sample_start!=index*stream.chunk_frames or chunk.sample_count!=count): fail('file_identity','Chunk layout differs')
                for _ in iter_samples(stream,chunk.data,sample_start=chunk.sample_start): pass
                stream_hash.update(chunk.data); size+=len(chunk.data); digest.update(raw); digest.update(b'\n'); length+=len(raw)+1
                plan.append(dict(fact_id=str(fact),raw_sha256=sha256(raw).hexdigest(),raw_length=len(raw)))
                start=stream.first_sample_index+chunk.sample_start
                for request,position,lo,hi,encoding,h in selected:
                    for sample in range(max(lo,start),min(hi,start+count-1)+1):
                        offset=(sample-start)*stream.frame_bytes+position*5; h.update(chunk.data[offset:offset+5])
            if size!=stream.decoded_bytes or stream_hash.hexdigest()!=stream.data_sha256: fail('file_identity','Stream bytes differ')
            for request,position,lo,hi,encoding,h in selected:
                fingerprints.append(dict(capture=key_dict(key),stream_id=stream.stream_id,channel_id=request['channel_id'],first_index=str(lo),last_index=str(hi),encoding=encoding,count=hi-lo+1,stream_data_sha256=stream.data_sha256,content_sha256=h.hexdigest()))
        if len(plan)!=len(slots)+1: fail('file_identity','Unexpected sealed slot')
        bounded(plan,131072)
        return dict(frame=frame,plan=plan,fingerprints=fingerprints,descriptor=dict(key=key_dict(key),capture_pk=str(capture['capture_pk']),
            filename='',content_length=length,file_sha256=digest.hexdigest(),manifest_sha256=frame.raw_sha256,file_plan_sha256=snapshot_hash('file_plan',plan),source_files=[]))

    def prepare_dataset(self,members,purpose,actor):
        certificates=[]; captures={}; requests=defaultdict(dict)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            for member in members:
                certificate=self.certificate(cur,member); certificates.append(certificate)
                if member['kind']=='capture' and purpose!='raw_archive' or certificate['purpose']!=purpose: fail('purpose_mismatch','Dataset and full certificate purposes differ')
                for chosen in certificate['selection']['captures']:
                    key=capture_key(chosen['key']); capture=self._capture(cur,key)
                    if not capture or capture['state']!='complete': fail('capture_not_complete','Only sealed captures can freeze')
                    captures[key]=capture
                for channel in certificate['selection']['channels']:
                    requests[capture_key(channel['capture'])][tuple(channel[k] for k in ('stream_id','channel_id','first_index','last_index'))]=channel
        files={}; kinds=set()
        # One owned reader for sequential file work, never service/global state.
        # It closes before the separate current-metadata phase/publication locks.
        with self._reader() as file_connection:
            for ordinal,key in enumerate(sorted(captures,key=lambda k:(k.gateway_id,k.device_id,str(k.capture_id)))):
                file=self.file_prepare(key,captures[key],tuple(requests[key].values()),actor,connection=file_connection)
                file['descriptor']['filename']=f'capture-{ordinal+1:04d}.owpwave'; files[key]=file; kinds.add(file['frame'].body['source_kind'])
        if purpose=='software_pair_test' and not kinds<={'synthetic','bench_measurement'} or purpose=='field_pair_input' and kinds!={'field_measurement'}: fail('source_mismatch','Source kinds do not match purpose')
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); cur=conn.cursor()
            imports=defaultdict(dict)
            for certificate in certificates:
                registry=self.registry_now(certificate); snap=certificate['snapshot']; snap['registry']['freeze']=registry
                reasons=self.current(cur,certificate,captures,registry=registry)
                if purpose!='raw_archive' and reasons: fail('restricted','Quantitative members have current restrictions')
                snap['restrictions']=self.restriction_snapshot(cur,certificate,captures,reasons)
                for channel in certificate['selection']['channels']:
                    file=files[capture_key(channel['capture'])]
                    snap['payload_fingerprints'].extend(p for p in file['fingerprints'] if all(p[k]==channel[k] for k in ('stream_id','channel_id','first_index','last_index')))
                for chosen in certificate['selection']['captures']:
                    key=capture_key(chosen['key'])
                    for ref in chosen['origins']:
                        origin=self._get(cur,UUID(ref['id'])); provenance=origin.body['provenance']
                        if provenance['transport']=='file':
                            id=UUID(provenance['import_id'])
                            cur.execute('SELECT import_id,file_sha256,file_bytes,line_count,final_lf FROM waveforms.imports WHERE import_id=%s',(id,))
                            original=cur.fetchone()
                            if original is None: fail('file_identity','Selected original import is missing')
                            imports[key][str(id)]=dict(import_id=str(id),file_sha256=original[1],bytes=original[2],line_count=original[3],final_lf=original[4])
                snap['payload_fingerprints'].sort(key=canonical_bytes); bounded(snap,262144)
            for key,originals in imports.items():
                if len(originals)>8: fail('file_limit','Too many selected original imports')
                files[key]['descriptor']['source_files']=[originals[id] for id in sorted(originals)]
        return dict(certificates=certificates,captures=captures,files=files,source_kinds=sorted(kinds))

    def capture_rows(self,cur,keys,*,current=False):
        keys=list(dict.fromkeys(keys))
        if len(keys)>2000: fail('member_limit','Too many physical captures')
        captures={}
        for start in range(0,len(keys),100):
            batch=keys[start:start+100]
            fields=',done.status AS completion_status,EXISTS(SELECT 1 FROM waveforms.capture_restrictions r WHERE r.subject_id=c.capture_pk) AS restricted' if current else ''
            join=' LEFT JOIN waveforms.completion done ON done.capture_pk=c.capture_pk' if current else ''
            cur.execute('SELECT c.gateway_id,c.device_id,c.capture_id,c.capture_pk,c.manifest_fact_id,c.manifest_sha256,c.state'+fields+
                ' FROM unnest(%s::text[],%s::text[],%s::uuid[]) AS wanted(gateway_id,device_id,capture_id) JOIN waveforms.capture_metadata c USING(gateway_id,device_id,capture_id)'+join,
                ([k.gateway_id for k in batch],[k.device_id for k in batch],[k.capture_id for k in batch]))
            while (capture:=row(cur)) is not None:
                key=CaptureKey(*(capture.pop(k) for k in ('gateway_id','device_id','capture_id')))
                captures[key]=capture
        return captures

    def load_prepared(self,cur,version_id):
        cur.execute('SELECT kind,reference_id,quality_run_id,split,snapshot FROM waveforms.dataset_members WHERE version_id=%s ORDER BY ordinal LIMIT 1001',(version_id,))
        members=cur.fetchall()
        if len(members)>1000: fail('member_limit','Frozen member bound differs')
        certificates=[]; keys=set()
        for start in range(0,len(members),100):
            batch=members[start:start+100]; quality_rows={}
            ids=list({quality for kind,reference,quality,split,snapshot in batch if kind=='capture'})
            if ids:
                cur.execute('SELECT * FROM waveforms.quality_runs WHERE quality_run_id=ANY(%s)',(ids,))
                while (run:=row(cur)) is not None: quality_rows[run['quality_run_id']]=run
            for kind,reference,quality,split,snapshot in batch:
                # Same certificate validation; only capture-member SQL is batched.
                certificate=self.certificate(cur,dict(kind=kind,reference_id=str(reference),quality_run_id=str(quality),split=split),
                    quality_rows=quality_rows if kind=='capture' else None)
                certificate['snapshot']=snapshot; certificates.append(certificate)
                keys.update(capture_key(chosen['key']) for chosen in certificate['selection']['captures'])
                if len(keys)>2000: fail('member_limit','Too many physical captures')
            del quality_rows
        captures=self.capture_rows(cur,keys)
        if set(captures)!=keys: fail('member_missing','Frozen capture absent')
        return dict(certificates=certificates,captures=captures)
