from collections import Counter
from copy import deepcopy
import json
import unittest
from uuid import uuid4

from backend.studies.condition_models import train_conditions
from backend.studies.contracts import KINDS,validate_envelope,StudyError
from backend.studies.energy_effects import evaluate_effects
from backend.studies.energy_schedule import optimize_schedule
from backend.studies.protection import replay_protection
from backend.studies.recovery import fit_recovery
from tools.testpack.contracts import PackConfig
from tools.testpack.cycles import generate_cycles
from tools.testpack.currents import generate_currents
from tools.testpack.history import generate_history
from tools.testpack.study_inputs import generate_studies,bind_study


class TestPackStudyInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=generate_studies(PackConfig(),generate_cycles(PackConfig()),generate_history(PackConfig()),generate_currents(PackConfig()))
        cls.templates={c.case_id:json.loads(c.artifacts[0].content) for c in cls.cases}

    def template(self,kind):
        return self.templates['study-'+kind.replace('_','-')]

    def payload(self,kind):
        return self.template(kind)['envelope']['payload']

    def test_seven_study_kinds_and_fixture_source(self):
        self.assertEqual({c.facts['study_kind'] for c in self.cases},KINDS)
        for t in self.templates.values():
            self.assertEqual(t['envelope']['source']['kind'],'software_fixture')
            self.assertTrue(t['envelope']['source']['references'])

    def test_train_validation_test_are_disjoint_and_test_labels_do_not_train(self):
        training=self.payload('condition_training')
        self.assertEqual(Counter(r['partition'] for r in training['rows']),{'train':30,'validation':30,'test':30})
        wells=[{r['well_id'] for r in training['rows'] if r['partition']==part} for part in ('train','validation','test')]
        self.assertFalse(wells[0]&wells[1] or wells[1]&wells[2] or wells[0]&wells[2])
        model=train_conditions(training)['artifact']
        changed=deepcopy(training)
        labels=['合成基准','合成高摩擦','合成失衡']
        for row in changed['rows']:
            if row['partition']=='test':
                row['label']=labels[(labels.index(row['label'])+1)%3]
        self.assertEqual(model,train_conditions(changed)['artifact'])

    def test_dependency_templates_require_actual_ids(self):
        t=self.template('condition_prediction')
        with self.assertRaises(ValueError):
            bind_study(t,{},'a'*64)
        record=str(uuid4())
        first=bind_study(t,{'condition_training':record},'a'*64)
        self.assertEqual(first,bind_study(t,{'condition_training':record},'a'*64))
        self.assertEqual(first['payload']['model_record_id'],record)
        self.assertIn('manifest-sha256:'+'a'*64,first['source']['references'])
        validate_envelope(first)
        self.assertNotIn('model_record_id',t['envelope']['payload'])

    def test_recovery_and_effects_have_known_bounds(self):
        fit=fit_recovery(self.payload('recovery_fit'))
        self.assertEqual(fit['status'],'fitted')
        self.assertLessEqual(abs(fit['parameters']['tau_minutes']-120),2)
        self.assertAlmostEqual(fit['parameters']['equilibrium_depth_m'],100,places=7)
        effects=evaluate_effects(self.payload('energy_effects'))
        self.assertEqual(effects['status'],'descriptive')
        self.assertFalse(effects['causal_effect_proven'])
        schedule=bind_study(self.template('energy_schedule'),{'recovery_fit:TESTPACK-WELL-001':str(uuid4())},'a'*64)['payload']
        schedule['wells'][0]['recovery']=fit
        result=optimize_schedule(schedule)
        self.assertEqual(result['status'],'feasible')
        self.assertFalse(result['dispatch_allowed'])

    def test_boundary_inputs_observe_real_rejections(self):
        short=self.templates['study-recovery-short']['envelope']['payload']
        self.assertEqual(fit_recovery(short)['status'],'rejected')
        effects=self.templates['study-effects-low-coverage']['envelope']['payload']
        self.assertEqual(evaluate_effects(effects)['status'],'insufficient_evidence')
        invalid=self.templates['study-protection-invalid']['envelope']['payload']
        with self.assertRaises(StudyError) as raised:
            replay_protection(invalid)
        self.assertEqual(raised.exception.code,'invalid_field')


if __name__=='__main__':
    unittest.main()
