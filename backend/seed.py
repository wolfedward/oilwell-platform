"""Deterministic, internally traceable history for the offline acceptance platform."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import random

from backend.database import Database


SIMULATOR = "simulator"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
GENERATED_AT = "2026-06-30T23:00:00.000+08:00"
PRODUCTION_START = date(2026, 4, 2)
TELEMETRY_START = datetime(2026, 6, 1, tzinfo=SHANGHAI_TIMEZONE)
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
    "administrator": ("users.read", "users.manage", "roles.read", "roles.manage", "reports.read", "audit.read", "strategy.approve", "wells.read", "dashboard.read", "telemetry.read", "wells.update", "devices.read", "devices.control", "devices.configure", "acquisition.read", "data_quality.read", "data_quality.repair"),
    "production_engineer": ("wells.read", "dashboard.read", "telemetry.read", "wells.update", "alarms.ack", "strategy.create", "strategy.submit", "reports.read", "devices.read", "acquisition.read", "data_quality.read"),
    "algorithm_engineer": ("wells.read", "dashboard.read", "telemetry.read", "diagnosis.run", "reports.read", "devices.read", "acquisition.read", "data_quality.read"),
    "maintenance_engineer": ("wells.read", "dashboard.read", "telemetry.read", "wells.update", "devices.read", "devices.control", "devices.configure", "maintenance.manage", "acquisition.read", "data_quality.read", "data_quality.repair"),
    "reviewer": ("wells.read", "dashboard.read", "telemetry.read", "diagnosis.review", "reports.read", "audit.read", "devices.read", "acquisition.read", "data_quality.read"),
}
AUTH_BOOTSTRAP_VERSION = 4


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


def _content_hash_from(connection, seed: int, namespace: str, *, projection: str = "strategy-recovery-v5") -> str:
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
    lifecycle_projection = projection in {"well-lifecycle-v3", "strategy-recovery-v5"} or (projection.startswith("device-config-v") and projection != "device-config-v4-legacy")
    well_fields = "'id',id,'group',group_id,'code',code,'name',name,'low',is_low_output,'source',source_type" if lifecycle_projection else "'id',id,'group',group_id,'code',code,'name',name,'status',status,'low',is_low_output,'source',source_type"
    rows_for_ids("wells", "wells", "id", well_ids, well_fields)
    config_projection = projection.startswith("device-config-v") or projection == "strategy-recovery-v5"
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
    for label, table, identifier, key, fields in (
        ("recovery", "recovery_tests", "id", "recovery_id", "'id',id,'well',well_id,'at',tested_at,'result',result_data,'status',status,'source',source_type"),
        ("strategies", "energy_strategies", "id", "strategy_id", "'id',id,'well',well_id,'name',name,'status',status,'source',source_type"),
        ("versions", "strategy_versions", "id", "version_id", "'id',id,'strategy',strategy_id,'version',version_number,'configuration',configuration,'status',status,'source',source_type"),
        ("alarms", "alarms", "id", "alarm_id", "'id',id,'well',well_id,'diagnosis',diagnosis_id,'type',alarm_type,'severity',severity,'status',status,'source',source_type"),
        ("approvals", "approvals", "id", "approval_id", "'id',id,'object_type',object_type,'object',object_id,'decision',decision,'status',status,'source',source_type"),
        ("orders", "work_orders", "id", "work_order_id", "'id',id,'well',well_id,'alarm',alarm_id,'title',title,'status',status,'source',source_type"),
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
    legacy_hash = _legacy_content_hash(db, requested_seed, manifest["namespace"])
    if manifest["content_hash"] not in {
        task1_previous_hash, task1_current_v4_hash, strategy_previous_v4_hash,
        previous_hash, legacy_hash,
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
                (MANIFEST_NAME, '{"after":{"projection":"strategy-recovery-v5"},"before":{"projection":"verified-legacy"}}', SIMULATOR),
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
             '{"after":{"projection":"strategy-recovery-v5"},"before":{"projection":"device-config-v4"}}',
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
    if marker is not None and marker["bootstrap_version"] >= AUTH_BOOTSTRAP_VERSION and not (set(permission_codes) - current_permissions):
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
            connection.execute("UPDATE auth_bootstrap_state SET bootstrap_version=? WHERE id=1", (AUTH_BOOTSTRAP_VERSION,))


def _with_auth_bootstrap(db: Database, summary: SeedSummary) -> SeedSummary:
    bootstrap_local_auth(db)
    return summary


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
    _insert_low_output_workflows(connection, [profile for profile in profiles if profile["low_output"]])
    _insert_users_and_report(connection, profiles[0]["namespace"])


def _insert_low_output_workflows(connection, low_profiles: list[dict]) -> None:
    specs = _workflow_specs(low_profiles)
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
        INSERT INTO energy_strategies(id, well_id, name, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (spec["strategy_id"], spec["profile"]["well_id"], f"{spec['profile']['name']} 间抽节能策略",
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
        INSERT INTO alarms(id, well_id, diagnosis_id, alarm_type, severity, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'supply_shortage', 'warning', ?, ?, ?, ?)
        """,
        [
            (spec["alarm_id"], spec["profile"]["well_id"], spec["profile"]["diagnosis_id"],
             spec["alarm"], SIMULATOR, GENERATED_AT, GENERATED_AT)
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
        INSERT INTO work_orders(id, well_id, alarm_id, title, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (spec["work_order_id"], spec["profile"]["well_id"], spec["alarm_id"],
             f"{spec['profile']['name']} 供液不足处置", spec["work_order"],
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for spec in specs
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_order_logs(work_order_id, message, status, source_type, created_at, updated_at)
        VALUES(?, '间抽策略处置记录', ?, ?, ?, ?)
        """,
        [
            (spec["work_order_id"], spec["work_order"], SIMULATOR, GENERATED_AT, GENERATED_AT)
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
    samples = [
        {"position_m": round(profile["stroke_m"] * phase / 7, 3),
         "load_kn": round((22 + profile["power_kw"] * 3) * (0.82 + 0.18 * math.sin(phase)), 3)}
        for phase in range(8)
    ]
    return json.dumps(samples, ensure_ascii=False, separators=(",", ":"))
