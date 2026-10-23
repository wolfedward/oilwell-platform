"""Explicit read-only full-native acceptance, separate from fast guard tests."""
from copy import deepcopy
from fractions import Fraction as Q
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import unittest


POSITIVES = ('cycle-base','cycle-balance','cycle-friction','cycle-speed') + tuple(
    f'cycle-variant-{index:02d}' for index in range(40))
FAULTS = ('cycle-noise','cycle-missing','cycle-clipped','cycle-shifted')
DESCRIPTORS = (('active_power','W',Q(1,100)), ('crank_angle','rad',Q(1,10**8)),
               ('load','kN',Q(1,1000)), ('displacement','m',Q(1,10**6)))


def _require(condition, message):
    if not condition: raise AssertionError(message)


def require_inventory(native_rows: list[dict], positive_ids: tuple[str,...]) -> dict:
    _require(type(positive_ids) is tuple and len(positive_ids)==44
             and set(positive_ids)==set(POSITIVES), 'exact registered positives required')
    _require(type(native_rows) is list and len(native_rows)==44,'44 native records required')
    seen=set(); encoded=0; frames=0
    for row in native_rows:
        _require(type(row) is dict and set(row)=={'case_id','sample_rate_hz','first_sample_index',
                 'time_offset_ns','channels'},'complete native row required')
        case=row['case_id']
        _require(type(case) is str and case in positive_ids and case not in seen,'duplicate/unknown case')
        seen.add(case)
        _require(type(row['sample_rate_hz']) is Q and row['sample_rate_hz']==128,'native rate mismatch')
        for key in ('first_sample_index','time_offset_ns'):
            _require(type(row[key]) is int and row[key]==0,'native timing mismatch')
        channels=row['channels']
        _require(type(channels) is list and len(channels)==4,'four native channels required')
        roles=set()
        for channel in channels:
            _require(type(channel) is dict and set(channel)=={'role','unit','scale','offset','raw','quality'},
                     'complete channel required')
            role=channel['role']
            _require(type(role) is str and role not in roles,'duplicate channel role')
            roles.add(role)
            descriptors=[d for d in DESCRIPTORS if d[0]==role]
            _require(len(descriptors)==1,'unknown native channel')
            _,unit,scale=descriptors[0]
            _require(channel['unit']==unit and type(channel['scale']) is Q and channel['scale']==scale
                     and type(channel['offset']) is Q and channel['offset']==0,'native descriptor mismatch')
            raw,quality=channel['raw'],channel['quality']
            _require(type(raw) is tuple and type(quality) is tuple and len(raw)==len(quality)==1025,
                     'complete native channel arrays required')
            _require(all(type(v) is int and -(2**31)<=v<2**31 for v in raw),'invalid i32 original code')
            _require(all(type(v) is int and v==0 for v in quality),'positive native quality mismatch')
            encoded+=len(raw)
        frames+=1025
    _require(seen==set(positive_ids),'incomplete native inventory')
    return dict(positive_cases=len(seen),native_frames=frames,encoded_values=encoded)


def _require_report_bindings(release, manifest, report, positives):
    from tools.testpack.deliver_v2 import require_offline_proofs
    try:
        _require(release['profile']==manifest['profile']=='extended','extended release required')
        commit=release['source']['commit']
        _require(type(commit) is str and len(commit)==40 and all(c in '0123456789abcdef' for c in commit)
                 and manifest['generator']['source_commit']==commit,'source commit mismatch')
        _require(report['schema']=='owp.testpack.offline-report/1'
                 and report['pack_id']==manifest['pack_id'],'offline pack identity mismatch')
        require_offline_proofs(report,positives)
        finite={}
        for case in positives+FAULTS:
            rows=[r for r in report['checks'] if r.get('check_id')==case+'-finite-inversion']
            _require(len(rows)==1,'missing/duplicate finite proof')
            row=rows[0]; actual=row['actual']
            _require(row['status']=='passed' and actual['assertion_status']=='passed'
                     and actual['source_proof_status']=='verified'
                     and type(actual['native_points']) is int and actual['native_points']==1025
                     and actual['field_accuracy_certified'] is False
                     and actual['approved_execution_status']=='not_run','finite assertion failed')
            finite[case]=row
        return finite
    except (KeyError,TypeError,ValueError) as error:
        raise AssertionError('invalid release/report binding') from error


def _release_paths(root):
    from tools.testpack.storage import strict_json
    from tools.review_deploy_source import verify_export
    root=Path(root).resolve(strict=True)
    release=strict_json((root/'release.json').read_bytes())
    _require(release.get('schema')=='owp.testpack.v2-release/1' and release.get('status')=='ready'
             and release.get('profile')=='extended','ready extended release required')
    for key,relative in (('root','.'),('stage','source'),('pack','pack-v2-extended'),
                         ('offline','offline-v2-extended.json'),('lab','lab-v2-extended')):
        _require(Path(release[key]).resolve(strict=True)==(root/relative).resolve(strict=True),
                 'release path escaped expected layout')
    verify_export(root/'source/code',release['source'])
    return root,release


