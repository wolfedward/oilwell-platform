"""Deterministic, internally traceable history for the offline acceptance platform."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
            f"WHERE well_id IN ({placeholder}) GROUP BY well_id", well_ids
        ).fetchall()
        if len(rows) != 48 or any(row["records"] != expected_rows for row in rows):
            raise SeedDataIntegrityError(f"seed dataset incomplete: {table_name} distribution")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SeedDataIntegrityError("seed dataset incomplete: foreign key violation")
    return SeedSummary(counts["units"], counts["groups"], counts["wells"], counts["devices"], counts["telemetry"])


def _count_seed_ids(connection, table_name: str, column_name: str, values: list[str]) -> int:
    placeholders = ", ".join("?" for _ in values)
    return connection.execute(
        f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} IN ({placeholders}) AND source_type = ?",
        [*values, SIMULATOR],
    ).fetchone()[0]


def _adopt_legacy_dataset(db: Database, seed: int, summary: SeedSummary) -> None:
    with db.transaction(
        None, "seed.adopt_manifest", "database", "platform", source_type=SIMULATOR
    ) as connection:
        connection.execute(
            """
            INSERT INTO seed_manifests(dataset_name, version, seed, namespace, summary_json, status,
                                       source_type, created_at, updated_at)
            VALUES(?, ?, ?, '', ?, 'complete', ?, ?, ?)
            """,
            (MANIFEST_NAME, MANIFEST_VERSION, seed, json.dumps(summary.__dict__, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT),
        )


def seed_database(db: Database, seed: int = 202606) -> SeedSummary:
    """Populate an empty platform database once, using one audited transaction.

    The profile of each well is the common source for production, power, dynagraph
    features, diagnosis, and (where appropriate) its energy-control workflow.
    """
    manifest = db.query_one(
        "SELECT version, seed, namespace FROM seed_manifests WHERE dataset_name = ?",
        (MANIFEST_NAME,),
    )
    if manifest:
        if manifest["version"] != MANIFEST_VERSION:
            raise SeedDataIntegrityError("seed dataset incomplete: unsupported manifest version")
        return _validated_summary(db, int(manifest["seed"]), manifest["namespace"])

    if db.scalar("SELECT 1 FROM units WHERE source_type = ? LIMIT 1", (SIMULATOR,)):
        summary = _validated_summary(db, seed, "")
        _adopt_legacy_dataset(db, seed, summary)
        return summary

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
        connection.execute(
            """
            INSERT INTO seed_manifests(dataset_name, version, seed, namespace, summary_json, status,
                                       source_type, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, 'complete', ?, ?, ?)
            """,
            (MANIFEST_NAME, MANIFEST_VERSION, seed, namespace, json.dumps(summary.__dict__, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT),
        )
    return _validated_summary(db, seed, namespace)


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
    states = (
        ("draft", "pending", "pending", "pending", "new", "pending"),
        ("pending", "rejected", "pending", "pending", "acknowledged", "pending"),
        ("active", "approved", "sent", "sent", "dispatched", "processing"),
        ("effective", "approved", "succeeded", "succeeded", "closed", "completed"),
        ("rollback", "approved", "rolled_back", "rolled_back", "closed", "closed"),
        ("active", "approved", "failed", "failed", "acknowledged", "processing"),
    )
    for position, profile in enumerate(low_profiles):
        strategy_status, approval_status, command_status, attempt_status, alarm_status, work_order_status = states[position % len(states)]
        profile["workflow"] = {
            "strategy": strategy_status, "approval": approval_status, "command": command_status,
            "attempt": attempt_status, "alarm": alarm_status, "work_order": work_order_status,
        }
        namespace = profile["namespace"]
        profile["strategy_id"] = _identifier(namespace, f"ES-{profile['index']:03d}")
        profile["strategy_version_id"] = _identifier(namespace, f"SV-{profile['index']:03d}")
        profile["command_id"] = _identifier(namespace, f"CMD-{profile['index']:03d}")
        profile["alarm_id"] = _identifier(namespace, f"AL-{profile['index']:03d}")
        profile["approval_id"] = _identifier(namespace, f"AP-{profile['index']:03d}")
        profile["work_order_id"] = _identifier(namespace, f"WO-{profile['index']:03d}")
    connection.executemany(
        """
        INSERT INTO recovery_tests(id, well_id, tested_at, result_data, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'completed', ?, ?, ?)
        """,
        [
            (_identifier(profile["namespace"], f"RT-{profile['index']:03d}"), profile["well_id"], GENERATED_AT,
             json.dumps({"recovery_hours": 8, "fluid_target": round(profile["fluid_target"], 3)}, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO energy_strategies(id, well_id, name, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (profile["strategy_id"], profile["well_id"], f"{profile['name']} 间抽节能策略",
             profile["workflow"]["strategy"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO strategy_versions(id, strategy_id, version_number, configuration, status, source_type,
                                      created_at, updated_at)
        VALUES(?, ?, 1, ?, ?, ?, ?, ?)
        """,
        [
            (profile["strategy_version_id"], profile["strategy_id"],
             json.dumps({"run_hours": round(profile["runtime_hours"], 2), "stop_hours": 8,
                          "minimum_daily_fluid": round(profile["fluid_target"] * 0.95, 3)}, sort_keys=True),
             profile["workflow"]["strategy"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO device_commands(id, device_id, strategy_version_id, command_type, payload, status,
                                    source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'set_intermittent_cycle', ?, ?, ?, ?, ?)
        """,
        [
            (profile["command_id"], profile["device_id"], profile["strategy_version_id"],
             json.dumps({"frequency_hz": round(profile["frequency_hz"], 3)}, sort_keys=True),
             profile["workflow"]["command"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO command_attempts(command_id, attempted_at, result_detail, status, source_type,
                                     created_at, updated_at)
        VALUES(?, ?, 'virtual controller processed command', ?, ?, ?, ?)
        """,
        [
            (profile["command_id"], GENERATED_AT, profile["workflow"]["attempt"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO alarms(id, well_id, diagnosis_id, alarm_type, severity, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'supply_shortage', 'warning', ?, ?, ?, ?)
        """,
        [
            (profile["alarm_id"], profile["well_id"], profile["diagnosis_id"],
             profile["workflow"]["alarm"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO approvals(id, object_type, object_id, decision, status, source_type, created_at, updated_at)
        VALUES(?, 'energy_strategy', ?, ?, ?, ?, ?, ?)
        """,
        [
            (profile["approval_id"], profile["strategy_id"], profile["workflow"]["approval"],
             profile["workflow"]["approval"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_orders(id, well_id, alarm_id, title, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (profile["work_order_id"], profile["well_id"], profile["alarm_id"],
             f"{profile['name']} 供液不足处置", profile["workflow"]["work_order"],
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_order_logs(work_order_id, message, status, source_type, created_at, updated_at)
        VALUES(?, '间抽策略处置记录', ?, ?, ?, ?)
        """,
        [
            (profile["work_order_id"], profile["workflow"]["work_order"], SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )


def _insert_users_and_report(connection, namespace: str) -> None:
    roles = (("administrator", "管理员"), ("production_engineer", "生产工程师"),
             ("algorithm_engineer", "算法工程师"), ("maintenance_engineer", "设备运维"),
             ("reviewer", "只读评审"))
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
