from dataclasses import replace
from uuid import uuid4
from tests.inversion_support import InversionDatabaseCase
from backend.inversion.contracts import InversionCommand, VersionPrecondition
from backend.waveforms.commands import Actor


class TransactionTests(InversionDatabaseCase):
    def command(self):
        return InversionCommand(Actor('local','1','author','engineer'),uuid4(),VersionPrecondition('absent'),'reason')

    def apply(self,cur,command):
        from backend.inversion.transactions import Change
        identifier=self.assembly(cur)
        return Change('assembly.create',identifier,1,'a'*64,{'id':str(identifier)})

    def test_actor_key_body_replay(self):
        from backend.inversion.transactions import InversionTransactions, MutationRequest, LockPlan, InversionError
        transaction=InversionTransactions(self.connect)
        command=self.command(); request=MutationRequest('assembly.create','new',b'{}')
        first=transaction.run(command,self.apply,request=request,dependencies=LockPlan())
        renamed=replace(command,actor=Actor('local','1','renamed','administrator'))
        replay=transaction.run(renamed,self.apply,request=request,dependencies=LockPlan())
        self.assertEqual(first.subject_id,replay.subject_id); self.assertTrue(replay.replayed)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.assemblies'),1)
        for changed in (MutationRequest('assembly.create','new',b'{"x":1}'),MutationRequest('assembly.create','other',b'{}')):
            with self.assertRaises(InversionError) as caught: transaction.run(command,self.apply,request=changed,dependencies=LockPlan())
            self.assertEqual(caught.exception.status,409)
        with self.assertRaises(InversionError): transaction.run(replace(command,reason='other'),self.apply,request=request,dependencies=LockPlan())

    def test_audit_receipt_and_business_rollback_together(self):
        from backend.inversion.transactions import InversionTransactions, MutationRequest, LockPlan
        with self.connect('owner') as conn:
            conn.execute('ALTER TABLE inversion.audit ADD CONSTRAINT injected_failure CHECK(false)')
        try:
            with self.assertRaises(RuntimeError): InversionTransactions(self.connect).run(self.command(),self.apply,request=MutationRequest('assembly.create','new',b'{}'),dependencies=LockPlan())
            self.assertEqual(tuple(self.scalar('SELECT count(*) FROM inversion.'+t) for t in ('assemblies','audit','receipts')),(0,0,0))
        finally:
            with self.connect('owner') as conn: conn.execute('ALTER TABLE inversion.audit DROP CONSTRAINT injected_failure')

    def test_timeouts_and_connection_closed_on_failure(self):
        from backend.inversion.transactions import InversionTransactions, MutationRequest, LockPlan
        connections=[]
        def connect():
            conn=self.connect(); connections.append(conn); return conn
        def apply(cur,command):
            cur.execute("SELECT current_setting('statement_timeout'),current_setting('lock_timeout')")
            self.assertEqual(cur.fetchone(),('3s','1s'))
            raise ValueError('injected')
        with self.assertRaises(ValueError): InversionTransactions(connect).run(self.command(),apply,request=MutationRequest('assembly.create','new',b'{}'),dependencies=LockPlan())
        self.assertTrue(all(conn.closed for conn in connections))

    def test_operator_can_lock_fixed_dependency_without_mutating_it(self):
        from backend.inversion.transactions import InversionTransactions, MutationRequest, LockPlan, Dependency, DependencyKind
        identifier=self.assembly()
        result=InversionTransactions(self.connect).run(self.command(),self.apply,
            request=MutationRequest('assembly.create','new',b'{}'),
            dependencies=LockPlan(rows=(Dependency(DependencyKind.ASSEMBLY,identifier),)))
        self.assertFalse(result.replayed)

    def test_lock_timeout_rolls_back_and_closes_connection(self):
        from backend.inversion.transactions import InversionTransactions, MutationRequest, LockPlan, DEPENDENCY_GUARD, InversionError
        with self.connect() as blocker:
            blocker.execute('SELECT pg_advisory_xact_lock(%s)',(DEPENDENCY_GUARD,))
            with self.assertRaises(InversionError) as caught:
                InversionTransactions(self.connect).run(self.command(),self.apply,request=MutationRequest('assembly.create','new',b'{}'),dependencies=LockPlan())
            self.assertEqual(caught.exception.__cause__.sqlstate,'55P03')
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.receipts'),0)

    def test_statement_timeout_rolls_back(self):
        from backend.inversion.transactions import InversionTransactions, MutationRequest, LockPlan, InversionError
        def slow(cur,command):
            self.assembly(cur)
            cur.execute('SELECT pg_sleep(4)')
        with self.assertRaises(InversionError) as caught:
            InversionTransactions(self.connect).run(self.command(),slow,request=MutationRequest('assembly.create','new',b'{}'),dependencies=LockPlan())
        self.assertEqual(caught.exception.__cause__.sqlstate,'57014')
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.assemblies'),0)