def _inspect_exported_release(root):
    """Called in the immutable export, so reconstructed checker hashes match it."""
    from backend.inversion import coverage_sources
    from backend.inversion.contracts import canonical_bytes
    from tools.testpack.inversion_binding_v2 import decode_native,offline_binding
    from tools.testpack.inversion_run_v2 import positive_case_ids
    from tools.testpack.storage import load_pack,strict_json,generator_fingerprint_v2
    root,release=_release_paths(root)
    _require(Path(coverage_sources.__file__).resolve().is_relative_to(root/'source/code'),
             'binding audit must use immutable exported source')
    pack=load_pack(root/'pack-v2-extended')
    _require(pack.manifest['generator']==generator_fingerprint_v2(),'generator/export identity mismatch')
    positives=positive_case_ids(pack)
    _require(len(positives)==44 and set(positives)==set(POSITIVES),'registered case set mismatch')
    report_path=root/'offline-v2-extended.json'
    report_bytes=report_path.read_bytes(); report=strict_json(report_bytes)
    proofs=_require_report_bindings(release,pack.manifest,report,positives)
    native_rows=[]; files={}; bindings={}
    for case in positives+FAULTS:
        records=[r for r in pack.manifest['cases'] if r['case_id']==case]
        _require(len(records)==1,'native case uniqueness')
        record=records[0]; paths=record['artifact_paths']
        wave=[p for p in paths if p.endswith('.owpwave')]
        metadata=[p for p in paths if p.endswith('.json')]
        _require(len(wave)==len(metadata)==1,'unique native wave and descriptor required')
        raw=(pack.root/wave[0]).read_bytes(); meta_bytes=(pack.root/metadata[0]).read_bytes()
        meta=strict_json(meta_bytes); scanned,channels=decode_native(raw)
        stream=scanned.manifest.streams[0]
        # Scanned ChannelSpec deliberately has no scale/offset. These live in
        # the external cycle metadata and must be checked, not filled by defaults.
        actual_descriptors=[{key:getattr(c,key) for key in ('role','unit','scale','offset')} for c in channels]
        _require(meta['channels']==actual_descriptors,'external scale/offset differs from native contract')
        _require(meta['file_sha256']==sha256(raw).hexdigest() and meta['case_id']==case
                 and meta['pack_id']==pack.manifest['pack_id'] and meta['source_kind']=='synthetic'
                 and meta['sample_rate_hz']==128 and meta['frame_count']==1025,'native metadata identity')
        if case in positives:
            native_rows.append(dict(case_id=case,sample_rate_hz=Q(stream.rate_num,stream.rate_den),
                first_sample_index=stream.first_sample_index,time_offset_ns=stream.offset_ns,
                channels=[dict(role=d['role'],unit=d['unit'],scale=Q(d['scale']),offset=Q(d['offset']),
                    raw=c.raw,quality=c.quality) for d,c in zip(meta['channels'],channels)]))
        row=proofs[case]; actual=row['actual']
        _require(set(row['artifacts'])==set(paths),'proof case artifact mismatch')
        binding=offline_binding(pack,case)  # bind bytes only; never predict or recompute trajectories
        expected=dict(parameter_sha256=binding.parameters.raw_sha256,
            parameter_core_sha256=binding.parameters.parameter_core_sha256,
            original_file_sha256=sha256(raw).hexdigest(),
            bound_file_sha256=binding.evidence.power.bound_file_sha256,
            evidence_sha256=sha256(canonical_bytes({
                'power':binding.evidence.power.channel_sha256,'angle':binding.evidence.angle.channel_sha256,
                'contract':binding.parameters.coverage_contract.raw_sha256,
                'checker':coverage_sources.checker_sha256()})).hexdigest())
        _require(all(actual.get(k)==v for k,v in expected.items()),'proof source/content digest mismatch')
        files[wave[0]]=sha256(raw).hexdigest();files[metadata[0]]=sha256(meta_bytes).hexdigest()
        bindings[case]=expected
    return require_inventory(native_rows,positives)|dict(faults=list(FAULTS),
        source_commit=release['source']['commit'],pack_id=pack.manifest['pack_id'],
        offline_sha256=sha256(report_bytes).hexdigest(),files=files,bindings=bindings,
        source_kind='synthetic',field_accuracy_certified=False,
        scope='full_native_inventory_and_prior_offline_proof_binding_not_second_physics_run')


