import json,socket,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.preview_support import make_source,ROOT
class PreviewLifecycleTests(unittest.TestCase):
    def test_repackage_stopped_preview_preserves_data_accounts_and_state(self):
        from tools.page_preview import prepare,repackage,verify_code
        from backend.preview.snapshot import file_hash
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);target=base/'install';prepare(make_source(base),target,code_source=ROOT,revision=None)
            hashes={name:file_hash(target/name) for name in ['business.db','private/accounts.json','private/source.db']}
            (target/'state.db').write_bytes(b'owned-state-must-survive')
            result=repackage(target,code_source=ROOT,revision=None)
            self.assertNotEqual(result['code_path'],'code');verify_code(target)
            self.assertEqual((target/'state.db').read_bytes(),b'owned-state-must-survive')
            self.assertEqual(hashes,{name:file_hash(target/name) for name in hashes})
            self.assertTrue((target/'code/preview.html').exists())
            with patch('tools.page_preview.status',return_value={'status':'ready'}):
                with self.assertRaisesRegex(ValueError,'停止'):repackage(target,code_source=ROOT,revision=None)
    def test_prepare_refuses_existing_and_validates_package(self):
        from tools.page_preview import prepare,verify_code
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source=make_source(base);target=base/'install'
            result=prepare(source,target,code_source=ROOT,revision=None)
            self.assertEqual(result['mode'],'preview');self.assertEqual(result['application_pages'],55);verify_code(target)
            with self.assertRaises(FileExistsError):prepare(source,target,code_source=ROOT,revision=None)
            (target/'code/preview.html').write_text('corrupt',encoding='utf-8')
            with self.assertRaises(ValueError):verify_code(target)
    def test_wrong_port_identity_and_snapshot_rejected(self):
        from tools.page_preview import prepare,launch,status,stop
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);target=base/'install';prepare(make_source(base),target,code_source=ROOT,revision=None)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
                with self.assertRaises(OSError):launch(target,port=port)
            self.assertEqual(stop(target)['status'],'stopped')
            with (target/'business.db').open('ab') as stream:stream.write(b'bad')
            with self.assertRaises(ValueError):launch(target,port=0)
    def test_real_launch_and_owned_stop(self):
        from tools.page_preview import prepare,launch,status,stop
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);target=base/'install';prepare(make_source(base),target,code_source=ROOT,revision=None)
            result=launch(target,port=0,timeout=120)
            self.addCleanup(lambda:stop(target) if target.exists() else None)
            self.assertEqual(result['status'],'ready');self.assertEqual(status(target)['status'],'ready')
            self.assertEqual(stop(target)['status'],'stopped');self.assertEqual(stop(target)['status'],'stopped')

    def test_foreign_identity_is_never_stopped(self):
        from tools.page_preview import prepare,stop
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);target=base/'install';prepare(make_source(base),target,code_source=ROOT,revision=None)
            (target/'private/runtime.json').write_text(json.dumps({'pid':123,'port':8772,'instance_id':'owned','shutdown_token':'private'}))
            with patch('tools.page_preview.request',return_value={'instance_id':'foreign'}) as request:
                with self.assertRaisesRegex(ValueError,'不属于'):stop(target)
                self.assertEqual(request.call_count,1)

    def test_start_failure_and_timeout_only_use_new_child_handle(self):
        from tools.page_preview import prepare,launch
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);target=base/'install';prepare(make_source(base),target,code_source=ROOT,revision=None)
            with patch('tools.page_preview.subprocess.Popen') as popen,patch('tools.page_preview.time.monotonic',side_effect=[0,999]):
                child=popen.return_value;child.pid=123;child.poll.return_value=None
                with self.assertRaises(TimeoutError):launch(target,port=0,timeout=1)
                child.terminate.assert_called_once();child.wait.assert_called_once()
            with patch('tools.page_preview.status',return_value={'status':'stopped'}),patch('tools.page_preview.subprocess.Popen') as popen:
                child=popen.return_value;child.pid=124;child.poll.return_value=1
                with self.assertRaisesRegex(RuntimeError,'异常退出'):launch(target,port=0)
                child.terminate.assert_not_called()
