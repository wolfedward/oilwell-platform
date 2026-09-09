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


def seed_database(db: Database, seed: int = 202606) -> SeedSummary:
    """Populate an empty platform database once, using one audited transaction.

    The profile of each well is the common source for production, power, dynagraph
    features, diagnosis, and (where appropriate) its energy-control workflow.
    """
    if db.scalar("SELECT 1 FROM units WHERE source_type = ? LIMIT 1", (SIMULATOR,)):
        return SUMMARY

    profiles = _well_profiles(seed)
    with db.transaction(None, "seed.generate", "database", "platform") as connection:
        connection.executemany(
            """
            INSERT INTO units(id, code, name, status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, 'active', ?, ?, ?)
            """,
            [
                (f"U-{index:02d}", f"CY-{index:02d}", name, SIMULATOR, GENERATED_AT, GENERATED_AT)
                for index, name in enumerate(UNIT_NAMES, start=1)
            ],
        )
        connection.executemany(
            """
            INSERT INTO well_groups(id, unit_id, code, name, status, source_type, created_at, updated_at)
            VALUES(?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            [
                (f"G-{index:02d}", f"U-{(index + 1) // 2:02d}", f"JG-{index:02d}",
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
                (profile["device_id"], profile["well_id"], f"SIM-DSP-{profile['index']:03d}",
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
    return SUMMARY


def _well_profiles(seed: int) -> list[dict]:
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
                "well_id": f"W-{index:03d}",
                "group_id": f"G-{(index - 1) // 6 + 1:02d}",
                "code": f"CYJ-{index:03d}",
                "name": f"抽油井 {index:03d}",
                "device_id": f"D-{index:03d}",
                "channel_id": f"CH-{index:03d}-POWER",
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
            day = PRODUCTION_START + timedelta(days=offset)
            variation = 1.0 + 0.035 * math.sin((offset + profile["index"]) / 5.0)
            daily_fluid = profile["fluid_target"] * variation
            if profile["index"] == 1 and offset == 0:
                daily_fluid = 2.846
            runtime_hours = profile["runtime_hours"] + 0.35 * math.sin((offset + profile["index"]) / 7.0)
            daily_oil = daily_fluid * (0.61 + profile["pump_efficiency"] * 0.08)
            power_kwh = profile["power_kw"] * runtime_hours * (0.94 + 0.02 * math.cos(offset / 4.0))
            rows.append(
                (
                    profile["well_id"], day.isoformat(), round(daily_oil, 3), round(daily_fluid, 3),
                    round(power_kwh, 3), round(profile["pump_efficiency"], 4), round(profile["stroke_m"], 3),
                    round(profile["frequency_hz"], 3), round(runtime_hours, 3), SIMULATOR,
                    f"{day.isoformat()}T23:00:00.000+08:00", f"{day.isoformat()}T23:00:00.000+08:00",
                )
            )
    return rows


def _telemetry_rows(profiles: list[dict]) -> list[tuple]:
    rows = []
    for profile in profiles:
        for offset in range(30 * 24):
            measured_at = TELEMETRY_START + timedelta(hours=offset)
            hour = measured_at.hour
            running_factor = 0.58 if profile["low_output"] and hour < 12 else 1.0
            fluctuation = 0.97 + 0.03 * math.sin((offset + profile["index"]) / 9.0)
            power_kw = profile["power_kw"] * running_factor * fluctuation
            timestamp = measured_at.isoformat(timespec="milliseconds")
            rows.append(
                (
                    profile["well_id"], profile["device_id"], profile["channel_id"], timestamp,
                    round(power_kw, 3), SIMULATOR, timestamp, timestamp,
                )
            )
    return rows


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
            (f"DG-{profile['index']:03d}", profile["well_id"], GENERATED_AT,
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
            (f"DG-{profile['index']:03d}", feature, value, SIMULATOR, GENERATED_AT, GENERATED_AT)
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
            (f"MR-{profile['index']:03d}", f"DG-{profile['index']:03d}", SIMULATOR, GENERATED_AT, GENERATED_AT)
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
            (f"DX-{profile['index']:03d}", profile["well_id"], f"DG-{profile['index']:03d}",
             f"MR-{profile['index']:03d}",
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
    _insert_users_and_report(connection)


def _insert_low_output_workflows(connection, low_profiles: list[dict]) -> None:
    connection.executemany(
        """
        INSERT INTO recovery_tests(id, well_id, tested_at, result_data, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'completed', ?, ?, ?)
        """,
        [
            (f"RT-{profile['index']:03d}", profile["well_id"], GENERATED_AT,
             json.dumps({"recovery_hours": 8, "fluid_target": round(profile["fluid_target"], 3)}, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO energy_strategies(id, well_id, name, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'active', ?, ?, ?)
        """,
        [
            (f"ES-{profile['index']:03d}", profile["well_id"], f"{profile['name']} 间抽节能策略",
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO strategy_versions(id, strategy_id, version_number, configuration, status, source_type,
                                      created_at, updated_at)
        VALUES(?, ?, 1, ?, 'effective', ?, ?, ?)
        """,
        [
            (f"SV-{profile['index']:03d}", f"ES-{profile['index']:03d}",
             json.dumps({"run_hours": round(profile["runtime_hours"], 2), "stop_hours": 8,
                         "minimum_daily_fluid": round(profile["fluid_target"] * 0.95, 3)}, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO device_commands(id, device_id, strategy_version_id, command_type, payload, status,
                                    source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'set_intermittent_cycle', ?, 'succeeded', ?, ?, ?)
        """,
        [
            (f"CMD-{profile['index']:03d}", profile["device_id"], f"SV-{profile['index']:03d}",
             json.dumps({"frequency_hz": round(profile["frequency_hz"], 3)}, sort_keys=True),
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO command_attempts(command_id, attempted_at, result_detail, status, source_type,
                                     created_at, updated_at)
        VALUES(?, ?, 'virtual controller accepted command', 'succeeded', ?, ?, ?)
        """,
        [
            (f"CMD-{profile['index']:03d}", GENERATED_AT, SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO alarms(id, well_id, diagnosis_id, alarm_type, severity, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'supply_shortage', 'warning', 'dispatched', ?, ?, ?)
        """,
        [
            (f"AL-{profile['index']:03d}", profile["well_id"], f"DX-{profile['index']:03d}",
             SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO approvals(id, object_type, object_id, decision, status, source_type, created_at, updated_at)
        VALUES(?, 'energy_strategy', ?, 'approved', 'approved', ?, ?, ?)
        """,
        [
            (f"AP-{profile['index']:03d}", f"ES-{profile['index']:03d}", SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_orders(id, well_id, alarm_id, title, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'processing', ?, ?, ?)
        """,
        [
            (f"WO-{profile['index']:03d}", profile["well_id"], f"AL-{profile['index']:03d}",
             f"{profile['name']} 供液不足处置", SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )
    connection.executemany(
        """
        INSERT INTO work_order_logs(work_order_id, message, status, source_type, created_at, updated_at)
        VALUES(?, '已下发间抽策略，待复核产液效果', 'processing', ?, ?, ?)
        """,
        [
            (f"WO-{profile['index']:03d}", SIMULATOR, GENERATED_AT, GENERATED_AT)
            for profile in low_profiles
        ],
    )


def _insert_users_and_report(connection) -> None:
    roles = (("administrator", "管理员"), ("production_engineer", "生产工程师"),
             ("algorithm_engineer", "算法工程师"), ("maintenance_engineer", "设备运维"),
             ("reviewer", "只读评审"))
    connection.executemany(
        """
        INSERT INTO roles(code, name, status, source_type, created_at, updated_at)
        VALUES(?, ?, 'active', ?, ?, ?)
        """,
        [(code, name, SIMULATOR, GENERATED_AT, GENERATED_AT) for code, name in roles],
    )
    role_ids = {row["code"]: row["id"] for row in connection.execute("SELECT id, code FROM roles")}
    connection.executemany(
        """
        INSERT INTO users(role_id, username, display_name, status, source_type, created_at, updated_at)
        VALUES(?, ?, ?, 'active', ?, ?, ?)
        """,
        [(role_ids[code], code, name, SIMULATOR, GENERATED_AT, GENERATED_AT) for code, name in roles],
    )
    connection.execute(
        """
        INSERT INTO reports(id, report_type, period_start, period_end, payload, status, source_type, created_at, updated_at)
        VALUES('RPT-SEED-001', 'production_daily', '2026-06-01T00:00:00.000+08:00',
               '2026-06-30T23:00:00.000+08:00', ?, 'generated', ?, ?, ?)
        """,
        (json.dumps({"source": SIMULATOR, "well_count": 48}, sort_keys=True), SIMULATOR, GENERATED_AT, GENERATED_AT),
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
