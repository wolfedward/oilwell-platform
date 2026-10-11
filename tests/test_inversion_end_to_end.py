"""File-first synthetic evidence through the installed spawned worker and HTTP."""
import unittest

from tests import inversion_e2e_support as support


class EndToEndTests(support.EndToEndCase):
    def test_complete_parameter_to_review_export_flow(self):
        result = self.completed_result()
        self.assertEqual(result['execution'], 'completed')
        self.assertEqual(result['source'], 'synthetic')
        self.assertEqual(result['current_applicability']['state'], 'applicable')
        self.check_dual_curves(result)
        self.review_result(result)
        exported = self.export_result(result, 'quantitative')
        self.assertIn(result['content_sha256'].encode(), exported)
        self.assertGreater(self.inversion_fixture.scalar('SELECT count(*) FROM inversion.audit'), 0)

    def test_revoke_after_run_keeps_evidence_only(self):
        before = self.completed_result()
        self.review_result(before)
        self.revoke_parameter()
        after = self.result_detail(before['id'])
        self.assertEqual(after['content_sha256'], before['content_sha256'])
        self.assertEqual(after['body'], before['body'])
        self.assertEqual(after['current_applicability']['state'], 'restricted')
        status, _ = self.http('POST', '/exports', self.envelope(dict(
            result_id=before['id'], intent='quantitative', format='json')))
        self.assertEqual(status, 409)
        self.assertIn(before['content_sha256'].encode(), self.export_result(after, 'evidence'))

    def test_supported_previous_model_rollback_does_not_rewrite_result(self):
        before = self.completed_result()
        original, replacement = self.select_test_only_replacement()
        status, reply = self.http('POST', '/selections/' + str(original.id) + '/rollback',
                                  self.envelope(version=replacement.version))
        self.assertEqual(status, 200, reply)
        after = self.result_detail(before['id'])
        self.assertEqual(after['content_sha256'], before['content_sha256'])
        self.assertEqual(after['body'], before['body'])
        self.assertEqual(after['current_applicability']['state'], 'restricted')


if __name__ == '__main__':
    unittest.main()
