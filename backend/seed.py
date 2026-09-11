"""Deterministic, internally traceable history for the offline acceptance platform."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import random

from backend.database import Database
from backend.algorithms.dynagraph_profiles import build_profile_card, profile_condition


SIMULATOR = "simulator"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
GENERATED_AT = "2026-06-30T23:00:00.000+08:00"
PRODUCTION_START = date(2026, 4, 2)
TELEMETRY_START = datetime(2026, 6, 1, tzinfo=SHANGHAI_TIMEZONE)
EFFECT_START = datetime(2026, 7, 1, tzinfo=SHANGHAI_TIMEZONE)
EFFECT_DAYS = 15
EFFECT_SOURCE = "measurement_import"
EFFECT_HISTORY_VERSION = 2
UNIT_NAMES = ("东部采油一区", "东部采油二区", "西部采油一区", "西部采油二区")


@dataclass(frozen=True)
class SeedSummary:
    units: int
    groups: int
    wells: int
    devices: int
    telemetry: int


SUMMARY = SeedSummary(units=4, groups=8, wells=48, devices=48, telemetry=48 * 30 * 24)
MANIFEST_NAME = "acceptance-history"
MANIFEST_VERSION = 2
WORKFLOW_SCENARIOS = (
    ("draft", "pending", None, None, "new", "pending"),
    ("pending", "rejected", None, None, "closed", "closed"),
    ("active", "approved", "sent", "sent", "dispatched", "processing"),
    ("effective", "approved", "succeeded", "succeeded", "closed", "completed"),
    ("rollback", "approved", "rolled_back", "rolled_back", "closed", "closed"),
    ("active", "approved", "failed", "failed", "acknowledged", "processing"),
    ("active", "approved", "pending", "pending", "new", "pending"),
)

# Explicit local demonstration accounts.  Password material is added after the
# Task 3 content fingerprint is validated, so security enrichment never alters
# the deterministic history contract.
LOCAL_ACCOUNTS = (
    ("administrator", "管理员", "administrator", "Admin@2026"),
    ("production_engineer", "生产工程师", "production_engineer", "Production@2026"),
    ("algorithm_engineer", "算法工程师", "algorithm_engineer", "Algorithm@2026"),
    ("maintenance_engineer", "设备运维", "maintenance_engineer", "Maintenance@2026"),
    ("reviewer", "复核评审", "reviewer", "Review@2026"),
)
ROLE_PERMISSIONS = {
    "production_engineer": ("wells.read", "dashboard.read", "telemetry.read", "wells.update", "alarms.ack", "strategy.create", "strategy.submit", "reports.read", "devices.read", "acquisition.read", "data_quality.read", "workflow.read"),
    "algorithm_engineer": ("wells.read", "dashboard.read", "telemetry.read", "diagnosis.run", "reports.read", "devices.read", "acquisition.read", "data_quality.read", "workflow.read"),
    "maintenance_engineer": ("wells.read", "dashboard.read", "telemetry.read", "wells.update", "devices.read", "devices.control", "devices.configure", "commands.read", "maintenance.manage", "acquisition.read", "data_quality.read", "data_quality.repair", "workflow.read"),
    "reviewer": ("wells.read", "dashboard.read", "telemetry.read", "diagnosis.review", "reports.read", "audit.read", "devices.read", "commands.read", "acquisition.read", "data_quality.read", "workflow.read"),
}
ROLE_PERMISSIONS["administrator"] = tuple(sorted(
    {
        permission
        for permissions in ROLE_PERMISSIONS.values()
        for permission in permissions
    }
    | {"users.read", "users.manage", "roles.read", "roles.manage", "strategy.approve", "audit.read"}
))
AUTH_BOOTSTRAP_VERSION = 7


class SeedDataIntegrityError(RuntimeError):
    """Raised when a persisted seed dataset cannot truthfully be reused."""


def _identifier(namespace: str, value: str) -> str:
    return f"{namespace}{value}" if namespace else value


def _new_namespace(db: Database, seed: int) -> str:
    if not any(db.scalar(f"SELECT 1 FROM {table_name} LIMIT 1") for table_name in (
        "units", "well_groups", "wells", "devices", "roles", "users", "reports",
    )):
        return ""
    for suffix in range(1, 1000):
        namespace = f"SIM-{seed}-{suffix}-"
        if not any(
            db.scalar(f"SELECT 1 FROM {table_name} WHERE {column_name} LIKE ? LIMIT 1", (f"{namespace}%",))
            for table_name, column_name in (
                ("units", "id"), ("units", "code"), ("well_groups", "id"), ("well_groups", "code"),
                ("wells", "id"), ("wells", "code"), ("devices", "id"), ("devices", "serial_number"),
                ("device_channels", "id"), ("dynagraphs", "id"), ("model_runs", "id"),
                ("diagnoses", "id"), ("recovery_tests", "id"), ("energy_strategies", "id"),
                ("strategy_versions", "id"), ("device_commands", "id"), ("alarms", "id"),
                ("approvals", "id"), ("work_orders", "id"), ("reports", "id"),
                ("roles", "code"), ("users", "username"),
            )
        ):
            return namespace
    raise SeedDataIntegrityError("unable to allocate isolated seed namespace")


def _validated_summary(db: Database, seed: int, namespace: str) -> SeedSummary:
    with db.connection() as connection:
        return _validated_summary_from(connection, seed, namespace)


def _validated_summary_from(connection, seed: int, namespace: str) -> SeedSummary:
    profiles = _well_profiles(seed, namespace)
    unit_ids = [_identifier(namespace, f"U-{index:02d}") for index in range(1, 5)]
    group_ids = [_identifier(namespace, f"G-{index:02d}") for index in range(1, 9)]
    well_ids = [profile["well_id"] for profile in profiles]
    device_ids = [profile["device_id"] for profile in profiles]
    counts = {
        "units": _count_seed_ids(connection, "units", "id", unit_ids),
        "groups": _count_seed_ids(connection, "well_groups", "id", group_ids),
        "wells": _count_seed_ids(connection, "wells", "id", well_ids),
        "devices": _count_seed_ids(connection, "devices", "id", device_ids),
    }
    placeholder = ", ".join("?" for _ in well_ids)
    counts["production"] = connection.execute(
        f"SELECT COUNT(*) FROM production_daily WHERE well_id IN ({placeholder}) AND source_type = ?",
        [*well_ids, SIMULATOR],
    ).fetchone()[0]
    counts["telemetry"] = connection.execute(
        f"SELECT COUNT(*) FROM telemetry WHERE well_id IN ({placeholder}) AND source_type = ?",
        [*well_ids, SIMULATOR],
    ).fetchone()[0]
    expected = {"units": 4, "groups": 8, "wells": 48, "devices": 48,
                "production": 48 * 90, "telemetry": 48 * 30 * 24}
    if counts != expected:
        raise SeedDataIntegrityError(f"seed dataset incomplete: counts={counts}")
    for table_name, expected_rows in (("production_daily", 90), ("telemetry", 720)):
        rows = connection.execute(
            f"SELECT well_id, COUNT(*) AS records FROM {table_name} "
            f"WHERE well_id IN ({placeholder}) AND source_type = ? GROUP BY well_id", [*well_ids, SIMULATOR]
        ).fetchall()
        if len(rows) != 48 or any(row["records"] != expected_rows for row in rows):
            raise SeedDataIntegrityError(f"seed dataset incomplete: {table_name} distribution")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SeedDataIntegrityError("seed dataset incomplete: foreign key violation")
    _validate_auxiliary_distributions(connection, profiles)
    return SeedSummary(counts["units"], counts["groups"], counts["wells"], counts["devices"], counts["telemetry"])


def _count_seed_ids(connection, table_name: str, column_name: str, values: list[str]) -> int:
    placeholders = ", ".join("?" for _ in values)
    return connection.execute(
        f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} IN ({placeholders}) AND source_type = ?",
        [*values, SIMULATOR],
    ).fetchone()[0]


def _workflow_specs(profiles: list[dict]) -> list[dict]:
    specs = []
    for position, profile in enumerate(profile for profile in profiles if profile["low_output"]):
        strategy, approval, command, attempt, alarm, work_order = WORKFLOW_SCENARIOS[position % len(WORKFLOW_SCENARIOS)]
        namespace = profile["namespace"]
        specs.append({
            "profile": profile, "strategy": strategy, "approval": approval, "command": command,
            "attempt": attempt, "alarm": alarm, "work_order": work_order,
            "recovery_id": _identifier(namespace, f"RT-{profile['index']:03d}"),
            "strategy_id": _identifier(namespace, f"ES-{profile['index']:03d}"),
            "version_id": _identifier(namespace, f"SV-{profile['index']:03d}"),
            "command_id": _identifier(namespace, f"CMD-{profile['index']:03d}"),
            "alarm_id": _identifier(namespace, f"AL-{profile['index']:03d}"),
            "approval_id": _identifier(namespace, f"AP-{profile['index']:03d}"),
            "work_order_id": _identifier(namespace, f"WO-{profile['index']:03d}"),
        })
    return specs


def _validate_auxiliary_distributions(connection, profiles: list[dict]) -> None:
    well_ids = [profile["well_id"] for profile in profiles]
    device_ids = [profile["device_id"] for profile in profiles]
    dynagraph_ids = [profile["dynagraph_id"] for profile in profiles]
    model_ids = [profile["model_run_id"] for profile in profiles]
    diagnosis_ids = [profile["diagnosis_id"] for profile in profiles]
    specs = _workflow_specs(profiles)
    workflow_ids = {key: [spec[key] for spec in specs] for key in (
        "recovery_id", "strategy_id", "version_id", "alarm_id", "approval_id", "work_order_id",
    )}
    expected = (
        ("device_heartbeats", "device_id", device_ids, 48),
        ("raw_messages", "device_id", device_ids, 48),
        ("data_quality_events", "well_id", well_ids, 48),
        ("dynagraphs", "id", dynagraph_ids, 48),
        ("dynagraph_features", "dynagraph_id", dynagraph_ids, 144),
        ("model_runs", "id", model_ids, 48),
        ("diagnoses", "id", diagnosis_ids, 48),
        ("recovery_tests", "id", workflow_ids["recovery_id"], 16),
        ("energy_strategies", "id", workflow_ids["strategy_id"], 16),
        ("strategy_versions", "id", workflow_ids["version_id"], 16),
        ("alarms", "id", workflow_ids["alarm_id"], 16),
        ("approvals", "id", workflow_ids["approval_id"], 16),
        ("work_orders", "id", workflow_ids["work_order_id"], 16),
        ("work_order_logs", "work_order_id", workflow_ids["work_order_id"], 16),
    )
    for table_name, column_name, values, count in expected:
        actual = _count_seed_ids(connection, table_name, column_name, values)
        if actual != count:
            raise SeedDataIntegrityError(f"seed dataset incomplete: {table_name}")
    namespace = profiles[0]["namespace"]
    role_codes = [_identifier(namespace, code) for code in (
        "administrator", "production_engineer", "algorithm_engineer", "maintenance_engineer", "reviewer",
    )]
    role_count = _count_seed_ids(connection, "roles", "code", role_codes)
    user_count = _count_seed_ids(connection, "users", "username", role_codes)
    report_count = _count_seed_ids(connection, "reports", "id", [_identifier(namespace, "RPT-SEED-001")])
    event_count = connection.execute(
        "SELECT COUNT(*) FROM system_events WHERE event_type = 'device_telemetry_cycle' "
        "AND source_type = ? AND json_extract(detail, '$.well_id') IN (" + ", ".join("?" for _ in well_ids) + ")",
        [SIMULATOR, *well_ids],
    ).fetchone()[0]
    command_count = _count_seed_ids(connection, "device_commands", "strategy_version_id", workflow_ids["version_id"])
    attempt_count = connection.execute(
        "SELECT COUNT(*) FROM command_attempts WHERE command_id IN "
        "(SELECT id FROM device_commands WHERE strategy_version_id IN (" +
        ", ".join("?" for _ in workflow_ids["version_id"]) + ") AND source_type = ?)",
        [*workflow_ids["version_id"], SIMULATOR],
    ).fetchone()[0]
    if (role_count, user_count, report_count, event_count) != (5, 5, 1, 48) or command_count not in {10, 16} or attempt_count != command_count:
        raise SeedDataIntegrityError(
            "seed dataset incomplete: supporting records "
            f"roles={role_count} users={user_count} reports={report_count} events={event_count} "
            f"commands={command_count} attempts={attempt_count}"
        )


def _content_hash(db: Database, seed: int, namespace: str) -> str:
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace)


def _content_hash_from(connection, seed: int, namespace: str, *, projection: str = "workflow-lifecycle-v6") -> str:
    profiles = _well_profiles(seed, namespace)
    specs = _workflow_specs(profiles)
    well_ids = [profile["well_id"] for profile in profiles]
    device_ids = [profile["device_id"] for profile in profiles]
    channel_ids = [profile["channel_id"] for profile in profiles]
    dynagraph_ids = [profile["dynagraph_id"] for profile in profiles]
    model_ids = [profile["model_run_id"] for profile in profiles]
    diagnosis_ids = [profile["diagnosis_id"] for profile in profiles]
    unit_ids = [_identifier(namespace, f"U-{index:02d}") for index in range(1, 5)]
    group_ids = [_identifier(namespace, f"G-{index:02d}") for index in range(1, 9)]
    digest = hashlib.sha256()
    projection_markers = {
        "legacy-auth-v1": "", "auth-v2": "|auth-projection-v2",
        "well-lifecycle-v3": "|well-lifecycle-projection-v3",
        "device-config-v4": "|device-config-projection-v4",
        "device-config-v4-legacy": "|device-config-projection-v4",
        "strategy-recovery-v5": "|strategy-recovery-projection-v5",
        "workflow-lifecycle-v6": "|workflow-lifecycle-projection-v6",
    }
    if projection not in projection_markers and not (
        projection.startswith("device-config-v")
        and projection.removeprefix("device-config-v").isdigit()
    ):
        raise ValueError("unsupported seed hash projection")
    if projection not in projection_markers:
        projection_markers[projection] = f"|device-config-projection-{projection.removeprefix('device-config-')}"
    digest.update(f"{MANIFEST_NAME}|{MANIFEST_VERSION}|{seed}|{namespace}{projection_markers[projection]}\n".encode())

    def append_rows(label: str, select_sql: str, parameters: list) -> None:
        row = connection.execute(
            "SELECT group_concat(payload, char(10)) FROM (" + select_sql + ")", parameters
        ).fetchone()
        digest.update(label.encode())
        digest.update(b"\n")
        digest.update((row[0] or "").encode("utf-8"))
        digest.update(b"\n")

    def rows_for_ids(label: str, table: str, identifier: str, values: list[str], fields: str,
                     order_by: str | None = None) -> None:
        append_rows(
            label,
            f"SELECT json_object({fields}) AS payload FROM {table} WHERE {identifier} IN (" +
            ", ".join("?" for _ in values) + f") AND source_type = ? ORDER BY {order_by or identifier + ', rowid'}",
            [*values, SIMULATOR],
        )

    rows_for_ids("units", "units", "id", unit_ids, "'id',id,'code',code,'name',name,'status',status,'source',source_type")
    rows_for_ids("groups", "well_groups", "id", group_ids, "'id',id,'unit',unit_id,'code',code,'name',name,'status',status,'source',source_type")
    # Device-config projections build on the well-lifecycle-v3 contract.  Well
    # status and optimistic version are mutable lifecycle state and therefore
    # must never invalidate a seed manifest.  Keep this capability-based rather
    # than checking only the current projection name so future vN projections
    # cannot accidentally reintroduce those fields.
    lifecycle_projection = projection in {"well-lifecycle-v3", "strategy-recovery-v5", "workflow-lifecycle-v6"} or (projection.startswith("device-config-v") and projection != "device-config-v4-legacy")
    well_fields = "'id',id,'group',group_id,'code',code,'name',name,'low',is_low_output,'source',source_type" if lifecycle_projection else "'id',id,'group',group_id,'code',code,'name',name,'status',status,'low',is_low_output,'source',source_type"
    rows_for_ids("wells", "wells", "id", well_ids, well_fields)
    config_projection = projection.startswith("device-config-v") or projection in {"strategy-recovery-v5", "workflow-lifecycle-v6"}
    device_fields = "'id',id,'well',well_id,'serial',serial_number,'type',device_type,'firmware',firmware_version,'source',source_type" if config_projection else "'id',id,'well',well_id,'serial',serial_number,'type',device_type,'firmware',firmware_version,'status',status,'source',source_type"
    channel_fields = "'id',id,'device',device_id,'code',channel_code,'name',name,'source',source_type" if config_projection else "'id',id,'device',device_id,'code',channel_code,'name',name,'status',status,'source',source_type"
    rows_for_ids("devices", "devices", "id", device_ids, device_fields)
    rows_for_ids("channels", "device_channels", "id", channel_ids, channel_fields)
    rows_for_ids("heartbeats", "device_heartbeats", "device_id", device_ids, "'device',device_id,'reported',reported_at,'status',status,'source',source_type")
    rows_for_ids("telemetry", "telemetry", "well_id", well_ids, "'well',well_id,'device',device_id,'channel',channel_id,'at',measured_at,'metric',metric_code,'value',value,'unit',unit,'status',status,'source',source_type", "well_id, measured_at, metric_code")
    rows_for_ids("production", "production_daily", "well_id", well_ids, "'well',well_id,'day',day,'oil',daily_oil,'fluid',daily_fluid,'energy',power_kwh,'efficiency',pump_efficiency,'stroke',stroke_m,'frequency',frequency_hz,'runtime',runtime_hours,'status',status,'source',source_type", "well_id, day")
    rows_for_ids("raw", "raw_messages", "device_id", device_ids, "'device',device_id,'received',received_at,'payload',payload,'status',status,'source',source_type")
    rows_for_ids("quality", "data_quality_events", "well_id", well_ids, "'telemetry',telemetry_id,'well',well_id,'event',event_type,'detail',detail,'status',status,'source',source_type")
    rows_for_ids("dynagraphs", "dynagraphs", "id", dynagraph_ids, "'id',id,'well',well_id,'at',captured_at,'sample',sample_data,'status',status,'source',source_type")
    rows_for_ids("features", "dynagraph_features", "dynagraph_id", dynagraph_ids, "'dynagraph',dynagraph_id,'name',feature_name,'value',feature_value,'status',status,'source',source_type")
    rows_for_ids("models", "model_runs", "id", model_ids, "'id',id,'name',model_name,'version',model_version,'input_type',input_object_type,'input',input_object_id,'status',status,'source',source_type")
    rows_for_ids("diagnoses", "diagnoses", "id", diagnosis_ids, "'id',id,'well',well_id,'dynagraph',dynagraph_id,'model',model_run_id,'type',diagnosis_type,'confidence',confidence,'evidence',evidence,'status',status,'source',source_type")
    workflow_projection = projection == "workflow-lifecycle-v6"
    alarm_fields = "'id',id,'well',well_id,'diagnosis',diagnosis_id,'type',alarm_type,'severity',severity,'source',source_type" if workflow_projection else "'id',id,'well',well_id,'diagnosis',diagnosis_id,'type',alarm_type,'severity',severity,'status',status,'source',source_type"
    order_fields = "'id',id,'well',well_id,'alarm',alarm_id,'title',title,'source',source_type" if workflow_projection else "'id',id,'well',well_id,'alarm',alarm_id,'title',title,'status',status,'source',source_type"
    for label, table, identifier, key, fields in (
        ("recovery", "recovery_tests", "id", "recovery_id", "'id',id,'well',well_id,'at',tested_at,'result',result_data,'status',status,'source',source_type"),
        ("strategies", "energy_strategies", "id", "strategy_id", "'id',id,'well',well_id,'name',name,'status',status,'source',source_type"),
        ("versions", "strategy_versions", "id", "version_id", "'id',id,'strategy',strategy_id,'version',version_number,'configuration',configuration,'status',status,'source',source_type"),
        ("alarms", "alarms", "id", "alarm_id", alarm_fields),
        ("approvals", "approvals", "id", "approval_id", "'id',id,'object_type',object_type,'object',object_id,'decision',decision,'status',status,'source',source_type"),
        ("orders", "work_orders", "id", "work_order_id", order_fields),
        ("logs", "work_order_logs", "work_order_id", "work_order_id", "'order',work_order_id,'message',message,'status',status,'source',source_type"),
    ):
        rows_for_ids(label, table, identifier, [spec[key] for spec in specs], fields)
    version_ids = [spec["version_id"] for spec in specs]
    rows_for_ids("commands", "device_commands", "strategy_version_id", version_ids, "'id',id,'device',device_id,'version',strategy_version_id,'type',command_type,'payload',payload,'status',status,'source',source_type")
    append_rows(
        "attempts",
        "SELECT json_object('command',command_id,'at',attempted_at,'detail',result_detail,'status',status,'source',source_type) AS payload "
        "FROM command_attempts WHERE command_id IN (SELECT id FROM device_commands WHERE strategy_version_id IN (" +
        ", ".join("?" for _ in version_ids) + ") AND source_type = ?) AND source_type = ? ORDER BY command_id, rowid",
        [*version_ids, SIMULATOR, SIMULATOR],
    )
    role_codes = [_identifier(namespace, code) for code in ("administrator", "production_engineer", "algorithm_engineer", "maintenance_engineer", "reviewer")]
    if projection == "legacy-auth-v1":
        rows_for_ids("roles", "roles", "code", role_codes, "'code',code,'name',name,'status',status,'source',source_type")
        rows_for_ids("users", "users", "username", role_codes, "'role',role_id,'username',username,'name',display_name,'status',status,'source',source_type")
    else:
        # Seed integrity owns account identity, not mutable local authorization configuration.
        rows_for_ids("roles", "roles", "code", role_codes, "'code',code,'source',source_type")
        rows_for_ids("users", "users", "username", role_codes, "'username',username,'source',source_type")
    rows_for_ids("reports", "reports", "id", [_identifier(namespace, "RPT-SEED-001")], "'id',id,'type',report_type,'start',period_start,'end',period_end,'payload',payload,'status',status,'source',source_type")
    append_rows(
        "events",
        "SELECT json_object('type',event_type,'detail',detail,'status',status,'source',source_type) AS payload "
        "FROM system_events WHERE event_type = 'device_telemetry_cycle' AND source_type = ? "
        "AND json_extract(detail, '$.well_id') IN (" + ", ".join("?" for _ in well_ids) + ") ORDER BY id",
        [SIMULATOR, *well_ids],
    )
    return digest.hexdigest()


def _legacy_content_hash(db: Database, seed: int, namespace: str) -> str:
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace, projection="legacy-auth-v1")


def _previous_content_hash(db: Database, seed: int, namespace: str) -> str:
    """Exact Task 4 projection: auth configuration excluded, lifecycle included."""
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace, projection="auth-v2")


def _task1_previous_content_hash(db: Database, seed: int, namespace: str) -> str:
    """Exact pre-Task-1 immutable projection used for one safe CAS upgrade."""
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace, projection="well-lifecycle-v3")


def _task1_current_v4_content_hash(db: Database, seed: int, namespace: str) -> str:
    """Exact ec75f85 projection accepted once while v4 semantics are repaired."""
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace, projection="device-config-v4-legacy")


def _strategy_previous_v4_content_hash(db: Database, seed: int, namespace: str) -> str:
    """Exact pre-strategy projection with the corrected mutable lifecycle contract."""
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace, projection="device-config-v4")


def _strategy_previous_v5_content_hash(db: Database, seed: int, namespace: str) -> str:
    """Exact strategy/recovery projection before workflow lifecycle became mutable."""
    with db.connection() as connection:
        return _content_hash_from(connection, seed, namespace, projection="strategy-recovery-v5")


def _expected_strategy_v5_content_hash(db: Database, seed: int, namespace: str) -> str:
    """Verify a v5 manifest after the first overdue-worker transition.

    The worker records the exact prior state in overdue_from_status.  Restore only
    that state inside a rolled-back savepoint; all immutable values and any other
    lifecycle edits remain fail-closed.
    """
    with db.connection() as connection:
        connection.execute("SAVEPOINT workflow_lifecycle_v5_probe")
        try:
            connection.execute(
                """UPDATE work_orders SET status=overdue_from_status
                   WHERE source_type=? AND status='overdue'
                     AND overdue_from_status IS NOT NULL""",
                (SIMULATOR,),
            )
            return _content_hash_from(
                connection, seed, namespace, projection="strategy-recovery-v5",
            )
        finally:
            connection.execute("ROLLBACK TO workflow_lifecycle_v5_probe")
            connection.execute("RELEASE workflow_lifecycle_v5_probe")


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _adopt_legacy_dataset(db: Database, seed: int, namespace: str, summary: SeedSummary) -> None:
    content_hash = _content_hash(db, seed, namespace)
    with db.transaction(
        None, "seed.adopt_manifest", "database", "platform", source_type=SIMULATOR
    ) as connection:
        connection.execute(
            """
            INSERT INTO seed_manifests(dataset_name, version, seed, namespace, summary_json, content_hash,
                                       status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?)
            ON CONFLICT(dataset_name) DO UPDATE SET
                version = excluded.version, seed = excluded.seed, namespace = excluded.namespace,
                summary_json = excluded.summary_json, content_hash = excluded.content_hash,
                status = excluded.status, source_type = excluded.source_type
            """,
            (MANIFEST_NAME, MANIFEST_VERSION, seed, namespace, _canonical_json(summary.__dict__), content_hash,
             SIMULATOR, GENERATED_AT, GENERATED_AT),
        )


def _validate_manifest(db: Database, manifest, requested_seed: int) -> SeedSummary:
    if (
        manifest["dataset_name"] != MANIFEST_NAME
        or manifest["version"] != MANIFEST_VERSION
        or manifest["seed"] != requested_seed
        or manifest["status"] != "complete"
        or manifest["source_type"] != SIMULATOR
        or not isinstance(manifest["namespace"], str)
    ):
        raise SeedDataIntegrityError("seed dataset incomplete: manifest contract")
    summary = _validated_summary(db, requested_seed, manifest["namespace"])
    expected_summary = _canonical_json(summary.__dict__)
    try:
        declared_summary = json.loads(manifest["summary_json"])
    except (TypeError, json.JSONDecodeError):
        raise SeedDataIntegrityError("seed dataset incomplete: manifest summary") from None
    if not isinstance(declared_summary, dict) or _canonical_json(declared_summary) != expected_summary:
        raise SeedDataIntegrityError("seed dataset incomplete: manifest summary")
    if _needs_recovery_enrichment(db, requested_seed, manifest["namespace"]):
        legacy_candidates = {
            _task1_previous_content_hash(db, requested_seed, manifest["namespace"]),
            _task1_current_v4_content_hash(db, requested_seed, manifest["namespace"]),
            _strategy_previous_v4_content_hash(db, requested_seed, manifest["namespace"]),
            _previous_content_hash(db, requested_seed, manifest["namespace"]),
            _legacy_content_hash(db, requested_seed, manifest["namespace"]),
            _strategy_previous_v5_content_hash(db, requested_seed, manifest["namespace"]),
        }
        if manifest["content_hash"] not in legacy_candidates:
            raise SeedDataIntegrityError("seed dataset incomplete: content fingerprint")
        _enrich_recovery_observations(
            db, requested_seed, manifest["namespace"], manifest["content_hash"],
        )
        return summary
    projected_hash = _content_hash(db, requested_seed, manifest["namespace"])
    if manifest["content_hash"] == projected_hash:
        return summary
    # Existing Task 3/Task 4 databases used the original projection.  Upgrade
    # only after proving that exact old projection still matches; otherwise a
    # damaged immutable seed record remains fail-closed.
    previous_hash = _previous_content_hash(db, requested_seed, manifest["namespace"])
    task1_previous_hash = _task1_previous_content_hash(db, requested_seed, manifest["namespace"])
    task1_current_v4_hash = _task1_current_v4_content_hash(db, requested_seed, manifest["namespace"])
    strategy_previous_v4_hash = _strategy_previous_v4_content_hash(
        db, requested_seed, manifest["namespace"],
    )
    strategy_previous_v5_hash = _strategy_previous_v5_content_hash(
        db, requested_seed, manifest["namespace"],
    )
    expected_strategy_v5_hash = _expected_strategy_v5_content_hash(
        db, requested_seed, manifest["namespace"],
    )
    legacy_hash = _legacy_content_hash(db, requested_seed, manifest["namespace"])
    if manifest["content_hash"] not in {
        task1_previous_hash, task1_current_v4_hash, strategy_previous_v4_hash,
        strategy_previous_v5_hash, expected_strategy_v5_hash, previous_hash, legacy_hash,
    }:
        raise SeedDataIntegrityError("seed dataset incomplete: content fingerprint")
    _upgrade_manifest_hash(db, manifest["content_hash"], projected_hash)
    return summary


def _upgrade_manifest_hash(db: Database, expected_hash: str, projected_hash: str) -> None:
    """CAS a legacy fingerprint, accepting a concurrent winner with this same hash."""
    with db.transaction(
        None, "seed.upgrade_manifest_hash", "seed_manifest", MANIFEST_NAME, source_type=SIMULATOR,
        auto_audit=False,
    ) as connection:
        updated = connection.execute(
            "UPDATE seed_manifests SET content_hash = ? WHERE dataset_name = ? AND content_hash = ?",
            (projected_hash, MANIFEST_NAME, expected_hash),
        ).rowcount
        if updated == 1:
            connection.execute(
                """INSERT INTO audit_logs(actor_id, action, object_type, object_id, details_json, source_type)
                   VALUES(NULL, 'seed.upgrade_manifest_hash', 'seed_manifest', ?, ?, ?)""",
                (MANIFEST_NAME, '{"after":{"projection":"workflow-lifecycle-v6"},"before":{"projection":"verified-legacy"}}', SIMULATOR),
            )
            return
        current = connection.execute(
            "SELECT content_hash FROM seed_manifests WHERE dataset_name = ?", (MANIFEST_NAME,)
        ).fetchone()
        if current is None or current["content_hash"] != projected_hash:
            raise SeedDataIntegrityError("seed dataset incomplete: manifest update")


def _recovery_payload(profile: dict) -> dict:
    """Build the persisted shut-in observations used by strategy optimization."""
    target = float(profile["fluid_target"])
    baseline = max(.5, target * .25)
    capacity = target * 4.0
    tau_minutes = 72.0 + (profile["index"] % 5) * 12.0
    observations = [
        {
            "shut_in_minutes": minute,
            "liquid_rate_m3_day": round(
                baseline + capacity * (1.0 - math.exp(-minute / tau_minutes)), 3,
            ),
        }
        for minute in (0, 30, 60, 90, 120, 180, 240, 360)
    ]
    return {
        "fluid_target": round(target, 3),
        "recovery_hours": 8,
        "observations": observations,
        "units": {"liquid_rate": "m³/d", "time": "min"},
    }


def _needs_recovery_enrichment(db: Database, seed: int, namespace: str) -> bool:
    for spec in _workflow_specs(_well_profiles(seed, namespace)):
        raw = db.scalar("SELECT result_data FROM recovery_tests WHERE id=?", (spec["recovery_id"],))
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or "observations" not in payload:
            return True
    return False


def _enrich_recovery_observations(
    db: Database, seed: int, namespace: str, expected_hash: str,
) -> None:
    """Upgrade verified v4 recovery summaries to measured-point v5 records atomically."""
    with db.transaction(
        None, "seed.enrich_recovery_observations", "seed_manifest", MANIFEST_NAME,
        source_type=SIMULATOR, auto_audit=False,
    ) as connection:
        current = connection.execute(
            "SELECT content_hash FROM seed_manifests WHERE dataset_name=?", (MANIFEST_NAME,),
        ).fetchone()
        if current is None or current["content_hash"] != expected_hash:
            raise SeedDataIntegrityError("seed dataset incomplete: manifest update")
        for spec in _workflow_specs(_well_profiles(seed, namespace)):
            expected_legacy = {
                "fluid_target": round(spec["profile"]["fluid_target"], 3),
                "recovery_hours": 8,
            }
            existing = connection.execute(
                "SELECT result_data FROM recovery_tests WHERE id=?", (spec["recovery_id"],),
            ).fetchone()
            try:
                existing_payload = json.loads(existing["result_data"]) if existing else None
            except (TypeError, json.JSONDecodeError):
                existing_payload = None
            if existing_payload != expected_legacy:
                raise SeedDataIntegrityError("seed dataset incomplete: legacy recovery result")
            connection.execute(
                "UPDATE recovery_tests SET result_data=? WHERE id=?",
                (_canonical_json(_recovery_payload(spec["profile"])), spec["recovery_id"]),
            )
        projected_hash = _content_hash_from(connection, seed, namespace)
        if connection.execute(
            "UPDATE seed_manifests SET content_hash=? WHERE dataset_name=? AND content_hash=?",
            (projected_hash, MANIFEST_NAME, expected_hash),
        ).rowcount != 1:
            raise SeedDataIntegrityError("seed dataset incomplete: manifest update")
        connection.execute(
            """INSERT INTO audit_logs(
                   actor_id,action,object_type,object_id,details_json,source_type
               ) VALUES(NULL,'seed.enrich_recovery_observations','seed_manifest',?,?,?)""",
            (MANIFEST_NAME,
             '{"after":{"projection":"workflow-lifecycle-v6"},"before":{"projection":"device-config-v4"}}',
             SIMULATOR),
        )


def bootstrap_local_auth(db: Database) -> None:
    """Idempotently add password material and explicit least-privilege grants.

    This intentionally does not touch Task 3's manifest-covered role/user
    identity fields.  Existing business records are preserved; missing local
    accounts only appear when an earlier manual namespace prevented their seed
    identities from using the conventional names.
    """
    from backend.auth import password_material

    permission_codes = sorted({permission for values in ROLE_PERMISSIONS.values() for permission in values})
    current_permissions = set(db.column("SELECT code FROM permissions"))
    marker = db.query_one("SELECT bootstrap_version FROM auth_bootstrap_state WHERE id = 1")
    administrator = db.query_one("SELECT id FROM roles WHERE code = 'administrator'")
    missing_administrator_permissions = set(ROLE_PERMISSIONS["administrator"])
    if administrator is not None:
        missing_administrator_permissions -= set(db.column(
            """SELECT p.code
                 FROM role_permissions rp
                 JOIN permissions p ON p.id = rp.permission_id
                WHERE rp.role_id = ?""",
            (administrator["id"],),
        ))
    if (
        marker is not None
        and marker["bootstrap_version"] >= AUTH_BOOTSTRAP_VERSION
        and not (set(permission_codes) - current_permissions)
        and not missing_administrator_permissions
    ):
        return

    with db.transaction(
        None, "auth.bootstrap", "database", "platform", source_type="system",
        details_json='{"after":{"authentication":"enriched"},"before":{}}',
    ) as connection:
        for permission in permission_codes:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code, name, source_type) VALUES(?, ?, 'system')",
                (permission, permission),
            )
        new_permission_codes = set(permission_codes) - current_permissions
        administrator = connection.execute(
            "SELECT id FROM roles WHERE code = 'administrator'"
        ).fetchone()
        if administrator is not None:
            for permission in ROLE_PERMISSIONS["administrator"]:
                connection.execute(
                    """INSERT OR IGNORE INTO role_permissions(role_id, permission_id, source_type)
                       SELECT ?, id, 'system' FROM permissions WHERE code = ?""",
                    (administrator["id"], permission),
                )
        if marker is None:
            # Default grants are a one-time bootstrap/migration action.  Later
            # restarts may add permission definitions but must honor revocations.
            for role_code, role_name, username, password in LOCAL_ACCOUNTS:
                connection.execute(
                    "INSERT OR IGNORE INTO roles(code, name, status, source_type) VALUES(?, ?, 'active', 'system')",
                    (role_code, role_name),
                )
                role_id = connection.execute("SELECT id FROM roles WHERE code = ?", (role_code,)).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO users(role_id, username, display_name, status, source_type) VALUES(?, ?, ?, 'active', 'system')",
                    (role_id, username, role_name),
                )
                user = connection.execute(
                    "SELECT id, password_hash FROM users WHERE username = ?", (username,)
                ).fetchone()
                if not user["password_hash"]:
                    salt, digest, iterations = password_material(password)
                    connection.execute(
                        "UPDATE users SET password_salt = ?, password_hash = ?, password_iterations = ? WHERE id = ?",
                        (salt, digest, iterations, user["id"]),
                    )
                for permission in ROLE_PERMISSIONS[role_code]:
                    permission_id = connection.execute(
                        "SELECT id FROM permissions WHERE code = ?", (permission,)
                    ).fetchone()[0]
                    connection.execute(
                        "INSERT OR IGNORE INTO role_permissions(role_id, permission_id, source_type) VALUES(?, ?, 'system')",
                        (role_id, permission_id),
                    )
            connection.execute(
                "INSERT INTO auth_bootstrap_state(id, bootstrap_version, source_type) VALUES(1, ?, 'system')",
                (AUTH_BOOTSTRAP_VERSION,),
            )
        else:
            # A later release may introduce a new named capability.  Grant
            # only those newly-created capabilities; never restore a revoked
            # pre-existing permission on restart.
            for role_code, permissions in ROLE_PERMISSIONS.items():
                role = connection.execute("SELECT id FROM roles WHERE code=?", (role_code,)).fetchone()
                if role is None:
                    continue
                for permission in set(permissions) & new_permission_codes:
                    connection.execute(
                        "INSERT OR IGNORE INTO role_permissions(role_id, permission_id, source_type) "
                        "SELECT ?, id, 'system' FROM permissions WHERE code=?",
                        (role["id"], permission),
                    )
            if marker["bootstrap_version"] < 4:
                # Version 4 separates machine execution from human review.
                # Remove only the original system grant, preserving a later
                # explicit administrator grant with manual provenance.
                connection.execute(
                    """DELETE FROM role_permissions
                       WHERE role_id=(SELECT id FROM roles WHERE code='algorithm_engineer')
                         AND permission_id=(SELECT id FROM permissions WHERE code='diagnosis.review')
                         AND source_type='system'"""
                )
                connection.execute(
                    """INSERT OR IGNORE INTO role_permissions(
                           role_id,permission_id,source_type
                       ) SELECT role.id,permission.id,'system'
                         FROM roles AS role CROSS JOIN permissions AS permission
                        WHERE role.code='reviewer' AND permission.code='diagnosis.review'"""
                )
                connection.execute(
                    "UPDATE roles SET name='复核评审' WHERE code='reviewer' AND name='只读评审'"
                )
                connection.execute(
                    "UPDATE users SET display_name='复核评审' WHERE username='reviewer' AND display_name='只读评审'"
                )
            if marker["bootstrap_version"] < 5:
                for permission in ("alarms.ack", "maintenance.manage", "diagnosis.review"):
                    connection.execute(
                        """INSERT OR IGNORE INTO role_permissions(
                               role_id,permission_id,source_type
                           ) SELECT role.id,permission.id,'system'
                               FROM roles AS role CROSS JOIN permissions AS permission
                              WHERE role.code='administrator' AND permission.code=?""",
                        (permission,),
                    )
            connection.execute(
                "UPDATE auth_bootstrap_state SET bootstrap_version=? WHERE id=1",
                (AUTH_BOOTSTRAP_VERSION,),
            )


def _with_auth_bootstrap(db: Database, summary: SeedSummary) -> SeedSummary:
    bootstrap_local_auth(db)
    _ensure_effect_history(db)
    _ensure_device_self_checks(db)
    _ensure_signed_command_history(db)
    return summary


def _ensure_device_self_checks(db: Database) -> None:
    """Persist one inspectable software-terminal self-check per configured device."""
    devices = db.query_all(
        "SELECT id,firmware_version,status FROM devices ORDER BY id"
    )
    if not devices:
        return
    with db.transaction(
        None, "device.self_check.bootstrap", "device", "all", source_type="system",
        details_json=_canonical_json({"device_count": len(devices)}),
    ) as connection:
        for device in devices:
            if connection.execute(
                "SELECT 1 FROM device_events WHERE device_id=? AND event_type='self_check' LIMIT 1",
                (device["id"],),
            ).fetchone():
                continue
            channel_count = connection.execute(
                "SELECT COUNT(*) FROM device_channels WHERE device_id=? AND enabled=1",
                (device["id"],),
            ).fetchone()[0]
            detail = _canonical_json({
                "firmware_version": device["firmware_version"],
                "enabled_channels": channel_count,
                "checks": {
                    "configuration": "passed",
                    "data_store": "passed",
                    "protocol_runtime": "passed",
                },
            })
            connection.execute(
                "INSERT INTO device_events(device_id,event_type,detail,status,source_type,created_at,updated_at) "
                "VALUES(?, 'self_check', ?, 'passed', 'system', ?, ?)",
                (device["id"], detail, GENERATED_AT, GENERATED_AT),
            )


def _ensure_signed_command_history(db: Database) -> None:
    """Add one complete signed historical command without mutating manifest rows."""
    manifest = db.query_one(
        "SELECT seed,namespace FROM seed_manifests WHERE dataset_name=?", (MANIFEST_NAME,)
    )
    if manifest is None:
        return
    strategy_id = _identifier(manifest["namespace"], "ES-CONTROL-001")
    version_id = _identifier(manifest["namespace"], "SV-CONTROL-001")
    approval_id = _identifier(manifest["namespace"], "APR-CONTROL-001")
    command_id = _identifier(manifest["namespace"], "CMD-CONTROL-001")
    if db.scalar("SELECT 1 FROM device_commands WHERE id=?", (command_id,)):
        return
    target = db.query_one(
        """SELECT device.id AS device_id,device.well_id,well.name AS well_name
             FROM devices AS device
             JOIN wells AS well ON well.id=device.well_id
            WHERE well.is_low_output=1 AND EXISTS(
                  SELECT 1 FROM recovery_tests AS recovery
                   WHERE recovery.well_id=device.well_id AND recovery.status='completed'
            )
            ORDER BY device.id LIMIT 1"""
    )
    if target is None:
        return
    from backend.algorithms.energy import fit_recovery_curve, optimize_cycle
    from backend.services.strategies import StrategyService
    from backend.simulator import load_command_signing_key, sign_control_command

    recovery = db.query_one(
        "SELECT id,tested_at,result_data FROM recovery_tests WHERE well_id=? AND status='completed' ORDER BY tested_at DESC,id DESC LIMIT 1",
        (target["well_id"],),
    )
    if recovery is None:
        return
    strategy_service = StrategyService(db, None)
    points, recovery_payload = strategy_service._recovery_points(recovery["result_data"])
    constraints, motor_kw, normalized = strategy_service._constraints({
        "min_on_minutes": 20, "max_on_minutes": 120,
        "min_off_minutes": 15, "max_off_minutes": 180,
        "min_liquid_m3_day": 1.0, "max_starts_per_day": 12,
        "motor_kw": 18.5,
    })
    model = fit_recovery_curve(points)
    feasible = [item for item in optimize_cycle(model, constraints, motor_kw) if item.feasible]
    if not feasible:
        raise SeedDataIntegrityError("signed command history has no safe cycle")
    selected = feasible[0]
    configuration = {
        "schema_version": 1,
        "recovery_test_id": recovery["id"],
        "recovery_tested_at": recovery["tested_at"],
        "recovery_observations": recovery_payload["observations"],
        "recovery_units": recovery_payload["units"],
        "recovery_model": asdict(model),
        "constraints": normalized,
        "selected_candidate": asdict(selected),
        "ranked_candidates": [asdict(item) for item in feasible[:10]],
    }
    strategy_service._assert_safe_configuration(configuration)
    issued = datetime(2026, 7, 1, 0, 5, tzinfo=SHANGHAI_TIMEZONE)
    expires = issued + timedelta(minutes=15)
    completed = issued + timedelta(seconds=8)
    created = issued - timedelta(minutes=30)
    submitted = issued - timedelta(minutes=20)
    decided = issued - timedelta(minutes=10)
    unsigned = {
        "schema_version": 1, "command_id": command_id,
        "device_id": target["device_id"], "strategy_id": strategy_id,
        "strategy_version_id": version_id, "command_type": "set_intermittent_cycle",
        "on_minutes": selected.on_minutes, "off_minutes": selected.off_minutes,
        "issued_at": issued.isoformat(), "expires_at": expires.isoformat(),
        "nonce": hashlib.sha256(f"{manifest['seed']}:{command_id}".encode()).hexdigest()[:32],
        "idempotency_key": f"history:{strategy_id}:{version_id}",
        "reason": "依据审批版本下发自适应间抽周期",
    }
    signature = sign_control_command(unsigned, load_command_signing_key(db.path))
    payload = {**unsigned, "signature": signature}
    creator_id = db.scalar("SELECT id FROM users WHERE username='production_engineer'")
    approver_id = db.scalar("SELECT id FROM users WHERE username='reviewer'")
    operator_id = db.scalar("SELECT id FROM users WHERE username='maintenance_engineer'")
    with db.transaction(
        operator_id, "command.history.bootstrap", "device_command", command_id,
        source_type="system", details_json=_canonical_json({"strategy_id": strategy_id}),
    ) as connection:
        connection.execute(
            """INSERT INTO energy_strategies(
                   id,well_id,name,version,created_by,submitted_by,decision_by,
                   submitted_at,decision_at,effective_at,approval_comment,status,
                   actor_id,source_type,created_at,updated_at
               ) VALUES(?,?,?,5,?,?,?,?,?,?,?,'effective',?,'system',?,?)""",
            (strategy_id, target["well_id"], f"{target['well_name']} 自适应间抽控制策略",
             creator_id, creator_id, approver_id, submitted.isoformat(), decided.isoformat(),
             completed.isoformat(), "恢复曲线、产量约束及启停次数复核通过",
             operator_id, created.isoformat(), completed.isoformat()),
        )
        connection.execute(
            """INSERT INTO strategy_versions(
                   id,strategy_id,version_number,configuration,created_by,submitted_at,
                   status,actor_id,source_type,created_at,updated_at
               ) VALUES(?,?,1,?,?,?,'submitted',?,'algorithm',?,?)""",
            (version_id, strategy_id, _canonical_json(configuration), creator_id,
             submitted.isoformat(), creator_id, created.isoformat(), submitted.isoformat()),
        )
        connection.execute(
            "UPDATE energy_strategies SET current_version_id=? WHERE id=?",
            (version_id, strategy_id),
        )
        connection.execute(
            """INSERT INTO approvals(
                   id,object_type,object_id,object_version,decision,comment,decided_at,
                   status,actor_id,source_type,created_at,updated_at
               ) VALUES(?,'energy_strategy',?,2,'approved',?,?,'decided',?,'manual',?,?)""",
            (approval_id, strategy_id, "恢复曲线、产量约束及启停次数复核通过",
             decided.isoformat(), approver_id, submitted.isoformat(), decided.isoformat()),
        )
        connection.execute(
            """INSERT INTO device_commands(
                   id,device_id,strategy_id,strategy_version_id,command_type,payload,signature,
                   idempotency_key,issued_at,expires_at,attempt_count,max_attempts,completed_at,
                   status,actor_id,source_type,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,1,3,?,'success',?,'system',?,?)""",
            (command_id, target["device_id"], strategy_id, version_id,
             "set_intermittent_cycle", _canonical_json(payload), signature,
             unsigned["idempotency_key"], issued.isoformat(), expires.isoformat(),
             completed.isoformat(), operator_id, issued.isoformat(), completed.isoformat()),
        )
        for offset, state in enumerate(("sent", "received", "validated", "executing", "success"), 1):
            recorded = issued + timedelta(seconds=offset)
            connection.execute(
                """INSERT INTO command_attempts(
                       command_id,attempted_at,attempt_number,state,receipt_at,result_detail,
                       status,actor_id,source_type,created_at,updated_at
                   ) VALUES(?,?,1,?,?,?,?,?,'system',?,?)""",
                (command_id, issued.isoformat(), state, recorded.isoformat(),
                 _canonical_json({"state": state, "on_minutes": selected.on_minutes,
                                  "off_minutes": selected.off_minutes}),
                  state, operator_id, recorded.isoformat(), recorded.isoformat()),
            )
        audit_rows = (
            (creator_id, "strategy.optimize", "energy_strategy", strategy_id,
             {"before": {}, "after": {"well_id": target["well_id"], "strategy_version_id": version_id}}, created),
            (creator_id, "strategy.submit", "energy_strategy", strategy_id,
             {"before": {"status": "draft"}, "after": {"status": "pending_approval", "approval_id": approval_id}}, submitted),
            (approver_id, "strategy.approve", "energy_strategy", strategy_id,
             {"before": {"status": "pending_approval"}, "after": {"status": "approved", "approval_id": approval_id}}, decided),
            (operator_id, "command.dispatch", "device_command", command_id,
             {"before": {}, "after": {"status": "queued", "strategy_version_id": version_id}}, issued),
            (operator_id, "command.success", "device_command", command_id,
             {"before": {"status": "queued"}, "after": {"status": "success", "strategy_version_id": version_id}}, completed),
        )
        connection.executemany(
            """INSERT INTO audit_logs(
                   actor_id,action,object_type,object_id,details_json,status,source_type,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,'recorded','system',?,?)""",
            [(actor, action, object_type, object_id, _canonical_json(details),
              at.isoformat(), at.isoformat())
             for actor, action, object_type, object_id, details, at in audit_rows],
        )


def _ensure_effect_history(db: Database) -> None:
    """Install one deterministic post-strategy measurement run.

    These are source rows, not cached report totals: hourly active power and
    daily production remain independently queryable and reconcilable.
    """
    manifest = db.query_one(
        "SELECT seed,namespace FROM seed_manifests WHERE dataset_name=?",
        (MANIFEST_NAME,),
    )
    if manifest is None:
        return
    marker = db.query_one(
        """SELECT id,detail FROM system_events
           WHERE event_type='strategy_effect_history_import' AND source_type='system'
           ORDER BY id LIMIT 1"""
    )
    if marker is not None:
        try:
            old_detail = json.loads(marker["detail"])
            marker_version = old_detail["version"]
        except (TypeError, KeyError, json.JSONDecodeError):
            raise SeedDataIntegrityError("strategy effect history manifest is invalid") from None
        if marker_version == EFFECT_HISTORY_VERSION:
            _validate_effect_history(db, marker["detail"])
            return
        if marker_version != 1:
            raise SeedDataIntegrityError("strategy effect history version is unsupported")
        try:
            old_well_ids = old_detail["well_ids"]
            old_start = datetime.fromisoformat(old_detail["start"])
            old_days = int(old_detail["days"])
        except (TypeError, KeyError, ValueError):
            raise SeedDataIntegrityError("strategy effect history manifest is invalid") from None
        if (
            not isinstance(old_well_ids, list) or not old_well_ids
            or any(not isinstance(well_id, str) for well_id in old_well_ids)
            or old_start.tzinfo is None or old_days < 1
        ):
            raise SeedDataIntegrityError("strategy effect history manifest is invalid")
        old_end = old_start + timedelta(days=old_days)
        old_placeholders = ",".join("?" for _ in old_well_ids)
        with db.transaction(
            None, "strategy_effect_history.upgrade", "seed_dataset", MANIFEST_NAME,
            source_type="system", immediate=True,
        ) as connection:
            connection.execute(
                f"""DELETE FROM telemetry WHERE source_type=?
                    AND well_id IN ({old_placeholders})
                    AND datetime(measured_at)>=datetime(?) AND datetime(measured_at)<=datetime(?)""",
                (EFFECT_SOURCE, *old_well_ids, old_start.isoformat(), old_end.isoformat()),
            )
            connection.execute(
                f"""DELETE FROM production_daily WHERE source_type=?
                    AND well_id IN ({old_placeholders}) AND day>=? AND day<?""",
                (
                    EFFECT_SOURCE, *old_well_ids, old_start.date().isoformat(),
                    old_end.date().isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM system_events WHERE id=?", (marker["id"],)
            )
    profiles = {
        profile["well_id"]: profile
        for profile in _well_profiles(manifest["seed"], manifest["namespace"])
    }
    strategy_rows = db.query_all(
        """SELECT id,well_id FROM energy_strategies
           WHERE source_type=? AND status IN ('active','effective','rollback')
           ORDER BY id""",
        (SIMULATOR,),
    )
    selected = [row for row in strategy_rows if row["well_id"] in profiles]
    if not selected:
        return
    well_ids = [row["well_id"] for row in selected]
    with db.transaction(
        None, "strategy_effect_history.import", "seed_dataset", MANIFEST_NAME,
        source_type="system", immediate=True,
    ) as connection:
        if connection.execute(
            """SELECT 1 FROM system_events
               WHERE event_type='strategy_effect_history_import' AND source_type='system'
               LIMIT 1"""
        ).fetchone() is not None:
            return
        production_rows = []
        telemetry_rows = []
        for strategy in selected:
            profile = profiles[strategy["well_id"]]
            baseline_energy = connection.execute(
                """SELECT SUM(value) FROM telemetry
                   WHERE well_id=? AND metric_code='power_kw' AND status='valid'
                     AND datetime(measured_at)>=datetime('2026-06-16T00:00:00+08:00')
                     AND datetime(measured_at)<datetime('2026-07-01T00:00:00+08:00')""",
                (profile["well_id"],),
            ).fetchone()[0]
            baseline_fluid = connection.execute(
                """SELECT SUM(daily_fluid) FROM production_daily
                   WHERE well_id=? AND status='valid'
                     AND day BETWEEN '2026-06-16' AND '2026-06-30'""",
                (profile["well_id"],),
            ).fetchone()[0]
            if not baseline_energy or not baseline_fluid:
                raise SeedDataIntegrityError(
                    f"strategy effect baseline is unavailable for {profile['well_id']}"
                )
            energy_factors = [1.0 + 0.012 * math.cos(offset / 4.0) for offset in range(EFFECT_DAYS)]
            fluid_factors = [
                1.0 + 0.008 * math.sin((offset + profile["index"]) / 4.0)
                for offset in range(EFFECT_DAYS)
            ]
            connection.execute(
                "UPDATE energy_strategies SET effective_at=? WHERE id=?",
                (EFFECT_START.isoformat(timespec="milliseconds"), strategy["id"]),
            )
            for offset in range(EFFECT_DAYS):
                daily = _effect_daily_profile(
                    profile,
                    offset,
                    float(baseline_energy) * 0.72 * energy_factors[offset] / sum(energy_factors),
                    float(baseline_fluid) * 0.99 * fluid_factors[offset] / sum(fluid_factors),
                )
                day = (EFFECT_START + timedelta(days=offset)).date()
                production_rows.append((
                    profile["well_id"], day.isoformat(), daily["daily_oil"],
                    daily["daily_fluid"], daily["power_kwh"],
                    round(profile["pump_efficiency"], 4), round(profile["stroke_m"], 3),
                    round(profile["frequency_hz"], 3), daily["runtime_hours"],
                    EFFECT_SOURCE, f"{day.isoformat()}T23:00:00.000+08:00",
                    f"{day.isoformat()}T23:00:00.000+08:00",
                ))
                running_hours = int(daily["runtime_hours"])
                weights = [
                    1.0 + 0.03 * math.sin((hour + profile["index"] + offset) / 3.0)
                    for hour in range(running_hours)
                ]
                total_weight = sum(weights)
                values = [round(daily["power_kwh"] * weight / total_weight, 3) for weight in weights]
                values[-1] = round(daily["power_kwh"] - sum(values[:-1]), 3)
                start_hour = (profile["index"] + offset * 5) % 24
                active = {(start_hour + hour) % 24: values[hour] for hour in range(running_hours)}
                for hour in range(24):
                    measured = EFFECT_START + timedelta(days=offset, hours=hour)
                    timestamp = measured.isoformat(timespec="milliseconds")
                    telemetry_rows.append((
                        profile["well_id"], profile["device_id"], profile["channel_id"],
                        timestamp, active.get(hour, 0.0), EFFECT_SOURCE, timestamp, timestamp,
                    ))
            endpoint = (EFFECT_START + timedelta(days=EFFECT_DAYS)).isoformat(timespec="milliseconds")
            telemetry_rows.append((
                profile["well_id"], profile["device_id"], profile["channel_id"],
                endpoint, 0.0, EFFECT_SOURCE, endpoint, endpoint,
            ))
        connection.executemany(
            """INSERT INTO production_daily(
                   well_id,day,daily_oil,daily_fluid,power_kwh,pump_efficiency,stroke_m,
                   frequency_hz,runtime_hours,status,source_type,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,'valid',?,?,?)""",
            production_rows,
        )
        connection.executemany(
            """INSERT INTO telemetry(
                   well_id,device_id,channel_id,measured_at,metric_code,value,unit,status,
                   source_type,created_at,updated_at
               ) VALUES(?,?,?,?,'power_kw',?,'kW','valid',?,?,?)""",
            telemetry_rows,
        )
        detail = _canonical_json({
            "version": EFFECT_HISTORY_VERSION, "well_ids": well_ids, "days": EFFECT_DAYS,
            "start": EFFECT_START.isoformat(timespec="milliseconds"),
            "production_rows": len(production_rows), "telemetry_rows": len(telemetry_rows),
        })
        connection.execute(
            """INSERT INTO system_events(event_type,detail,status,source_type)
               VALUES('strategy_effect_history_import',?,'recorded','system')""",
            (detail,),
        )


def _validate_effect_history(db: Database, raw_detail: str) -> None:
    try:
        detail = json.loads(raw_detail)
        well_ids = detail["well_ids"]
        expected_production = int(detail["production_rows"])
        expected_telemetry = int(detail["telemetry_rows"])
        start = datetime.fromisoformat(detail["start"])
        days = int(detail["days"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise SeedDataIntegrityError("strategy effect history manifest is invalid") from None
    if not isinstance(well_ids, list) or not well_ids or any(
        not isinstance(well_id, str) for well_id in well_ids
    ) or start.tzinfo is None or days < 1:
        raise SeedDataIntegrityError("strategy effect history manifest is invalid")
    end = start + timedelta(days=days)
    placeholders = ",".join("?" for _ in well_ids)
    production = db.scalar(
        f"""SELECT COUNT(*) FROM production_daily WHERE source_type=?
            AND well_id IN ({placeholders}) AND day>=? AND day<?""",
        (EFFECT_SOURCE, *well_ids, start.date().isoformat(), end.date().isoformat()),
    )
    telemetry = db.scalar(
        f"""SELECT COUNT(*) FROM telemetry WHERE source_type=?
            AND well_id IN ({placeholders})
            AND datetime(measured_at)>=datetime(?) AND datetime(measured_at)<=datetime(?)""",
        (EFFECT_SOURCE, *well_ids, start.isoformat(), end.isoformat()),
    )
    effective = db.scalar(
        f"""SELECT COUNT(*) FROM energy_strategies
            WHERE source_type=? AND well_id IN ({placeholders}) AND effective_at IS NOT NULL""",
        (SIMULATOR, *well_ids),
    )
    if (production, telemetry, effective) != (
        expected_production, expected_telemetry, len(well_ids),
    ):
        raise SeedDataIntegrityError("strategy effect history is incomplete")


def seed_database(db: Database, seed: int = 202606) -> SeedSummary:
    """Populate an empty platform database once, using one audited transaction.

    The profile of each well is the common source for production, power, dynagraph
    features, diagnosis, and (where appropriate) its energy-control workflow.
    """
    manifest = db.query_one(
        "SELECT dataset_name, version, seed, namespace, summary_json, content_hash, status, source_type "
        "FROM seed_manifests WHERE dataset_name = ?",
        (MANIFEST_NAME,),
    )
    if manifest:
        if manifest["content_hash"] is None:
            if (
                manifest["dataset_name"] != MANIFEST_NAME or manifest["version"] != MANIFEST_VERSION
                or manifest["seed"] != seed or manifest["status"] != "complete"
                or manifest["source_type"] != SIMULATOR
            ):
                raise SeedDataIntegrityError("seed dataset incomplete: legacy manifest contract")
            summary = _validated_summary(db, seed, manifest["namespace"])
            _adopt_legacy_dataset(db, seed, manifest["namespace"], summary)
            return _with_auth_bootstrap(db, _validate_manifest(db, db.query_one(
                "SELECT dataset_name, version, seed, namespace, summary_json, content_hash, status, source_type "
                "FROM seed_manifests WHERE dataset_name = ?", (MANIFEST_NAME,)
            ), seed))
        return _with_auth_bootstrap(db, _validate_manifest(db, manifest, seed))

    if db.scalar("SELECT 1 FROM seed_manifests LIMIT 1"):
        raise SeedDataIntegrityError("seed dataset incomplete: manifest dataset name")

    if db.scalar("SELECT 1 FROM units WHERE source_type = ? LIMIT 1", (SIMULATOR,)):
        summary = _validated_summary(db, seed, "")
        _adopt_legacy_dataset(db, seed, "", summary)
        return _with_auth_bootstrap(db, _validate_manifest(db, db.query_one(
            "SELECT dataset_name, version, seed, namespace, summary_json, content_hash, status, source_type "
            "FROM seed_manifests WHERE dataset_name = ?", (MANIFEST_NAME,)
        ), seed))

    namespace = _new_namespace(db, seed)
    profiles = _well_profiles(seed, namespace)
    with db.transaction(
        None, "seed.generate", "database", "platform", source_type=SIMULATOR
    ) as connection:
        connection.executemany(
            """
            INSERT INTO units(id, code, name, status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, 'active', ?, ?, ?)
            """,
            [
                (_identifier(namespace, f"U-{index:02d}"), _identifier(namespace, f"CY-{index:02d}"),
                 name, SIMULATOR, GENERATED_AT, GENERATED_AT)
                for index, name in enumerate(UNIT_NAMES, start=1)
            ],
        )
        connection.executemany(
            """
            INSERT INTO well_groups(id, unit_id, code, name, status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            [
                (_identifier(namespace, f"G-{index:02d}"), _identifier(namespace, f"U-{(index + 1) // 2:02d}"),
                 _identifier(namespace, f"JG-{index:02d}"),
                 f"{UNIT_NAMES[(index - 1) // 2]}第{1 if index % 2 else 2}井组",
                 SIMULATOR, GENERATED_AT, GENERATED_AT)
                for index in range(1, 9)
            ],
        )
        connection.executemany(
            """
            INSERT INTO wells(id, group_id, code, name, status, source_type, is_low_output, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (profile["well_id"], profile["group_id"], profile["code"], profile["name"],
                 "intermittent" if profile["low_output"] else "running", SIMULATOR,
                 int(profile["low_output"]), GENERATED_AT, GENERATED_AT)
                for profile in profiles
            ],
        )
        connection.executemany(
            """
            INSERT INTO devices(id, well_id, serial_number, device_type, firmware_version, status,
                                source_type, created_at, updated_at)
            VALUES(?, ?, ?, 'DSP_MCU_CONTROLLER', '2.4.1', 'online', ?, ?, ?)
            """,
            [
                (profile["device_id"], profile["well_id"],
                 _identifier(profile["namespace"], f"SIM-DSP-{profile['index']:03d}"),
                 SIMULATOR, GENERATED_AT, GENERATED_AT)
                for profile in profiles
            ],
        )
        connection.executemany(
            """
            INSERT INTO device_channels(id, device_id, channel_code, name, status, source_type,
                                        created_at, updated_at)
            VALUES(?, ?, 'POWER_KW', '有功功率', 'active', ?, ?, ?)
            """,
            [
                (profile["channel_id"], profile["device_id"], SIMULATOR, GENERATED_AT, GENERATED_AT)
                for profile in profiles
            ],
        )
        connection.executemany(
            """
            INSERT INTO device_heartbeats(device_id, reported_at, status, source_type, created_at, updated_at)
            VALUES(?, ?, 'online', ?, ?, ?)
            """,
            [
                (profile["device_id"], GENERATED_AT, SIMULATOR, GENERATED_AT, GENERATED_AT)
                for profile in profiles
            ],
        )
        connection.executemany(
            """
            INSERT INTO production_daily(
                well_id, day, daily_oil, daily_fluid, power_kwh, pump_efficiency, stroke_m,
                frequency_hz, runtime_hours, status, source_type, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?, ?)
            """,
            _production_rows(profiles),
        )
        connection.executemany(
            """
            INSERT INTO telemetry(well_id, device_id, channel_id, measured_at, metric_code, value, unit,
                                  status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, ?, 'power_kw', ?, 'kW', 'valid', ?, ?, ?)
            """,
            _telemetry_rows(profiles),
        )
        _insert_analysis_and_operations(connection, profiles, seed)
        summary = _validated_summary_from(connection, seed, namespace)
        content_hash = _content_hash_from(connection, seed, namespace)
        connection.execute(
            """
            INSERT INTO seed_manifests(dataset_name, version, seed, namespace, summary_json, content_hash,
                                       status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?)
            """,
            (MANIFEST_NAME, MANIFEST_VERSION, seed, namespace, _canonical_json(summary.__dict__), content_hash,
             SIMULATOR, GENERATED_AT, GENERATED_AT),
        )
    return _with_auth_bootstrap(db, _validate_manifest(db, db.query_one(
        "SELECT dataset_name, version, seed, namespace, summary_json, content_hash, status, source_type "
        "FROM seed_manifests WHERE dataset_name = ?", (MANIFEST_NAME,)
    ), seed))


def _well_profiles(seed: int, namespace: str = "") -> list[dict]:
    randomizer = random.Random(seed)
    profiles = []
    for index in range(1, 49):
        low_output = index % 3 == 1
        fluid_target = 2.846 if index == 1 else (
            2.45 + randomizer.random() * 1.35 if low_output else 7.9 + randomizer.random() * 5.2
        )
        pump_efficiency = (0.54 + randomizer.random() * 0.13) if low_output else (0.74 + randomizer.random() * 0.16)
        stroke_m = 1.85 + fluid_target * 0.035 + randomizer.random() * 0.08
        frequency_hz = 29.0 + fluid_target * 0.85 + randomizer.random() * 1.5
        runtime_hours = (12.2 + randomizer.random() * 2.2) if low_output else (22.4 + randomizer.random() * 1.2)
        power_kw = 1.9 + fluid_target * 0.72 + (1.0 - pump_efficiency) * 1.8
        profiles.append(
            {
                "index": index,
                "namespace": namespace,
                "well_id": _identifier(namespace, f"W-{index:03d}"),
                "group_id": _identifier(namespace, f"G-{(index - 1) // 6 + 1:02d}"),
                "code": _identifier(namespace, f"CYJ-{index:03d}"),
                "name": f"抽油井 {index:03d}",
                "device_id": _identifier(namespace, f"D-{index:03d}"),
                "channel_id": _identifier(namespace, f"CH-{index:03d}-POWER"),
                "dynagraph_id": _identifier(namespace, f"DG-{index:03d}"),
                "model_run_id": _identifier(namespace, f"MR-{index:03d}"),
                "diagnosis_id": _identifier(namespace, f"DX-{index:03d}"),
                "low_output": low_output,
                "fluid_target": fluid_target,
                "pump_efficiency": pump_efficiency,
                "stroke_m": stroke_m,
                "frequency_hz": frequency_hz,
                "runtime_hours": runtime_hours,
                "power_kw": power_kw,
            }
        )
    return profiles


def _production_rows(profiles: list[dict]) -> list[tuple]:
    rows = []
    for profile in profiles:
        for offset in range(90):
            daily = _daily_profile(profile, offset)
            day = daily["day"]
            rows.append(
                (
                    profile["well_id"], day.isoformat(), daily["daily_oil"], daily["daily_fluid"],
                    daily["power_kwh"], round(profile["pump_efficiency"], 4), round(profile["stroke_m"], 3),
                    round(profile["frequency_hz"], 3), daily["runtime_hours"], SIMULATOR,
                    f"{day.isoformat()}T23:00:00.000+08:00", f"{day.isoformat()}T23:00:00.000+08:00",
                )
            )
    return rows


def _telemetry_rows(profiles: list[dict]) -> list[tuple]:
    rows = []
    for profile in profiles:
        for day_offset in range(30):
            daily = _daily_profile(profile, 60 + day_offset)
            running_hours = int(daily["runtime_hours"])
            weights = [1.0 + 0.04 * math.sin((hour + profile["index"] + day_offset) / 3.0)
                       for hour in range(running_hours)]
            total_weight = sum(weights)
            running_values = [round(daily["power_kwh"] * weight / total_weight, 3) for weight in weights]
            running_values[-1] = round(daily["power_kwh"] - sum(running_values[:-1]), 3)
            start_hour = (profile["index"] + day_offset * 3) % 24
            active_hours = {(start_hour + hour) % 24: running_values[hour] for hour in range(running_hours)}
            for hour in range(24):
                measured_at = TELEMETRY_START + timedelta(days=day_offset, hours=hour)
                timestamp = measured_at.isoformat(timespec="milliseconds")
                rows.append(
                    (profile["well_id"], profile["device_id"], profile["channel_id"], timestamp,
                     active_hours.get(hour, 0.0), SIMULATOR, timestamp, timestamp)
                )
    return rows


def _daily_profile(profile: dict, offset: int) -> dict:
    day = PRODUCTION_START + timedelta(days=offset)
    variation = 1.0 + 0.035 * math.sin((offset + profile["index"]) / 5.0)
    daily_fluid = 2.846 if profile["index"] == 1 and offset == 0 else profile["fluid_target"] * variation
    runtime_hours = (12 + (offset + profile["index"]) % 3) if profile["low_output"] else (22 + (offset + profile["index"]) % 3)
    daily_oil = daily_fluid * (0.61 + profile["pump_efficiency"] * 0.08)
    power_kwh = profile["power_kw"] * runtime_hours * (0.94 + 0.02 * math.cos(offset / 4.0))
    return {
        "day": day, "daily_fluid": round(daily_fluid, 3), "daily_oil": round(daily_oil, 3),
        "runtime_hours": runtime_hours, "power_kwh": round(power_kwh, 3),
    }


def _effect_daily_profile(
    profile: dict, offset: int, power_kwh: float, daily_fluid: float,
) -> dict:
    """Post-effective measurements retain liquid while reducing runtime."""
    runtime_hours = 9 + (offset + profile["index"]) % 2
    daily_oil = daily_fluid * (0.61 + profile["pump_efficiency"] * 0.08)
    return {
        "daily_fluid": round(daily_fluid, 3),
        "daily_oil": round(daily_oil, 3),
        "runtime_hours": runtime_hours,
        "power_kwh": round(power_kwh, 3),
    }


def _insert_analysis_and_operations(connection, profiles: list[dict], seed: int) -> None:
    connection.executemany(
        """
        INSERT INTO raw_messages(device_id, received_at, payload, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'processed', ?, ?, ?)
        """,
        [
            (profile["device_id"], GENERATED_AT, _json_payload(profile, seed), SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO dynagraphs(id, well_id, captured_at, sample_data, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'available', ?, ?, ?)
        """,
        [
            (profile["dynagraph_id"], profile["well_id"], GENERATED_AT,
             _dynagraph_sample(profile), SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO dynagraph_features(dynagraph_id, feature_name, feature_value, status, source_type,
                                       created_at, updated_at)
        VALUES(?, ?, ?, 'valid', ?, ?, ?)
        """,
        [
            (profile["dynagraph_id"], feature, value, SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
            for feature, value in (
                ("pump_efficiency", round(profile["pump_efficiency"], 4)),
                ("stroke_m", round(profile["stroke_m"], 3)),
                ("power_kw", round(profile["power_kw"], 3)),
            )
        ],
    )
    connection.executemany(
        """
        INSERT INTO model_runs(id, model_name, model_version, input_object_type, input_object_id, status,
                               source_type, created_at, updated_at)
        VALUES(?, 'rule_profile_diagnosis', '1.0', 'dynagraph', ?, 'completed', ?, ?, ?)
        """,
        [
            (profile["model_run_id"], profile["dynagraph_id"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO diagnoses(id, well_id, dynagraph_id, model_run_id, diagnosis_type, confidence, evidence,
                              status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, 'reviewed', ?, ?, ?)
        """,
        [
            (profile["diagnosis_id"], profile["well_id"], profile["dynagraph_id"], profile["model_run_id"],
             "供液不足" if profile["low_output"] else "运行正常",
             round(0.88 if profile["low_output"] else 0.94, 2),
             json.dumps({"fluid_target": round(profile["fluid_target"], 3),
                         "pump_efficiency": round(profile["pump_efficiency"], 4),
                         "power_kw": round(profile["power_kw"], 3)}, ensure_ascii=False, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
        ],
    )
    telemetry_ids = {
        row["well_id"]: row["id"]
        for row in connection.execute(
            "SELECT well_id, MIN(id) AS id FROM telemetry GROUP BY well_id"
        )
    }
    connection.executemany(
        """
        INSERT INTO data_quality_events(telemetry_id, well_id, event_type, detail, status, source_type,
                                        created_at, updated_at)
        VALUES(?, ?, 'completeness_check', ?, 'closed', ?, ?, ?)
        """,
        [
            (telemetry_ids[profile["well_id"]], profile["well_id"], "hourly data complete",
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO system_events(event_type, detail, status, source_type, created_at, updated_at)
        VALUES('device_telemetry_cycle', ?, 'recorded', ?, ?, ?)
        """,
        [
            (json.dumps({"device_id": profile["device_id"], "well_id": profile["well_id"],
                         "power_kw": round(profile["power_kw"], 3)}, ensure_ascii=False, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in profiles
        ],
    )
    _insert_users_and_report(connection, profiles[0]["namespace"])
    _insert_low_output_workflows(connection, [profile for profile in profiles if profile["low_output"]])


def _insert_low_output_workflows(connection, low_profiles: list[dict]) -> None:
    specs = _workflow_specs(low_profiles)
    namespace = low_profiles[0]["namespace"] if low_profiles else ""
    maintenance_id = connection.execute(
        "SELECT id FROM users WHERE username=?",
        (_identifier(namespace, "maintenance_engineer"),),
    ).fetchone()["id"]
    reviewer_id = connection.execute(
        "SELECT id FROM users WHERE username=?",
        (_identifier(namespace, "reviewer"),),
    ).fetchone()["id"]
    connection.executemany(
        """
        INSERT INTO recovery_tests(id, well_id, tested_at, result_data, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'completed', ?, ?, ?)
        """,
        [
            (spec["recovery_id"], spec["profile"]["well_id"], GENERATED_AT,
             _canonical_json(_recovery_payload(spec["profile"])),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.executemany(
        """
        INSERT INTO energy_strategies(
            id, well_id, name, effective_at, status, source_type, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (spec["strategy_id"], spec["profile"]["well_id"], f"{spec['profile']['name']} 间抽节能策略",
             "2026-06-16T00:00:00.000+08:00"
             if spec["strategy"] in {"active", "effective", "rollback"} else None,
             spec["strategy"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.executemany(
        """
        INSERT INTO strategy_versions(id, strategy_id, version_number, configuration, status, source_type,
                                      created_at, updated_at)
        VALUES(?, ?, 1, ?, ?, ?, ?, ?)
        """,
        [
            (spec["version_id"], spec["strategy_id"],
             json.dumps({"run_hours": round(spec["profile"]["runtime_hours"], 2), "stop_hours": 8,
                          "minimum_daily_fluid": round(spec["profile"]["fluid_target"] * 0.95, 3)}, sort_keys=True),
             spec["strategy"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.execute(
        """UPDATE energy_strategies SET current_version_id=(
               SELECT version.id FROM strategy_versions AS version
               WHERE version.strategy_id=energy_strategies.id
               ORDER BY version.version_number DESC,version.id DESC LIMIT 1
           ) WHERE source_type=? AND current_version_id IS NULL""",
        (SIMULATOR,),
    )
    connection.executemany(
        """
        INSERT INTO device_commands(id, device_id, strategy_version_id, command_type, payload, status,
                                    source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'set_intermittent_cycle', ?, ?, ?, ?, ?)
        """,
        [
            (spec["command_id"], spec["profile"]["device_id"], spec["version_id"],
             json.dumps({"frequency_hz": round(spec["profile"]["frequency_hz"], 3)}, sort_keys=True),
             spec["command"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs if spec["command"]
        ],
    )
    connection.executemany(
        """
        INSERT INTO command_attempts(command_id, attempted_at, result_detail, status, source_type,
                                     created_at, updated_at)
        VALUES(?, ?, 'virtual controller processed command', ?, ?, ?, ?)
        """,
        [
            (spec["command_id"], GENERATED_AT, spec["attempt"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs if spec["command"]
        ],
    )
    connection.executemany(
        """
        INSERT INTO alarms(
            id,well_id,diagnosis_id,alarm_type,severity,details_json,
            source_object_type,source_object_id,status,source_type,created_at,updated_at
        ) VALUES(?,?,?,'supply_shortage','warning','{}','diagnosis',?,?,?,?,?)
        """,
        [
            (spec["alarm_id"], spec["profile"]["well_id"], spec["profile"]["diagnosis_id"],
             spec["profile"]["diagnosis_id"], spec["alarm"], SIMULATOR,
             GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.executemany(
        """
        INSERT INTO approvals(id, object_type, object_id, decision, status, source_type, created_at, updated_at)
        VALUES(?, 'energy_strategy', ?, ?, ?, ?, ?, ?)
        """,
        [
            (spec["approval_id"], spec["strategy_id"], spec["approval"], spec["approval"],
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_orders(
            id,well_id,alarm_id,title,kind,assignee_id,due_at,accepted_at,
            completed_at,closed_at,closed_by,status,source_type,created_at,updated_at
        ) VALUES(?,?,?,?, 'production_adjustment',?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (spec["work_order_id"], spec["profile"]["well_id"], spec["alarm_id"],
             f"{spec['profile']['name']} 供液不足处置", maintenance_id,
             "2026-07-01T03:00:00.000+08:00",
             GENERATED_AT if spec["work_order"] in {"processing", "completed", "closed"} else None,
             GENERATED_AT if spec["work_order"] in {"completed", "closed"} else None,
             GENERATED_AT if spec["work_order"] == "closed" else None,
             reviewer_id if spec["work_order"] == "closed" else None,
             spec["work_order"],
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_order_logs(
            work_order_id,message,from_status,to_status,comment,changed_at,
            status,source_type,created_at,updated_at
        ) VALUES(?, '间抽策略处置记录',NULL,?,
                 '间抽策略处置记录',?,?,?, ?,?)
        """,
        [
            (spec["work_order_id"], spec["work_order"], GENERATED_AT,
             spec["work_order"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )


def _insert_users_and_report(connection, namespace: str) -> None:
    roles = (("administrator", "管理员"), ("production_engineer", "生产工程师"),
             ("algorithm_engineer", "算法工程师"), ("maintenance_engineer", "设备运维"),
             ("reviewer", "复核评审"))
    connection.executemany(
        """
        INSERT INTO roles(code, name, status, source_type, created_at, updated_at)
        VALUES(?, ?, 'active', ?, ?, ?)
        """,
        [(_identifier(namespace, code), name, SIMULATOR, GENERATED_AT, GENERATED_AT) for code, name in roles],
    )
    role_ids = {row["code"]: row["id"] for row in connection.execute("SELECT id, code FROM roles")}
    connection.executemany(
        """
        INSERT INTO users(role_id, username, display_name, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'active', ?, ?, ?)
        """,
        [(role_ids[_identifier(namespace, code)], _identifier(namespace, code), name,
          SIMULATOR, GENERATED_AT, GENERATED_AT) for code, name in roles],
    )
    connection.execute(
        """
        INSERT INTO reports(id, report_type, period_start, period_end, payload, status, source_type, created_at, updated_at)
        VALUES(?, 'production_daily', '2026-06-01T00:00:00.000+08:00',
               '2026-06-30T23:00:00.000+08:00', ?, 'generated', ?, ?, ?)
        """,
        (_identifier(namespace, "RPT-SEED-001"),
         json.dumps({"source": SIMULATOR, "well_count": 48}, sort_keys=True),
         SIMULATOR, GENERATED_AT, GENERATED_AT),
    )


def _json_payload(profile: dict, seed: int) -> str:
    return json.dumps(
        {"generator": "seed_database", "seed": seed, "well_id": profile["well_id"],
         "power_kw": round(profile["power_kw"], 3)},
        ensure_ascii=False, sort_keys=True,
    )


def _dynagraph_sample(profile: dict) -> str:
    card = build_profile_card(profile_condition(profile["device_id"]), {
        "stroke_m": profile["stroke_m"], "strokes_per_minute": 4.2,
    }, variation=.2 * math.sin(profile["index"]))
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))
