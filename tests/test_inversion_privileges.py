import psycopg
from psycopg import sql
from tests.inversion_support import InversionDatabaseCase


class PrivilegeTests(InversionDatabaseCase):
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
