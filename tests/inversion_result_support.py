"""Real acquired snapshot/publication, deterministic prediction (not numerical E2E)."""
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
import importlib.util
from backend.inversion.contracts import AuthorityRef, AuthoritySnapshot
from backend.inversion.transactions import InversionError
from backend.waveforms.commands import Actor
from tests.inversion_worker import RunCase, ACTOR, REF, authority
from tests.inversion_governance_support import command

REVIEWER=Actor('local','2','Independent reviewer','reviewer')
PERMISSIONS=frozenset(('inversion.read','inversion.review','inversion.export','inversion.audit.read',
                      'waveform.read','waveform.raw.read','waveform.export'))

class ResultCase(RunCase):
    def setUp(self):
        if importlib.util.find_spec('backend.inversion.results') is None:
            self.skipTest('Result service not implemented yet')
        super().setUp()
        from backend.inversion.results import ResultService
        self.denied=set(); self.session_valid=True
        self.results=ResultService(self.inversion_fixture.connect,
            read_connect=lambda:self.inversion_fixture.connect('reader'),authorize=self.current_authority)
        self.run=self.create(); attempt=self.service.claim(uuid4()); self.prepared_result=self.result()
        self.published=self.service.publish(attempt,self.prepared_result,authority(REF))

    def current_authority(self,actor,capability):
        if not self.session_valid or capability in self.denied:
            raise InversionError('forbidden',status=403)
        return AuthoritySnapshot(AuthorityRef(actor.domain,int(actor.user_id),1),PERMISSIONS-self.denied,
                                 datetime.now(timezone.utc),1)

    def approve(self):
        return self.results.review(self.published.id,'approve',command(REVIEWER))

    def revoke_parameter(self):
        self.parameters.revoke(self.request.parameter_id,command(REVIEWER,3))
