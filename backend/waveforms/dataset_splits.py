"""Exact, conservative connected-component holdout rules; no I/O or UUID independence."""
from collections import defaultdict
from datetime import datetime
from fractions import Fraction
from .dataset_contracts import validate_split,fail
from .registry_context import datetime_ns


def absolute_support(snapshot):
    if not snapshot or any(snapshot.get(k) is None for k in ('utc_start','lower_ns','upper_ns','uncertainty_ns')): return None
    anchor=datetime_ns(datetime.fromisoformat(snapshot['utc_start'].replace('Z','+00:00')))
    return Fraction(anchor)+Fraction(snapshot['lower_ns']),Fraction(anchor)+Fraction(snapshot['upper_ns'])


def check_splits(members,plan):
    plan=validate_split(plan)
    if plan['kind']=='none':
        if any(m['split']!='unassigned' for m in members): fail('split_invalid','Collection members must be unassigned')
        return False
    used={m['split'] for m in members}
    if 'unassigned' in used or not {'train','test'}<=used: fail('split_invalid','Independent split requires assigned train and test')
    parent=list(range(len(members))); known={}; ranges=defaultdict(list); wells={}
    def root(i):
        while parent[i]!=i: parent[i]=parent[parent[i]]; i=parent[i]
        return i
    def join(a,b): parent[root(b)]=root(a)
    def same(key,i):
        if key in known: join(i,known[key])
        else: known[key]=i
    for i,member in enumerate(members):
        snap=member['snapshot']; cycles=snap['physical_cycles']
        if not cycles: fail('independence_unknown','No resolved physical cycle')
        registry=snap['registry']['freeze']; support=absolute_support(registry)
        for run in snap['quality']:
            key=run['source_key']; same(('capture',key['gateway_id'],key['device_id'],key['capture_id']),i)
        for fingerprint in snap['payload_fingerprints']:
            same(('payload',fingerprint['content_sha256']),i)
            if fingerprint.get('stream_data_sha256'): same(('stream_payload',fingerprint['stream_data_sha256']),i)
        for cycle in cycles:
            if len(cycle['events'])!=3 or not cycle['source_intervals']: fail('independence_unknown','Incomplete original physical identity')
            same(('cycle',cycle['physical_cycle_key']),i)
            for interval in cycle['source_intervals']:
                source=interval['source']
                if any(not source.get(k) for k in ('gateway_id','device_id','boot_id')): fail('independence_unknown','Unknown physical source')
                physical=(source['gateway_id'],source['device_id'])
                lo,hi=int(interval['first_index']),int(interval['last_index'])
                if lo>hi: fail('independence_unknown','Invalid original index interval')
                ranges[('index',*physical,source['boot_id'])].append((lo,hi,i))
                if support: ranges[('time',*physical)].append((*support,i))
        if plan['kind']=='well_holdout':
            well=registry.get('well') if registry else None
            if not well or registry['status']!='resolved' or not well.get('id'): fail('well_unknown','Independent well is not established')
            id=well['id']
            if id in wells and wells[id]!=member['split']: fail('well_leakage','Well crosses partitions')
            wells[id]=member['split']
        else:
            interval=plan['intervals'].get(member['split'])
            if support is None or interval is None: fail('time_unknown','Chronological support is not established')
            if not Fraction(interval['start_ns'])<=support[0]<=support[1]<=Fraction(interval['end_ns']): fail('time_leakage','Member exceeds declared inclusive interval')
    for intervals in ranges.values():
        previous=None
        for low,high,i in sorted(intervals):
            if previous is not None and low<=previous[0]:
                join(i,previous[1]); previous=(max(high,previous[0]),previous[1])
            else: previous=(high,i)
    partitions={}
    for i,member in enumerate(members):
        component=root(i)
        if component in partitions and partitions[component]!=member['split']: fail('physical_leakage','Related physical observations cross partitions')
        partitions[component]=member['split']
    if plan['kind']=='time_holdout' and (plan['intervals']['validation'] is not None)!=('validation' in used): fail('split_invalid','Validation interval and membership differ')
    return True
