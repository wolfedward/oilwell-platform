from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.testpack.contracts import PackConfig
from tools.testpack.storage import write_pack,load_pack,canonical_bytes
from tests.test_testpack_contracts import example,GENERATOR


class TestPackReviewFixes(unittest.TestCase):
    def test_check_target_must_be_a_declared_case_artifact(self):
        for target in ('../outside.owpwave','C:/private/file.owpwave','data/unlisted.owpwave','other-case.json'):
            with self.subTest(target=target),TemporaryDirectory() as folder:
                root=Path(folder)
                case=replace(example(),expected=({'check_id':'check','layer':'offline','assertion':'equals','target':target,'expected':'valid'},))
                with self.assertRaises(ValueError):write_pack(root/'bad',PackConfig(),(case,),GENERATOR)
                pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
                manifest=pack.manifest;manifest['cases'][0]['expected']=list(case.expected)
                raw=canonical_bytes(manifest);(pack.root/'manifest.json').write_bytes(raw)
                (pack.root/'manifest.sha256').write_text(sha256(raw).hexdigest()+'\n')
                with self.assertRaises(ValueError):load_pack(pack.root)

    def test_legal_out_of_order_chunks_preserve_native_sample_order(self):
        from tools.testpack.cycles import generate_cycles
        from tools.testpack.waveforms import encode_cycle,decode_channels
        from tools.testpack.waveform_run import read_cycle
        from backend.waveforms.files import scan_file
        from io import BytesIO
        cycle=generate_cycles(PackConfig())[0];case=encode_cycle(cycle,PackConfig())
        lines=case.artifacts[0].content.splitlines();lines[1],lines[2]=lines[2],lines[1]
        reordered=replace(case,artifacts=(replace(case.artifacts[0],content=b'\n'.join(lines)+b'\n'),case.artifacts[1]))
        scan_file(BytesIO(reordered.artifacts[0].content))
        self.assertEqual(decode_channels(reordered),decode_channels(case))
        with TemporaryDirectory() as folder:
            pack=write_pack(Path(folder)/'pack',PackConfig(),(reordered,),GENERATOR)
            restored,_=read_cycle(pack,cycle.case_id)
            self.assertEqual(restored.channels,cycle.channels)

    def test_all_protected_ports_refused_before_socket_creation(self):
        from tools.testpack.lab import PROTECTED_PORTS
        from tools.testpack.viewer import serve_viewer
        from tools.testpack.modbus_device import ReadOnlyModbusDevice
        from tools.testpack.history import telemetry_point_map
        with TemporaryDirectory() as folder:
            pack=write_pack(Path(folder)/'pack',PackConfig(),(example(),),GENERATOR)
            for port in PROTECTED_PORTS:
                with self.subTest(port=port):
                    with patch('tools.testpack.viewer.ThreadingHTTPServer',side_effect=AssertionError('must refuse before bind')):
                        with self.assertRaises(ValueError):serve_viewer(pack,port=port)
                    with self.assertRaises(ValueError):ReadOnlyModbusDevice({1:telemetry_point_map()},host='127.0.0.1',port=port)
