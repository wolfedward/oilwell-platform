"""Test-only, fail-closed unittest security observer. Never repairs catalog state.

Importing this module does not load configuration, connect to a database, or
discover tests. The final implementer must supply approved private paths,
configured connection factories, a full per-alias identity gate, exact setup
allowances, and a final baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Mapping
import unittest


PRIVATE = Path("C:/Users/hp/Documents/外专双百/data")
PINNED_CONFIGS = {
    "registry": PRIVATE / "registry-test/registry-test.local.toml",
    "ingest": PRIVATE / "ingest-reliability-test/ingest-test.local.toml",
    "waveform": PRIVATE / "waveform-test/waveform-test.local.toml",
    "waveform_registry": PRIVATE / "waveform-test/registry-sidecar/registry-test.local.toml",
    "inversion": PRIVATE / "waveform-test/inversion-sidecar/inversion-test.local.toml",
    "center": PRIVATE / "registry-test/legacy-test.local.toml",
}
PINNED_IDENTITIES = {
    "registry": PRIVATE / "registry-test/identity.json",
    "ingest": PRIVATE / "ingest-reliability-test/identity.json",
    "waveform": PRIVATE / "waveform-test/identity.json",
    "waveform_registry": PRIVATE / "waveform-test/registry-sidecar/identity.json",
    "inversion": PRIVATE / "waveform-test/inversion-sidecar/identity.json",
}
ENDPOINTS = {
    "registry": ("oilwell_registry_test", "127.0.0.1", 3822),
    "ingest": ("oilwell_ingest_reliability_test", "127.0.0.1", 3822),
    "waveform": ("oilwell_waveforms_test", "127.0.0.1", 3822),
}
SCHEMAS = {
    "registry": ("device_registry",),
    "ingest": ("ingest_reliability", "device_registry"),
    "waveform": ("waveforms", "device_registry", "inversion"),
}
PUBLIC_RELATIONS = ("telemetry_samples", "ingest_messages")
SECURITY_MODULES = {
    "tests.test_ingest_privileges": "ingest",
    "tests.test_registry_privileges": "registry",
    "tests.test_waveform_privileges": "waveform",
    "tests.test_waveform_registry_dependency": "waveform",
    "tests.test_inversion_privileges": "inversion",
}
SECURITY_TEST_IDS = {
    "tests.test_waveform_end_to_end.RecoveryInventoryTests.test_effective_relation_acls_preserve_empty_public_runtime_grant_option_and_owner": "waveform",
    "tests.test_waveform_end_to_end.RecoveryInventoryTests.test_native_check_roundtrip_keeps_operand_boolean_and_constraint_flags_distinct": "waveform",
    "tests.test_waveform_end_to_end.RecoveryPreparationTests.test_small_restore_uses_supervised_target_exports_and_exact_audit_deltas": "inversion",
    "tests.test_ingest_consumer.LiveStartupTests.test_check_refuses_actual_privilege_drift_without_silently_revoking_it": "ingest",
    "tests.test_ingest_operations.IncidentOperationsTests.test_raw_function_public_execute_is_rejected_by_readonly_preflight": "ingest",
    "tests.test_ingest_publisher.PublisherStartupTests.test_privilege_drift_is_refused_without_runtime_repair": "ingest",
    "tests.test_ingest_reopen_function.ReopenFunctionTests.test_preflight_accepts_exact_function_grant_but_rejects_public_exposure": "ingest",
    "tests.test_ingest_startup.LiveManagementStartupTests.test_missing_operator_privilege_never_exposes_a_partial_ready_runtime": "ingest",
    "tests.test_ingest_startup.LiveManagementStartupTests.test_missing_metadata_structure_is_reported_not_implicitly_recreated": "ingest",
    "tests.test_ingest_api.IncidentHTTPTests.test_audit_failure_never_returns_raw_or_claims_a_case_change": "ingest",
    "tests.test_ingest_operations.IncidentOperationsTests.test_audit_rejection_rolls_back_case_and_idempotency_result": "ingest",
    "tests.test_ingest_operations.IncidentOperationsTests.test_resend_audit_failure_keeps_previous_delivery_request": "ingest",
    "tests.test_ingest_operations.IncidentOperationsTests.test_raw_audit_failure_or_lost_commit_response_returns_no_raw_data": "ingest",
    "tests.test_ingest_reopen_function.ReopenFunctionTests.test_audit_failure_rolls_back_reopen_inside_the_function": "ingest",
    "tests.test_registry_queries.RegistryQueryTests.test_metadata_and_histories_never_read_raw_telemetry": "registry",
    "tests.test_registry_end_to_end.RegistryEndToEndTests.test_http_audit_failure_rolls_back_business_and_receipt": "registry",
    "tests.test_registry_transactions.RegistryTransactionTests.test_audit_failure_rolls_back_business_receipt_and_revision": "registry",
    "tests.test_registry_lifecycle.RegistryLifecycleTests.test_audit_insert_failure_rolls_back_closed_and_opened_relationships": "registry",
    "tests.test_waveform_api.WaveformHTTPTests.test_readiness_missing_grant_is_unavailable_without_repair": "waveform",
    "tests.test_waveform_capacity.WaveformCapacityTests.test_unlogged_storage_rejected_without_data_loss": "waveform",
    "tests.test_waveform_consumer.WaveformConsumerTests.test_readiness_rejects_unlogged_storage_without_repair": "waveform",
    "tests.test_inversion_parameters.ParameterGovernanceTests.test_metadata_only_privileges_and_probe_drift": "inversion",
    "tests.test_inversion_recovery.RecoveryTests.test_database_commit_failure_rolls_back_results_audit_and_settlement": "inversion",
    "tests.test_inversion_transactions.TransactionTests.test_audit_receipt_and_business_rollback_together": "inversion",
}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str)


def _digest(value) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _acl(value) -> tuple[str, ...] | None:
    """Keep NULL distinct from empty; aclitem text retains PUBLIC and '*' option."""
    return None if value is None else tuple(sorted(str(item) for item in value))


def _definition(value: str | None) -> str | None:
    # pg_get_*def is PostgreSQL's canonical deparse. Never compare recreated OIDs.
    return None if value is None else sha256(value.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    values: Mapping[str, object]

    def __post_init__(self):
        # Deep-copy into JSON primitives so a caller cannot mutate a baseline.
        object.__setattr__(self, "values", json.loads(_canonical(dict(self.values))))

    def differences(self, other: "Snapshot") -> frozenset["DiffToken"]:
        return frozenset(DiffToken(key,
                                   _digest(self.values[key]) if key in self.values else None,
                                   _digest(other.values[key]) if key in other.values else None)
                         for key in self.values.keys() | other.values.keys()
                         if key not in self.values or key not in other.values
                         or self.values[key] != other.values[key])


@dataclass(frozen=True, order=True)
class DiffToken:
    key: str
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class AuthorizedInputs:
    """Exact test paths and catalog users approved by the final implementer."""

    configs: Mapping[str, Path]
    identities: Mapping[str, Path]
    catalog_users: Mapping[str, str]
    role_names: tuple[str, ...]

    def validate(self) -> None:
        for supplied, pinned in ((self.configs, PINNED_CONFIGS),
                                 (self.identities, PINNED_IDENTITIES)):
            if set(supplied) != set(pinned):
                raise ValueError("Exact private test path inventory required")
            for name, target in pinned.items():
                path = Path(supplied[name])
                if not path.is_absolute() or path.resolve() != target.resolve() or not path.is_file():
                    raise ValueError(f"Unapproved or missing private test path: {name}")
        if set(self.catalog_users) != set(ENDPOINTS) or any(
                not isinstance(user, str) or not user for user in self.catalog_users.values()):
            raise ValueError("Explicit catalog identity for every test DB required")
        if not self.role_names or any(not isinstance(name, str) or not name for name in self.role_names):
            raise ValueError("Explicit verified test role inventory required")


class CatalogSnapshotProvider:
    """Read-only PostgreSQL catalog provider using already configured factories.

    `connectors` and the read-only `full_identity_gate(alias)` must be built by
    the final implementer from pinned test configs/identity helpers. The gate
    must raise on any mismatch. No import name or DSN is accepted from input.
    """

    def __init__(self, authorized: AuthorizedInputs, connectors: Mapping[str, Callable],
                 *, full_identity_gate: Callable[[str], None]):
        authorized.validate()
        if set(connectors) != set(ENDPOINTS) or not all(callable(item) for item in connectors.values()):
            raise ValueError("One configured factory per exact test DB required")
        if not callable(full_identity_gate):
            raise ValueError("Reviewed full identity gate required for every snapshot")
        self.authorized, self.connectors = authorized, dict(connectors)
        self.full_identity_gate = full_identity_gate

    @staticmethod
    def _rows(conn, query: str, parameters=()):
        return conn.execute(query, parameters).fetchall()

    def snapshot(self) -> Snapshot:
        values: dict[str, object] = {}
        for alias in ("registry", "ingest", "waveform"):
            # The caller's pinned read-only gate must check system identifier,
            # data directory, database owner and fixture marker on every pass.
            # A failure propagates before this alias opens a catalog connection.
            self.full_identity_gate(alias)
            # A new transaction and connection for each observation: no inherited
            # fixture cursor, no DDL, no writes, no owner/admin fallback.
            with self.connectors[alias]() as conn:
                with conn.transaction():
                    conn.execute("SET TRANSACTION READ ONLY")
                    conn.execute("SET LOCAL statement_timeout='3s'")
                    conn.execute("SET LOCAL lock_timeout='1s'")
                    actual = conn.execute("SELECT current_database(),host(inet_server_addr()),inet_server_port(),current_user").fetchone()
                    if tuple(actual[:3]) != ENDPOINTS[alias] or actual[3] != self.authorized.catalog_users[alias]:
                        raise RuntimeError(f"Unpinned catalog endpoint/role: {alias}")
                    self._database(conn, alias, values)
                    self._schemas(conn, alias, values)
                    self._relations(conn, alias, values)
                    self._functions(conn, alias, values)
                    self._triggers_constraints(conn, alias, values)
                    self._extension(conn, alias, values)
                    if alias == "registry":
                        self._roles(conn, values)
        return Snapshot(values)

    def _database(self, conn, alias, out):
        rows = self._rows(conn, "SELECT datname,pg_get_userbyid(datdba),datacl::text[] "
                          "FROM pg_database WHERE datname=current_database()")
        for name, owner, acl in rows:
            out[f"{alias}:database:{name}"] = (owner, _acl(acl))

    def _schemas(self, conn, alias, out):
        rows = self._rows(conn, """SELECT nspname,pg_get_userbyid(nspowner),nspacl::text[]
            FROM pg_namespace WHERE nspname=ANY(%s) OR nspname IN ('public','registry_probe_test_saved')
            OR nspname ~ '^pgwaveformtest[0-9a-f]{32}$' ORDER BY nspname""", (list(SCHEMAS[alias]),))
        for name, owner, acl in rows:
            out[f"{alias}:schema:{name}"] = (owner, _acl(acl))

    def _relations(self, conn, alias, out):
        rows = self._rows(conn, """SELECT n.nspname,c.relname,c.relkind,c.relpersistence,pg_get_userbyid(c.relowner),
                c.relacl::text[],c.relrowsecurity,c.relforcerowsecurity,
                CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid,true) ELSE NULL END
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) OR n.nspname='registry_probe_test_saved'
              OR n.nspname ~ '^pgwaveformtest[0-9a-f]{32}$'
              OR (n.nspname='public' AND (c.relname=ANY(%s) OR EXISTS (
                SELECT 1 FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent
                JOIN pg_namespace pn ON pn.oid=p.relnamespace
                WHERE i.inhrelid=c.oid AND pn.nspname='public' AND p.relname='telemetry_samples')))
            ORDER BY n.nspname,c.relname""", (list(SCHEMAS[alias]), list(PUBLIC_RELATIONS)))
        for schema, name, kind, persistence, owner, acl, row_security, force_security, view_def in rows:
            out[f"{alias}:relation:{schema}.{name}"] = (
                kind, persistence, owner, _acl(acl), row_security, force_security, _definition(view_def))
        rows = self._rows(conn, """SELECT n.nspname,c.relname,a.attname,a.attacl::text[]
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE a.attnum>0 AND NOT a.attisdropped AND
              (n.nspname=ANY(%s) OR n.nspname='registry_probe_test_saved'
               OR n.nspname ~ '^pgwaveformtest[0-9a-f]{32}$'
               OR (n.nspname='public' AND (c.relname=ANY(%s) OR EXISTS (
                 SELECT 1 FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent
                 JOIN pg_namespace pn ON pn.oid=p.relnamespace
                 WHERE i.inhrelid=c.oid AND pn.nspname='public' AND p.relname='telemetry_samples'))))
            ORDER BY n.nspname,c.relname,a.attnum""", (list(SCHEMAS[alias]), list(PUBLIC_RELATIONS)))
        for schema, relation, column, acl in rows:
            out[f"{alias}:column:{schema}.{relation}.{column}"] = _acl(acl)

    def _functions(self, conn, alias, out):
        rows = self._rows(conn, """SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),
               pg_get_userbyid(p.proowner),p.proacl::text[],l.lanname,p.probin,p.prosrc,
               p.proparallel,p.prosecdef,p.proleakproof,p.proconfig,
               CASE WHEN p.prosupport=0 THEN NULL ELSE p.prosupport::regprocedure::text END,
               pg_get_functiondef(p.oid),e.extname
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_language l ON l.oid=p.prolang
            LEFT JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid
                 AND d.refclassid='pg_extension'::regclass AND d.deptype='e'
            LEFT JOIN pg_extension e ON e.oid=d.refobjid
            WHERE n.nspname=ANY(%s) OR n.nspname='registry_probe_test_saved'
              OR n.nspname ~ '^pgwaveformtest[0-9a-f]{32}$' OR e.extname='btree_gist'
            ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)""",
                          (list(SCHEMAS[alias]),))
        for schema, name, args, owner, acl, language, bin_path, source, parallel, security_definer, leakproof, config, support, definition, extension in rows:
            out[f"{alias}:function:{schema}.{name}({args})"] = (
                owner, _acl(acl), language, bin_path, _definition(source), parallel,
                security_definer, leakproof, tuple(sorted(config or ())), support,
                _definition(definition), extension)

    def _triggers_constraints(self, conn, alias, out):
        params = (list(SCHEMAS[alias]), list(PUBLIC_RELATIONS))
        where = """n.nspname=ANY(%s) OR n.nspname='registry_probe_test_saved'
            OR n.nspname ~ '^pgwaveformtest[0-9a-f]{32}$'
            OR (n.nspname='public' AND (c.relname=ANY(%s) OR EXISTS (
              SELECT 1 FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent
              JOIN pg_namespace pn ON pn.oid=p.relnamespace
              WHERE i.inhrelid=c.oid AND pn.nspname='public' AND p.relname='telemetry_samples')))"""
        for schema, relation, name, enabled, internal, definition in self._rows(conn,
                "SELECT n.nspname,c.relname,t.tgname,t.tgenabled,t.tgisinternal,pg_get_triggerdef(t.oid,true) "
                "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE " + where, params):
            out[f"{alias}:trigger:{schema}.{relation}.{name}"] = (
                enabled, internal, _definition(definition))
        for schema, relation, name, kind, validated, enforced, definition in self._rows(conn,
                "SELECT n.nspname,c.relname,k.conname,k.contype,k.convalidated,k.conenforced,"
                "pg_get_constraintdef(k.oid,true) FROM pg_constraint k "
                "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE " + where,
                params):
            out[f"{alias}:constraint:{schema}.{relation}.{name}"] = (
                kind, validated, enforced, _definition(definition))

    def _extension(self, conn, alias, out):
        for name, version, schema, owner, relocatable in self._rows(conn,
                "SELECT e.extname,e.extversion,n.nspname,pg_get_userbyid(e.extowner),e.extrelocatable "
                "FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='btree_gist'"):
            out[f"{alias}:extension:{name}"] = (version, schema, owner, relocatable)

    def _roles(self, conn, out):
        names = list(self.authorized.role_names)
        rows = self._rows(conn, """SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreatedb,
               rolcreaterole,rolreplication,rolbypassrls,rolconnlimit,
               shobj_description(oid,'pg_authid')
            FROM pg_roles WHERE rolname=ANY(%s) OR rolname ~ '^wp03_drift_[0-9a-f]{32}$'
            ORDER BY rolname""", (names,))
        tracked = []
        for row in rows:
            tracked.append(row[0])
            out[f"cluster:role:{row[0]}"] = tuple(row[1:])
        rows = self._rows(conn, """SELECT parent.rolname,member.rolname,m.admin_option,
               m.inherit_option,m.set_option
            FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid
            JOIN pg_roles member ON member.oid=m.member
            WHERE parent.rolname=ANY(%s) OR member.rolname=ANY(%s)
            ORDER BY parent.rolname,member.rolname""", (tracked, tracked))
        for parent, member, admin, inherit, set_role in rows:
            out[f"cluster:membership:{parent}->{member}"] = (admin, inherit, set_role)
        for name, acl in self._rows(conn,
                "SELECT parname,paracl::text[] FROM pg_parameter_acl "
                "WHERE parname='session_replication_role'"):
            out[f"cluster:parameter:{name}"] = _acl(acl)


@dataclass
class Evidence:
    max_events: int = 512
    max_differences: int = 64
    sink: Callable[[dict[str, object]], None] | None = None
    events: list[dict[str, object]] = field(default_factory=list)

    def add(self, phase: str, test_id: str, differences: Iterable[DiffToken]) -> None:
        changes = sorted(differences)
        if len(self.events) >= self.max_events or len(changes) > self.max_differences:
            raise RuntimeError("Bounded security evidence limit reached")
        event = {"phase": phase, "test_id": test_id,
                 "differences": [vars(item) for item in changes]}
        self.events.append(event)
        if self.sink is not None:
            self.sink(event)  # caller provides a verified private append-only sink


class RedactedResult(unittest.TestResult):
    """Retain only test IDs and outcome categories, never traceback contents."""

    def __init__(self, sensitive_ids: set[str]):
        super().__init__()
        self.sensitive_ids = sensitive_ids
        self.outcomes: list[tuple[str, str]] = []

    def addError(self, test, err):
        self.errors.append((test, "redacted"))
        self.outcomes.append((test.id(), "error"))
        if test.id() in self.sensitive_ids:
            self.stop()

    def addFailure(self, test, err):
        self.failures.append((test, "redacted"))
        self.outcomes.append((test.id(), "failure"))
        if test.id() in self.sensitive_ids:
            self.stop()

    def addSubTest(self, test, subtest, err):
        if err is None:
            return
        failed = issubclass(err[0], test.failureException)
        bucket = self.failures if failed else self.errors
        bucket.append((subtest, "redacted"))
        self.outcomes.append((test.id(), "subtest_failure" if failed else "subtest_error"))
        if test.id() in self.sensitive_ids:
            self.stop()

    def addUnexpectedSuccess(self, test):
        self.unexpectedSuccesses.append(test)
        self.outcomes.append((test.id(), "unexpected_success"))
        if test.id() in self.sensitive_ids:
            self.stop()

    def addExpectedFailure(self, test, err):
        self.expectedFailures.append((test, "redacted"))
        self.outcomes.append((test.id(), "expected_failure"))
        if test.id() in self.sensitive_ids:
            self.stop()

    def addSkip(self, test, reason):
        self.skipped.append((test, "redacted"))
        self.outcomes.append((test.id(), "skip"))
        self.stop()  # zero-skip gate


def _cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        elif isinstance(item, unittest.TestCase):
            yield item
        else:
            raise TypeError("Only concrete unittest cases may run")


def _family(test) -> str | None:
    module = test.__class__.__module__
    test_id = test.id()
    if not module.startswith("tests."):
        module = "tests." + module
    if not test_id.startswith("tests."):
        test_id = "tests." + test_id
    return SECURITY_TEST_IDS.get(test_id, SECURITY_MODULES.get(module))


@dataclass(frozen=True)
class Outcome:
    tests_run: int
    failures: tuple[tuple[str, str], ...]
    skipped: int
    interrupted: bool
    security_ok: bool
    evidence: tuple[dict[str, object], ...]
    unittest_ok: bool

    @property
    def ok(self):
        return (not self.failures and self.skipped == 0 and not self.interrupted
                and self.security_ok and self.unittest_ok)


def execute_cases(cases: Iterable[unittest.TestCase], provider, *, expected_final: Snapshot,
                  approved_setup_changes: Mapping[str, frozenset[DiffToken]],
                  classifier: Callable[[unittest.TestCase], str | None] = _family,
                  evidence: Evidence | None = None, result=None) -> Outcome:
    """Run already discovered cases serially. No automatic catalog repair."""

    ordered = tuple(cases)
    if not ordered or any(not isinstance(case, unittest.TestCase) for case in ordered):
        raise ValueError("Nonempty concrete test sequence required")
    if not isinstance(expected_final, Snapshot):
        raise ValueError("Explicit reviewed final security baseline required")
    evidence = evidence or Evidence()
    sensitive = {case.id() for case in ordered if classifier(case) is not None}
    result = result if result is not None else RedactedResult(sensitive)
    originals = []
    security_ok = True
    interrupted = False
    settled = None

    def observe(phase, test_id):
        nonlocal security_ok
        try:
            return provider.snapshot()
        except Exception:
            security_ok = False
            result.stop()
            evidence.add(phase + "_snapshot_unavailable", test_id, ())
            raise RuntimeError("Security snapshot unavailable") from None

    for case in ordered:
        family = classifier(case)
        if family is None:
            continue
        test_id = case.id()
        original_setup = case.setUp
        original_method = getattr(case, case._testMethodName)
        state = {"body_baseline": None}

        @wraps(original_setup)
        def guarded_setup(*, _setup=original_setup, _state=state, _id=test_id, _family=family):
            nonlocal security_ok, settled
            before = observe("before_setup", _id)
            try:
                _setup()
            except BaseException:
                after = observe("failed_setup", _id)
                delta = before.differences(after)
                if delta:
                    security_ok = False
                    result.stop()
                    evidence.add("failed_setup_drift", _id, delta)
                raise
            after = observe("after_setup", _id)
            delta = before.differences(after)
            allowed = approved_setup_changes.get(_id, approved_setup_changes.get(_family, frozenset()))
            if delta != allowed:
                security_ok = False
                result.stop()
                evidence.add("unexpected_setup_drift", _id, delta)
                raise RuntimeError("Unapproved fixture security delta")
            if delta:
                evidence.add("approved_setup", _id, delta)
            _state["body_baseline"] = after
            settled = after
            evidence.add("method_ready", _id, ())

        @wraps(original_method)
        def guarded_method(*, _method=original_method, _state=state, _id=test_id):
            nonlocal security_ok, settled
            try:
                return _method()
            finally:
                before = _state["body_baseline"]
                if before is None:
                    security_ok = False
                    result.stop()
                    raise RuntimeError("Missing post-setUp security baseline")
                after = observe("after_body", _id)
                delta = before.differences(after)
                if delta:
                    security_ok = False
                    result.stop()
                    evidence.add("body_drift", _id, delta)
                    raise RuntimeError("Security state changed across test body")
                settled = after
                evidence.add("method_clean", _id, ())

        case.setUp = guarded_setup
        setattr(case, case._testMethodName, guarded_method)
        originals.append((case, original_setup, original_method))

    try:
        suite = unittest.TestSuite(ordered)
        result.startTestRun()
        try:
            suite.run(result)
        except BaseException:
            interrupted = True
            security_ok = False
            result.stop()
            evidence.add("interrupted", "suite", ())
        finally:
            result.stopTestRun()
        final = observe("final", "suite")
        delta = expected_final.differences(final)
        if delta:
            security_ok = False
            evidence.add("final_drift", "suite", delta)
        if settled is not None and not security_ok:
            # Preserve the last confirmed method baseline in bounded evidence.
            evidence.add("last_confirmed_method", "suite", ())
        return Outcome(result.testsRun, tuple(result.outcomes), len(result.skipped),
                       interrupted, security_ok, tuple(evidence.events),
                       result.wasSuccessful() and not result.expectedFailures)
    finally:
        for case, setup, method in originals:
            case.setUp = setup
            setattr(case, case._testMethodName, method)
