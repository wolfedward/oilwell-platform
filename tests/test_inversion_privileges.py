import psycopg
from psycopg import sql
from tests.inversion_support import InversionDatabaseCase


class PrivilegeTests(InversionDatabaseCase):
    def test_maintain_and_grant_option_are_refused_for_runtime_roles(self):
        from backend.inversion.privileges import probe_role
        from backend.inversion.transactions import InversionError
        for role in ('reader','operator'):
            name=sql.Identifier(self.sidecar['roles'][role])
            for grant_option in (False,True):
                with self.subTest(role=role,grant_option=grant_option):
                    with self.connect('owner') as conn:
                        conn.execute(sql.SQL('GRANT MAINTAIN ON inversion.assemblies TO {}{}').format(
                            name,sql.SQL(' WITH GRANT OPTION' if grant_option else '')))
                    try:
                        with self.assertRaises(InversionError) as caught:
                            probe_role(lambda:self.connect(role),role)
                        self.assertEqual(caught.exception.code,'inversion.unsafe_privileges')
                    finally:
                        with self.connect('owner') as conn:
                            conn.execute(sql.SQL('REVOKE MAINTAIN ON inversion.assemblies FROM {}').format(name))
        for role in ('reader','operator'):
            with self.connect(role) as conn:
                self.assertEqual(conn.execute("SELECT has_table_privilege(current_user,'inversion.assemblies','MAINTAIN'),has_table_privilege(current_user,'inversion.assemblies','MAINTAIN WITH GRANT OPTION')").fetchone(),(False,False))
            probe_role(lambda:self.connect(role),role)

    def test_runtime_roles_and_receiver(self):
        from backend.inversion.privileges import probe_role
        for role in ('reader','operator'): probe_role(lambda:self.connect(role),role)
        with self.connect('owner') as conn:
            self.assertFalse(conn.execute("SELECT has_schema_privilege(%s,'inversion','USAGE')",(self.parent['roles']['receiver'],)).fetchone()[0])
        with self.connect('reader') as conn:
            with self.assertRaises(psycopg.Error): self.assembly(conn)

    def test_public_privileges_are_refused(self):
        from backend.inversion.privileges import probe_role
        with self.connect('owner') as conn: conn.execute('GRANT INSERT ON inversion.assemblies TO PUBLIC')
        try:
            with self.assertRaises(RuntimeError): probe_role(lambda:self.connect('reader'),'reader')
        finally:
            with self.connect('owner') as conn: conn.execute('REVOKE INSERT ON inversion.assemblies FROM PUBLIC')

    def test_public_read_access_is_also_refused(self):
        from backend.inversion.privileges import probe_role
        with self.connect('owner') as conn: conn.execute('GRANT SELECT ON inversion.assemblies TO PUBLIC')
        try:
            with self.assertRaises(RuntimeError): probe_role(lambda:self.connect('reader'),'reader')
        finally:
            with self.connect('owner') as conn: conn.execute('REVOKE SELECT ON inversion.assemblies FROM PUBLIC')

    def test_inherited_privileges_are_refused(self):
        from backend.inversion.privileges import probe_role
        owner,reader=(sql.Identifier(self.sidecar['roles'][r]) for r in ('owner','reader'))
        self.guard.execute(sql.SQL('GRANT {} TO {}').format(owner,reader))
        try:
            with self.assertRaises(RuntimeError): probe_role(lambda:self.connect('reader'),'reader')
        finally: self.guard.execute(sql.SQL('REVOKE {} FROM {}').format(owner,reader))

    def test_column_only_insert_privilege_is_refused(self):
        from backend.inversion.privileges import probe_role
        reader=sql.Identifier(self.sidecar['roles']['reader'])
        with self.connect('owner') as conn:
            conn.execute(sql.SQL('GRANT INSERT(body) ON inversion.assemblies TO {}').format(reader))
        try:
            with self.assertRaises(RuntimeError): probe_role(lambda:self.connect('reader'),'reader')
        finally:
            with self.connect('owner') as conn:
                conn.execute(sql.SQL('REVOKE INSERT(body) ON inversion.assemblies FROM {}').format(reader))

    def test_trigger_function_owner_drift_is_refused(self):
        from backend.inversion.privileges import probe_role
        reader,owner=(sql.Identifier(self.sidecar['roles'][r]) for r in ('reader','owner'))
        self.guard.execute(sql.SQL('ALTER FUNCTION inversion.assignment_project() OWNER TO {}').format(reader))
        try:
            with self.assertRaises(RuntimeError): probe_role(lambda:self.connect('operator'),'operator')
        finally: self.guard.execute(sql.SQL('ALTER FUNCTION inversion.assignment_project() OWNER TO {}').format(owner))
