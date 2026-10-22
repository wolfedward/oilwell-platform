"""Fixed cases and rigorously enclosed forward generation, not field truth."""
from fractions import Fraction as Q
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.inversion.contracts import canonical_bytes
from backend.inversion.interval_runtime import RationalInterval


CONFIG = {'schema':'owp.testpack.config/2','profile':'smoke','seed':20261022,
          'start_utc':'2026-09-30T16:00:00.000000Z','timezone':'Asia/Shanghai',
          'max_bytes':268435456,'generator_version':'finite-input/2'}


class GeneratorV2Tests(unittest.TestCase):
    def module(self,name):
        try: return import_module(name)
        except ModuleNotFoundError: self.fail('v2 generator component missing: '+name)

    def case(self,case_id='cycle-base'):
        registry=self.module('backend.inversion.synthetic_registry')
        return registry.case_definition(sha256(canonical_bytes(CONFIG)).hexdigest(),case_id)

    def test_preregistered_cases(self):
        config=self.module('tools.testpack.config_v2')
        registry=self.module('backend.inversion.synthetic_registry')
        for profile,count in (('smoke',8),('extended',48)):
            body=CONFIG|{'profile':profile}
            parsed=config.parse_config_v2(body)
            self.assertEqual(config.config_dict_v2(parsed),body)
            cases=registry.case_ids(sha256(canonical_bytes(body)).hexdigest())
            self.assertEqual(len(cases),count)
            self.assertEqual(cases[:8],tuple('cycle-'+s for s in ('base','balance','friction','speed','noise','missing','clipped','shifted')))
        for change in ({'seed':20261023},{'start_utc':'2026-10-01T00:00:00.000000Z'},{'profile':'field'}):
            with self.assertRaises(ValueError): config.parse_config_v2(CONFIG|change)

    def test_outward_forward_error_limits_and_same_inputs(self):
        forward=self.module('tools.testpack.forward_v2')
        case=self.case()
        first=forward.forward_enclosures(case,Q(1))
        self.assertEqual(first,forward.forward_enclosures(case,Q(1)))
        self.assertLessEqual(first['theta_rad'].width,Q(2,10**20))
        self.assertLessEqual(first['power_W'].width,Q(2,100))
        self.assertGreater(first['load_N'].lo,0)
        self.assertLess(first['load_N'].hi,100000)

    def test_exact_integer_rounding_and_ambiguous_boundary(self):
        forward=self.module('tools.testpack.forward_v2')
        for value,want in ((Q(1,2),0),(Q(3,2),2),(Q(-1,2),0),(Q(-3,2),-2)):
            self.assertEqual(forward.quantize_interval(RationalInterval(value,value),Q(1)),want)
        with self.assertRaises(RuntimeError):
            forward.quantize_interval(RationalInterval(Q(49,100),Q(51,100)),Q(1))
        with self.assertRaises(RuntimeError):
            forward.quantize_interval(RationalInterval(Q(2**31),Q(2**31)),Q(1))

    def test_roots_enclose_true_reversals_and_do_not_claim_exact(self):
        forward=self.module('tools.testpack.forward_v2')
        roots=forward.reversal_brackets(self.case())
        self.assertEqual(len(roots),2)
        for root in roots:
            self.assertGreater(root.width,0)
            self.assertLess(root.width,Q(1,2**90))
            image=forward.jacobian_enclosure(self.case(),root)
            self.assertLessEqual(image.lo,0)
            self.assertGreaterEqual(image.hi,0)

    def test_integer_rounding_and_i32_full_cycle(self):
        cycles=self.module('tools.testpack.cycles_v2')
        cycle=cycles.generate_case_v2(self.case())
        angle=next(c for c in cycle.channels if c.role=='crank_angle')
        self.assertEqual(angle.scale,'0.00000001')
        self.assertEqual(len(angle.raw),1025)
        self.assertEqual(cycle.time_ns,tuple(i*7812500 for i in range(1025)))
        self.assertTrue(all(a<b for a,b in zip(angle.raw,angle.raw[1:])))
        self.assertTrue(all(-(2**31)<=v<2**31 for c in cycle.channels for v in c.raw))
        self.assertEqual(cycle,cycles.generate_case_v2(self.case()))
        self.assertEqual(cycle.diagnostics['physical_evaluations'],1025*129)
        self.assertLessEqual(Q(cycle.diagnostics['power_enclosure_width_max_W']),Q(2,100))
        self.assertLessEqual(Q(cycle.diagnostics['angle_enclosure_width_max_rad']),Q(2,10**20))
        self.assertEqual(cycle.diagnostics['field_accuracy_certified'],False)

    def test_signal_rules_do_not_change_seed_or_hide_quality(self):
        cycles=self.module('tools.testpack.cycles_v2')
        registry=self.module('backend.inversion.synthetic_registry')
        base=cycles.generate_case_v2(self.case())
        noisy=cycles.generate_case_v2(self.case('cycle-noise'))
        for channel_index,amplitude in ((0,Q(10)),(1,Q(1,10**6))):
            before,after=base.channels[channel_index],noisy.channels[channel_index]
            scale=Q(before.scale)
            for i in (0,400,1024):
                noise=amplitude*(2*registry.counter_fraction('cycle-noise',before.role,i)-1)
                self.assertLessEqual(abs((after.raw[i]-before.raw[i])*scale-noise),scale)
        missing=cycles.generate_case_v2(self.case('cycle-missing'))
        self.assertEqual(missing.channels[0].quality[400:420],(1,)*20)
        self.assertEqual(missing.channels[0].raw[400:420],(0,)*20)
        shifted=cycles.generate_case_v2(self.case('cycle-shifted'))
        self.assertEqual(shifted.channels[0].raw[:-2],base.channels[0].raw[2:])
        self.assertEqual(shifted.channels[0].quality[-2:],(1,1))

    def test_v2_storage_roundtrip_and_v1_golden_identity(self):
        storage=self.module('tools.testpack.storage')
        config=self.module('tools.testpack.config_v2')
        from tests.test_testpack_contracts import CONFIG as OLD,GENERATOR,example
        from tools.testpack.config import parse_config
        with TemporaryDirectory(prefix='inversion-v2-roundtrip-') as tmp:
            root=Path(tmp).resolve()
            old=storage.write_pack(root/'old',parse_config(OLD),(example(),),GENERATOR)
            self.assertEqual(old.manifest['pack_id'],'tp-3ec2a4eca7a61950dfd21b37')
            before=(root/'old/manifest.json').read_bytes()
            parsed=config.parse_config_v2(CONFIG)
            newgen=GENERATOR|{'version':'owp.testpack.generator/2'}
            new=storage.write_pack(root/'new',parsed,(example(),),newgen)
            self.assertEqual(new.manifest['schema'],'owp.testpack/1')
            self.assertEqual(new.manifest['configuration'],CONFIG)
            self.assertEqual(storage.load_pack(new.root).manifest,new.manifest)
            self.assertEqual(storage.load_pack(old.root).manifest,old.manifest)
            self.assertEqual(before,(root/'old/manifest.json').read_bytes())
            for cfg,gen in ((parsed,GENERATOR),(parse_config(OLD),newgen)):
                with self.assertRaises(ValueError): storage.pack_identity(cfg,gen)

    def test_v2_fingerprint_and_waveform_are_version_matched(self):
        storage=self.module('tools.testpack.storage')
        self.assertTrue(hasattr(storage,'generator_fingerprint_v2'))
        fingerprint=storage.generator_fingerprint_v2()
        self.assertEqual(fingerprint['version'],'owp.testpack.generator/2')
        self.assertEqual(fingerprint,storage.generator_fingerprint_v2())
        from tools.testpack.config_v2 import parse_config_v2
        from tools.testpack.contracts import Channel
        from tools.testpack.waveforms import _encode
        import json
        raw=_encode('version-probe',parse_config_v2(CONFIG),(Channel('crank_angle','rad','0.00000001','0',(1,2),(0,0)),),128,7812500)
        self.assertEqual(json.loads(raw.splitlines()[0])['producer']['recorder_version'],'owp.testpack.generator/2')

    def test_clip_boundary_touch_is_not_silently_classified(self):
        cycles=self.module('tools.testpack.cycles_v2')
        self.assertTrue(hasattr(cycles,'clip_interval'))
        for lo,hi in ((20000,20001),(-20001,-20000),(-20001,20001)):
            with self.assertRaises(ValueError): cycles.clip_interval(RationalInterval(Q(lo),Q(hi)))
        for lo,hi,want,quality in ((-20000,20000,None,0),(20001,20002,20000,2),(-20002,-20001,-20000,2)):
            interval=RationalInterval(Q(lo),Q(hi))
            result,q=cycles.clip_interval(interval)
            self.assertEqual(q,quality)
            self.assertEqual(result,interval if want is None else RationalInterval(Q(want),Q(want)))

    def test_smoke_build_v2_roundtrip(self):
        build=self.module('tools.testpack.build_v2')
        from tools.testpack.config_v2 import parse_config_v2
        from tools.testpack.storage import load_pack
        with TemporaryDirectory(prefix='inversion-v2-build-') as tmp:
            root=Path(tmp).resolve()
            first=build.build_v2(parse_config_v2(CONFIG),root/'first')
            second=build.build_v2(parse_config_v2(CONFIG),root/'second')
            self.assertEqual(first.manifest,second.manifest)
            self.assertEqual(load_pack(first.root).manifest,first.manifest)
            self.assertEqual(first.manifest['configuration'],CONFIG)
            self.assertEqual(first.manifest['generator']['version'],'owp.testpack.generator/2')
            cycles=[c for c in first.manifest['cases'] if c['case_id'].startswith('cycle-')]
            self.assertEqual(len(cycles),8)
            self.assertTrue(all(a['size_bytes']>0 for a in first.manifest['artifacts']))
            print('v2-smoke generated bytes:',sum(a['size_bytes'] for a in first.manifest['artifacts']))

    def test_v2_history_uses_matching_generator(self):
        from tools.testpack.history import generate_history
        from tools.testpack.config_v2 import parse_config_v2
        history=generate_history(parse_config_v2(CONFIG))
        self.assertEqual(len(history),2)


if __name__=='__main__': unittest.main()