def inspect_extended_release(root: Path) -> dict:
    """Audit through the new export's Python/cwd; never mix working-tree hashes."""
    root,_=_release_paths(root)
    environment={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','PGPASSWORD')
                 and not k.startswith(('PLATFORM_','REVIEW_'))}
    environment.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    script=('import importlib.util,json,sys; '
            's=importlib.util.spec_from_file_location("inventory_audit",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s);s.loader.exec_module(m); '
            'print(json.dumps(m._inspect_exported_release(sys.argv[2]),sort_keys=True))')
    result=subprocess.run([str(root/'source/venv/Scripts/python.exe'),'-B','-c',script,
                           str(Path(__file__).resolve()),str(root)],
        cwd=root/'source/code',env=environment,capture_output=True,text=True,encoding='utf-8',timeout=300,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    _require(result.returncode==0,'exported inventory failed: '+result.stderr[-3000:])
    return json.loads(result.stdout)


class InventoryGuardTests(unittest.TestCase):
    def inventory(self):
        return [dict(case_id=case, sample_rate_hz=Q(128), first_sample_index=0,
                     time_offset_ns=0, channels=[dict(role=role,unit=unit,scale=scale,
                         offset=Q(0),raw=(0,)*1025,quality=(0,)*1025)
                         for role,unit,scale in DESCRIPTORS]) for case in POSITIVES]

    def helper(self, name):
        helper=globals().get(name)
        self.assertTrue(callable(helper),name+' is missing')
        return helper

    def test_full_inventory_counts_without_mutation(self):
        rows=self.inventory(); before=deepcopy(rows)
        result=self.helper('require_inventory')(rows,POSITIVES)
        self.assertEqual(result,dict(positive_cases=44,native_frames=45100,encoded_values=180400))
        self.assertEqual(rows,before)

    def test_missing_duplicate_or_invalid_native_data_refused(self):
        helper=self.helper('require_inventory')
        changes=(lambda rows:rows.pop(),
                 lambda rows:rows.append(deepcopy(rows[0])),
                 lambda rows:rows[0]['channels'].pop(),
                 lambda rows:rows[0]['channels'][0].update(raw=(0,)*1024),
                 lambda rows:rows[0]['channels'][0].update(quality=(0,)*1024),
                 lambda rows:rows[0]['channels'][0].update(scale=Q(1)),
                 lambda rows:rows[0]['channels'][0].update(offset=Q(1)),
                 lambda rows:rows[0]['channels'][0].update(raw=(2**31,)+(0,)*1024),
                 lambda rows:rows[0]['channels'][0].update(raw=(False,)+(0,)*1024),
                 lambda rows:rows[0]['channels'][0].update(quality=(1,)+(0,)*1024),
                 lambda rows:rows[0]['channels'][0].update(unit='kW'),
                 lambda rows:rows[0].update(sample_rate_hz=Q(64)),
                 lambda rows:rows[0].update(first_sample_index=False),
                 lambda rows:rows[0].update(time_offset_ns=1))
        for index,change in enumerate(changes):
            with self.subTest(change=index):
                rows=self.inventory();change(rows)
                with self.assertRaises(AssertionError):helper(rows,POSITIVES)
        with self.assertRaises(AssertionError):helper([{'passed':True,'count':180400}],POSITIVES)
        with self.assertRaises(AssertionError):helper(self.inventory(),POSITIVES[:-1])

    def reports(self):
        release={'profile':'extended','source':{'commit':'a'*40}}
        manifest={'profile':'extended','pack_id':'tp-test',
                  'generator':{'source_commit':'a'*40}}
        report={'schema':'owp.testpack.offline-report/1','pack_id':'tp-test','status':'passed',
                'checks':[{'check_id':case+'-finite-inversion','status':'passed','actual':{
                    'source_proof_status':'verified','passed':case in POSITIVES,
                    'native_points':1025,'field_accuracy_certified':False,
                    'approved_execution_status':'not_run','original_prediction_coverage':'0.9',
                    'joint_coverage':'0.9','load_rmse_N':1,'assertion_status':'passed'}}
                    for case in POSITIVES+FAULTS]}
        return release,manifest,report

    def test_wrong_source_and_missing_proof_refused(self):
        helper=self.helper('_require_report_bindings')
        release,manifest,report=self.reports()
        helper(release,manifest,report,POSITIVES)
        for kind in ('commit','pack','missing','duplicate','fault','claim','coverage','fault_claim','fault_points'):
            with self.subTest(kind=kind):
                r,m,p=deepcopy((release,manifest,report))
                if kind=='commit':m['generator']['source_commit']='b'*40
                elif kind=='pack':p['pack_id']='tp-wrong'
                elif kind=='missing':p['checks'].pop(0)
                elif kind=='duplicate':p['checks'].append(deepcopy(p['checks'][0]))
                elif kind=='fault':p['checks'].pop()
                elif kind=='claim':p['checks'][0]['actual']['field_accuracy_certified']=True
                elif kind=='fault_claim':p['checks'][-1]['actual']['field_accuracy_certified']=True
                elif kind=='fault_points':p['checks'][-1]['actual']['native_points']=1024
                else:p['checks'][0]['actual']['joint_coverage']='0.79'
                with self.assertRaises(AssertionError):helper(r,m,p,POSITIVES)


class ExtendedNativeTests(unittest.TestCase):
    def test_actual_extended_native_inventory(self):
        root=os.environ.get('OILWELL_QUADRATURE_EXTENDED_RELEASE')
        self.assertTrue(root,'explicit new extended release required; this test cannot skip')
        result=inspect_extended_release(Path(root))
        self.assertEqual(result['positive_cases'],44)
        self.assertEqual(result['native_frames'],45100)
        self.assertEqual(result['encoded_values'],180400)
        self.assertEqual(result['faults'],list(FAULTS))
        print('extended-native-inventory:',result)


if __name__=='__main__':unittest.main()
