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
    def test_select_rejects_wrong_content_and_stale_review_pins(self):
        from backend.inversion.selections import SelectionService
        models=self.service()
        registered=models.register('beam_energy_balance/1',command())
        models.review(registered.subject_id,'approve',command(REVIEWER,1))
        model=models.get(registered.subject_id)
        parameter=self.approved_parameter()
        selection=SelectionService(self.inversion_fixture.connect,
            read_connect=lambda:self.inversion_fixture.connect('reader'),authorize=self.authorize,
            parameters=self.parameters,models=models)
        body=dict(assembly_id=str(parameter.assembly_id),parameter_id=str(parameter.id),
            parameter_sha256=parameter.content_sha256,parameter_review_version=parameter.review_version,
            model_id=str(model.id),model_sha256=model.content_sha256,model_review_version=model.review_version)
        first=selection.select(body,command())
        self.assertEqual(first.version,1)
        audit_before=self.inv_scalar('SELECT count(*) FROM inversion.audit')
        receipts_before=self.inv_scalar('SELECT count(*) FROM inversion.receipts')
        for field,value in (('parameter_sha256','0'*64),('model_sha256','0'*64),
                            ('parameter_review_version',2),('model_review_version',1)):
            with self.subTest(field=field), self.assertRaises(InversionError) as caught:
                selection.select(body|{field:value},command(version=1))
            self.assertEqual(caught.exception.code,'inversion.selection_pin_mismatch')
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.selections'),1)
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.audit'),audit_before)
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.receipts'),receipts_before)
        for field in ('parameter_sha256','parameter_review_version','model_sha256','model_review_version'):
            incomplete=body.copy(); del incomplete[field]
            with self.subTest(missing=field), self.assertRaises(ValueError): selection.select(incomplete,command(version=1))

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
            def body(index):
                model=models.get(model_records[index].subject_id)
                return dict(assembly_id=str(assembly.subject_id),parameter_id=str(params[index].id),
                    parameter_sha256=params[index].content_sha256,parameter_review_version=params[index].review_version,
                    model_id=str(model.id),model_sha256=model.content_sha256,model_review_version=model.review_version)
            first=selection.select(body(0),command())
            current=selection.select(body(1),command(version=1))
            with self.assertRaises(InversionError): selection.rollback(current.subject_id,command(version=2))
            with self.assertRaises(InversionError): selection.rollback(uuid4(),command(version=2))
            incompatible=body(0)|{key:body(1)[key] for key in ('model_id','model_sha256','model_review_version')}
            with self.assertRaises(InversionError) as caught: selection.select(incompatible,command(version=2))
            self.assertEqual(caught.exception.code,'inversion.selection_incompatible')
            rollback=selection.rollback(first.subject_id,command(version=2))
            self.assertEqual(rollback.version,3)
            models.revoke(model_records[1].subject_id,command(REVIEWER,2))
            with self.assertRaises(InversionError): selection.rollback(current.subject_id,command(version=3))
            self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.selections'),3)


class ModelFoundationTests(unittest.TestCase):
    def test_model_and_selection_services_are_available(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.models'))
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.selections'))
