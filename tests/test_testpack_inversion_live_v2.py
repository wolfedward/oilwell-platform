"""Run only against a fresh committed owned installation; missing opt-in may skip."""
import os
import unittest
from tests.testpack_owned_lab_support import run_explicit_live


@unittest.skipUnless(os.environ.get('OILWELL_TESTPACK_INVERSION_V2_LIVE')=='1','owned v2 live test not opted in')
class OwnedInversionLiveV2Tests(unittest.TestCase):
    def test_full_owned_workflow(self):
        result=run_explicit_live()
        self.assertTrue(result['passed'])
        outcomes={o.case_id:o for o in result['outcomes']}
        for case in ('cycle-base','cycle-balance','cycle-friction','cycle-speed'):
            self.assertIn(case,outcomes)
            actual=outcomes[case]
            self.assertEqual(actual.approved_execution_status,'passed')
            self.assertEqual(actual.facts['run']['state'],'completed')
            self.assertEqual(actual.facts['native_prediction_count'],1025)
            self.assertEqual(actual.facts['native_proof_count'],1025)
            self.assertEqual(actual.facts['review']['state'],'approved')
            self.assertEqual(actual.facts['result']['content_sha256'],actual.facts['export']['result_sha256'])


if __name__=='__main__':unittest.main()
