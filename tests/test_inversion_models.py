import importlib.util
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from tests.inversion_governance_support import GovernanceCase, command, AUTHOR, REVIEWER
from backend.inversion.contracts import canonical_bytes
from backend.inversion.transactions import InversionError


class ModelGovernanceTests(GovernanceCase):
    def service(self, installed=None):
        from backend.inversion.models import ModelService
        return ModelService(self.inversion_fixture.connect,
            read_connect=lambda:self.inversion_fixture.connect('reader'),authorize=self.authorize,
            **({'installed':installed} if installed is not None else {}))

    def test_model_registrar_cannot_review_after_rename_or_admin_promotion(self):
        service=self.service(); item=service.register('beam_energy_balance/1',command())
        self.assertEqual(service.get(item.subject_id).review_state,'unreviewed')
        with self.assertRaises(InversionError):
            service.review(item.subject_id,'approve',command(replace(AUTHOR,username='Other',role_code='administrator'),1))
        approved=command(REVIEWER,1)
        service.review(item.subject_id,'approve',approved)
        self.assertTrue(service.review(item.subject_id,'approve',approved).replayed)
        self.allowed=False
        with self.assertRaises(InversionError) as caught: service.review(item.subject_id,'approve',approved)
        self.assertEqual(caught.exception.status,403)

    def test_uninstalled_upload_and_changed_installed_artifact_are_refused(self):
        service=self.service()
        with self.assertRaises(InversionError): service.register('https://example.invalid/code.py',command())
        from backend.inversion.models import InstalledModel
        with TemporaryDirectory() as folder:
            artifact=Path(folder)/'test.txt'; artifact.write_text('isolated v1',encoding='utf-8')
            installed={'test-model-1':InstalledModel('test-model-1',1,(artifact,))}
            service=self.service(installed)
            item=service.register('test-model-1',command())
            before=service.get(item.subject_id).content_sha256
            artifact.write_text('isolated v2',encoding='utf-8')
            with self.assertRaises(InversionError): service.review(item.subject_id,'approve',command(REVIEWER,1))
            self.assertEqual(service.get(item.subject_id).content_sha256,before)
            artifact.write_text('isolated v1',encoding='utf-8')
            service.review(item.subject_id,'approve',command(REVIEWER,1))
            artifact.write_text('isolated v3',encoding='utf-8')
            with self.assertRaises(InversionError): service.check_current(item.subject_id)

    def test_only_compatible_approved_rollback(self):
        from backend.inversion.models import InstalledModel
        from backend.inversion.selections import SelectionService
        with TemporaryDirectory() as folder:
            artifact=Path(folder)/'test.txt'; artifact.write_text('isolated rollback fixture',encoding='utf-8')
            installed={key:InstalledModel(key,index,(artifact,)) for index,key in enumerate(('test-model-1','test-model-2'),1)}
            models=self.service(installed)
            selection=SelectionService(self.inversion_fixture.connect,
                read_connect=lambda:self.inversion_fixture.connect('reader'),authorize=self.authorize,
                parameters=self.parameters,models=models)
            assembly,_=self.create_assembly(); params=[]; model_records=[]
            for index,key in enumerate(installed):
                m=models.register(key,command())
                models.review(m.subject_id,'approve',command(REVIEWER,1)); model_records.append(m)
                body=self.body(assembly.subject_id,parent=index or None)
                body['policy']['body']['model_version']=key
                body['policy']['content_sha256']=sha256(canonical_bytes(body['policy']['body'])).hexdigest()
                body['provenance']['coverage']['policy_sha256']=body['policy']['content_sha256']
                params.append(self.approved_parameter(assembly.subject_id,body,index or None))
            def body(index): return dict(assembly_id=str(assembly.subject_id),parameter_id=str(params[index].id),model_id=str(model_records[index].subject_id))
            first=selection.select(body(0),command())
            current=selection.select(body(1),command(version=1))
            with self.assertRaises(InversionError): selection.rollback(current.subject_id,command(version=2))
            with self.assertRaises(InversionError): selection.rollback(uuid4(),command(version=2))
            with self.assertRaises(InversionError): selection.select(body(0)|{'model_id':str(model_records[1].subject_id)},command(version=2))
            rollback=selection.rollback(first.subject_id,command(version=2))
            self.assertEqual(rollback.version,3)
            models.revoke(model_records[1].subject_id,command(REVIEWER,2))
            with self.assertRaises(InversionError): selection.rollback(current.subject_id,command(version=3))
            self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.selections'),3)


class ModelFoundationTests(unittest.TestCase):
    def test_model_and_selection_services_are_available(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.models'))
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.selections'))
