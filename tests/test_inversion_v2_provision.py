"""Catch overwrites, arbitrary wheels, and incomplete native inventory."""
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import base64
import csv
from hashlib import sha256
import io
import zipfile
import unittest


class V2ProvisionTests(unittest.TestCase):
    def tool(self):
        try:
            return import_module('tools.provision_inversion_v2')
        except ModuleNotFoundError:
            self.fail('owned v2 provisioning is not implemented')

    def test_existing_target_preserved(self):
        tool = self.tool()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root/'keep.txt'
            sentinel.write_text('preserve', encoding='utf-8')
            with self.assertRaises(ValueError):
                tool.provision(root, base_python=Path('not-executed'))
            self.assertEqual(sentinel.read_text(), 'preserve')
            self.assertEqual({p.name for p in root.iterdir()}, {'keep.txt'})

    def test_invalid_parent_or_relative_target_refused(self):
        tool = self.tool()
        for target in (Path('relative-runtime'), Path(Path.cwd().anchor)):
            with self.subTest(target=str(target)), self.assertRaises(ValueError):
                tool.provision(target, base_python=Path('not-executed'))

    def test_wrong_wheel_rejected_before_native_import(self):
        tool = self.tool()
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/tool.WHEEL_NAME
            path.write_bytes(b'not the pinned wheel')
            with self.assertRaisesRegex(ValueError, 'wheel digest'):
                tool.inspect_wheel(path)

    def test_record_covers_files_not_directory_entries(self):
        tool = self.tool()
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/tool.WHEEL_NAME
            files = {tool.DIST_INFO+'/WHEEL': b'Tag: cp313-cp313-win_amd64\n',
                     tool.DIST_INFO+'/METADATA': b'Version: 0.9.0\nLicense-Expression: MIT\n',
                     'flint/module.pyd': b'native-test-bytes'}
            for name in ('flint-3.6.0', 'gmp-6.3.0', 'mpfr-4.2.2'):
                files[tool.DIST_INFO+'/licenses/python-flint.libs/'+name+'/COPYING'] = b'license'
            record = io.StringIO()
            writer = csv.writer(record, lineterminator='\n')
            for name, data in files.items():
                writer.writerow([name, 'sha256='+base64.urlsafe_b64encode(sha256(data).digest()).decode().rstrip('='), str(len(data))])
            writer.writerow([tool.DIST_INFO+'/RECORD', '', ''])
            files[tool.DIST_INFO+'/RECORD'] = record.getvalue().encode()
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('flint/', b'')
                for name, data in files.items():
                    archive.writestr(name, data)
            # Synthetic wheel tests only RECORD mechanics, not the production pin.
            with patch.object(tool, 'WHEEL_SHA256', sha256(path.read_bytes()).hexdigest()):
                inventory = tool.inspect_wheel(path)
            self.assertEqual(set(inventory['files']), set(files))

    def test_installed_tamper_refused(self):
        tool = self.tool()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root/'Lib'/'site-packages'
            site.mkdir(parents=True)
            (site/'module.pyd').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Installed wheel differs'):
                tool.verify_installed(root, {'files': {'module.pyd': sha256(b'approved').hexdigest()}})

    def test_unsupported_base_leaves_target_absent(self):
        tool = self.tool()
        with TemporaryDirectory() as temporary:
            target = Path(temporary)/'new'
            # Only the external interpreter probe is doubled, before any write.
            with patch.object(tool, 'probe_python', return_value={'version':'3.12.14','bits':64,'gil':1}):
                with self.assertRaisesRegex(ValueError, 'CPython 3.13.2'):
                    tool.provision(target, base_python=Path('not-executed'))
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
