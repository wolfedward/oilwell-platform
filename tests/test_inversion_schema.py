import psycopg
from uuid import uuid4
from tests.inversion_support import InversionDatabaseCase, TEST_DATABASE


class SchemaTests(InversionDatabaseCase):
    def test_recovery_lookup_index_is_migrated_and_structure_checked(self):
        from backend.inversion.migrate import migrate,check_schema
        self.assertIsNotNone(self.scalar("SELECT to_regclass('inversion.audit_actor_request')"))
        self.assertEqual(migrate(self.settings['owner'],expected_database=TEST_DATABASE,limits=self.limits)['version'],'5')
        with self.connect('owner') as conn:
            conn.execute('DROP INDEX inversion.audit_actor_request')
            with self.assertRaises(RuntimeError): check_schema(conn,expected_database=TEST_DATABASE,limits=self.limits)
            conn.rollback()

    def test_mechanical_effective_end_is_constrained_append_only(self):
        from psycopg.types.json import Jsonb
        assembly=self.assembly(); version=uuid4()
        with self.connect() as conn:
            conn.execute('INSERT INTO inversion.assembly_versions(id,assembly_id,version,body,content_sha256) VALUES(%s,%s,2,%s,%s)',
                (version,assembly,Jsonb({'valid_from':'2026-01-01T00:00:00.000000Z','valid_to':None}),'b'*64))
        with self.connect() as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute('INSERT INTO inversion.assembly_versions(assembly_id,version,body,content_sha256) VALUES(%s,3,%s,%s)',
                    (assembly,Jsonb({'valid_from':'2026-02-01T00:00:00.000000Z','valid_to':None}),'c'*64))
        with self.connect() as conn:
            conn.execute("INSERT INTO inversion.mechanical_events(id,version_id,revision,valid_to,actor,reason,body) VALUES(gen_random_uuid(),%s,2,'2026-03-01','{}','Explicit closure','{}')",(version,))
        for end in ('2025-12-01','2026-04-01'):
            with self.connect() as conn:
                with self.assertRaises(psycopg.Error):
                    conn.execute("INSERT INTO inversion.mechanical_events(id,version_id,revision,valid_to,actor,reason,body) VALUES(gen_random_uuid(),%s,3,%s,'{}','Invalid','{}')",(version,end))
        for query in ('UPDATE inversion.mechanical_state SET valid_to=NULL', 'DELETE FROM inversion.mechanical_events'):
            with self.connect() as conn:
                with self.assertRaises(psycopg.Error): conn.execute(query)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.mechanical_events'),1)
        self.assertEqual(self.scalar('SELECT content_sha256 FROM inversion.assembly_versions WHERE id=%s',(version,)),'b'*64)

    def test_immutable_payload_update_rejected(self):
        identifier=self.assembly()
        for mutation in ('UPDATE inversion.assemblies SET body=\'{"changed":true}\' WHERE id=%s',
                         'DELETE FROM inversion.assemblies WHERE id=%s'):
            with self.connect('owner') as conn:
                with self.assertRaises(psycopg.Error), conn.transaction(): conn.execute(mutation,(identifier,))

    def test_complete_migration_is_repeatable_and_limits_persist(self):
        from backend.inversion.migrate import migrate
        result=migrate(self.settings['owner'],expected_database=TEST_DATABASE,limits=self.limits)
        self.assertFalse(result['applied'])
        self.assertEqual(self.scalar('SELECT limits->>\'selection_samples\' FROM inversion.capacity'),'32768')

    def test_partial_or_drifted_schema_refused_without_repair(self):
        from backend.inversion.migrate import check_schema
        for mutation in ('ALTER TABLE inversion.assemblies ADD COLUMN drift int',
                         'ALTER TABLE inversion.assembly_versions ADD COLUMN drift int',
                         'DELETE FROM inversion.schema_migrations',
                         'DELETE FROM inversion.schema_migrations WHERE version=2',
                         "UPDATE inversion.schema_migrations SET sha256=repeat('0',64) WHERE version=1",
                         "UPDATE inversion.schema_migrations SET sha256=repeat('0',64)"):
            with self.connect('owner') as conn:
                try:
                    conn.execute(mutation)
                    with self.assertRaises(RuntimeError): check_schema(conn,expected_database=TEST_DATABASE,limits=self.limits)
                finally: conn.rollback()

    def test_assignment_overlap_rejected(self):
        a,b=self.assembly(),self.assembly()
        with self.connect() as conn:
            conn.execute("INSERT INTO inversion.assignments(id,assembly_id,well_id,valid_from,valid_to,body,content_sha256) VALUES(gen_random_uuid(),%s,'well','2026-01-01','2026-03-01','{}',repeat('a',64))",(a,))
        with self.connect() as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute("INSERT INTO inversion.assignments(id,assembly_id,well_id,valid_from,valid_to,body,content_sha256) VALUES(gen_random_uuid(),%s,'well','2026-02-01','2026-04-01','{}',repeat('b',64))",(b,))

    def test_assignment_end_event_preserves_body_and_allows_next_binding(self):
        self.assertIsNotNone(self.scalar("SELECT to_regclass('inversion.assignment_events')"),'assignment end event foundation required')
        assignment=uuid4(); first,second=self.assembly(),self.assembly()
        with self.connect() as conn:
            conn.execute("INSERT INTO inversion.assignments(id,assembly_id,well_id,valid_from,body,content_sha256) VALUES(%s,%s,'well','2026-01-01','{}',repeat('a',64))",(assignment,first))
            conn.execute("INSERT INTO inversion.assignment_events(id,assignment_id,version,valid_to,actor,reason) VALUES(gen_random_uuid(),%s,2,'2026-03-01','{}','reassignment')",(assignment,))
            conn.execute("INSERT INTO inversion.assignments(id,assembly_id,well_id,valid_from,body,content_sha256) VALUES(gen_random_uuid(),%s,'well','2026-03-01','{}',repeat('b',64))",(second,))
        self.assertIsNone(self.scalar('SELECT valid_to FROM inversion.assignments WHERE id=%s',(assignment,)))
        self.assertEqual(self.scalar('SELECT version FROM inversion.assignment_state WHERE assignment_id=%s',(assignment,)),2)
        for end in ('2025-12-01','2026-04-01'):
            with self.connect() as conn:
                with self.assertRaises(psycopg.Error):
                    conn.execute("INSERT INTO inversion.assignment_events(id,assignment_id,version,valid_to,actor,reason) VALUES(gen_random_uuid(),%s,3,%s,'{}','invalid')",(assignment,end))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.assignment_events'),1)

    def create_run(self,conn):
        assembly=self.assembly(conn); parameter,model,run=uuid4(),uuid4(),uuid4()
        conn.execute("INSERT INTO inversion.parameter_versions(id,assembly_id,assembly_version,version,author,body,content_sha256) VALUES(%s,%s,1,1,'{}','{}',repeat('a',64))",(parameter,assembly))
        conn.execute("INSERT INTO inversion.model_versions(id,model_key,version,body,content_sha256) VALUES(%s,%s,1,'{}',repeat('a',64))",(model,str(model)))
        conn.execute("INSERT INTO inversion.run_requests(id,pair_id,parameter_id,model_id,actor,body,content_sha256,input_sha256) VALUES(%s,gen_random_uuid(),%s,%s,'{}','{}',repeat('a',64),repeat('b',64))",(run,parameter,model))
        return run

    def test_state_requires_corresponding_append_event(self):
        with self.connect() as conn:
            with self.assertRaises(psycopg.Error), conn.transaction():
                run=self.create_run(conn)
                conn.execute("INSERT INTO inversion.run_state(run_id,state) VALUES(%s,'queued')",(run,))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.run_state'),0)

    def test_stale_attempt_result_is_rejected(self):
        with self.connect() as conn:
            run=self.create_run(conn)
            conn.execute("INSERT INTO inversion.run_state(run_id,state) VALUES(%s,'queued')",(run,))
            conn.execute("INSERT INTO inversion.run_events(id,run_id,version,state,body) VALUES(gen_random_uuid(),%s,1,'queued','{}')",(run,))
            conn.execute("UPDATE inversion.run_state SET state='running',version=2,generation=1,owner_id=gen_random_uuid(),deadline=clock_timestamp()+interval '180 seconds' WHERE run_id=%s",(run,))
            conn.execute("INSERT INTO inversion.run_events(id,run_id,version,state,body) VALUES(gen_random_uuid(),%s,2,'running','{}')",(run,))
            conn.execute("INSERT INTO inversion.attempts(id,run_id,generation,owner_id,deadline,body) VALUES(gen_random_uuid(),%s,2,gen_random_uuid(),clock_timestamp()+interval '180 seconds','{}')",(run,))
        with self.connect() as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute("INSERT INTO inversion.results(id,run_id,generation,input_snapshot,body,content_sha256) VALUES(gen_random_uuid(),%s,2,'{}','{}',repeat('a',64))",(run,))

    def test_successor_mechanical_version_preserves_initial_body(self):
        self.assertIsNotNone(self.scalar("SELECT to_regclass('inversion.assembly_versions')"),'mechanical version foundation required')
        assembly=self.assembly()
        with self.connect() as conn:
            conn.execute("INSERT INTO inversion.assembly_versions(assembly_id,version,body,content_sha256) VALUES(%s,2,'{\"motor\":\"replaced\"}',repeat('b',64))",(assembly,))
            conn.execute("INSERT INTO inversion.parameter_versions(id,assembly_id,assembly_version,version,author,body,content_sha256) VALUES(gen_random_uuid(),%s,2,1,'{}','{}',repeat('c',64))",(assembly,))
        self.assertEqual(self.scalar('SELECT content_sha256 FROM inversion.assembly_versions WHERE assembly_id=%s AND version=1',(assembly,)),'a'*64)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.assembly_versions WHERE assembly_id=%s',(assembly,)),2)
        with self.connect('owner') as conn:
            with self.assertRaises(psycopg.Error): conn.execute("UPDATE inversion.assembly_versions SET body='{}' WHERE assembly_id=%s AND version=2",(assembly,))

    def test_parameter_cannot_reference_nonexistent_mechanical_version(self):
        assembly=self.assembly()
        with self.connect() as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute("INSERT INTO inversion.parameter_versions(id,assembly_id,assembly_version,version,author,body,content_sha256) VALUES(gen_random_uuid(),%s,99,1,'{}','{}',repeat('a',64))",(assembly,))
