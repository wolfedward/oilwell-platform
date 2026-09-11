"""Run a complete acceptance chain against an isolated local database."""

import argparse
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import traceback

from backend.http_server import create_server


SHANGHAI = timezone(timedelta(hours=8))
CONSTRAINTS = {
    "min_on_minutes": 20,
    "max_on_minutes": 120,
    "min_off_minutes": 15,
    "max_off_minutes": 180,
    "min_liquid_m3_day": 2.4,
    "max_starts_per_day": 12,
    "motor_kw": 18.5,
}


class SmokeRunner:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.server = None
        self.thread = None
        self.stages = []

    def record(self, name, **evidence):
        self.stages.append({"name": name, "status": "passed", "evidence": evidence})

    def start(self):
        self.server = create_server("127.0.0.1", 0, database_path=self.database_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.thread.join(timeout=10)
        self.server.server_close()
        self.server = None
        self.thread = None

    def request(self, method, path, token=None, body=None, expected=200, *, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            if response.status != expected:
                raise AssertionError(f"{method} {path}: expected {expected}, got {response.status}: {payload[:500]!r}")
            if raw:
                return payload, response.getheader("Content-Type", "")
            decoded = json.loads(payload.decode("utf-8"))
            if not decoded.get("ok"):
                raise AssertionError(f"{method} {path}: API returned ok=false")
            return decoded["data"]
        finally:
            connection.close()

    def login(self, username, password):
        return self.request(
            "POST", "/api/auth/login", body={"username": username, "password": password}
        )["token"]

    def run(self):
        self.start()
        health = self.request("GET", "/api/health")
        assert health["service"] == health["database"] == "ok"
        self.record("本机服务与数据库健康检查", version=health["version"])

        tokens = {
            "admin": self.login("administrator", "Admin@2026"),
            "production": self.login("production_engineer", "Production@2026"),
            "algorithm": self.login("algorithm_engineer", "Algorithm@2026"),
            "maintenance": self.login("maintenance_engineer", "Maintenance@2026"),
        }
        self.record("四岗位登录与权限会话", accounts=list(tokens))

        dashboard = self.request("GET", "/api/dashboard", tokens["admin"])
        required = {"production", "devices", "quality", "alarms", "strategies", "energy", "trends"}
        assert required.issubset(dashboard)
        self.record("综合看板读取", sections=sorted(required))

        well = self.request("GET", "/api/wells/W-001", tokens["admin"])
        assert well["well"]["id"] == "W-001" and well.get("device")
        self.record("单井与终端追溯", well_id=well["well"]["id"], device_id=well["device"]["id"])

        dynagraphs = self.request("GET", "/api/dynagraphs?page_size=1", tokens["admin"])
        dynagraph_id = dynagraphs["items"][0]["id"]
        analysis = self.request(
            "POST", f"/api/dynagraphs/{dynagraph_id}/analyze", tokens["algorithm"], {}
        )
        assert analysis["model_run"]["status"] == "completed" and analysis.get("diagnosis")
        self.record(
            "电功图转换与智能诊断", dynagraph_id=dynagraph_id,
            diagnosis_id=analysis["diagnosis"]["id"],
            model_version=analysis["diagnosis"]["model_version"],
        )

        strategy = self.request(
            "POST", "/api/strategies/optimize", tokens["production"],
            {"well_id": "W-001", "constraints": CONSTRAINTS}, expected=201,
        )
        submitted = self.request(
            "POST", f"/api/strategies/{strategy['id']}/submit", tokens["production"],
            {"version": strategy["version"]},
        )
        approved = self.request(
            "POST", f"/api/strategies/{strategy['id']}/approve", tokens["admin"],
            {"version": submitted["version"], "comment": "安全约束、产量保持与启停次数复核通过"},
        )
        self.record("策略优化与独立审批", strategy_id=strategy["id"], status=approved["status"])

        command = self.request(
            "POST", f"/api/strategies/{strategy['id']}/dispatch",
            tokens["maintenance"], {}, expected=202,
        )
        self.request("POST", "/api/commands/process", tokens["maintenance"], {})
        command_detail = self.request("GET", f"/api/commands/{command['id']}", tokens["admin"])
        assert command_detail["state"] == "success" and len(command_detail["receipts"]) == 5
        assert len(command_detail["signature"]) == 64
        self.record(
            "签名指令与五阶段回执", command_id=command["id"],
            state=command_detail["state"], receipts=len(command_detail["receipts"]),
        )

        csv_body, content_type = self.request(
            "GET", "/api/reports/production.csv?unit_id=U-01", tokens["admin"], raw=True
        )
        assert len(csv_body) > 100 and "text/csv" in content_type
        self.record("生产报表导出", bytes=len(csv_body), content_type=content_type)

        audit = self.request(
            "GET", "/api/audit?object_type=device_command&page_size=100", tokens["admin"]
        )
        assert any(item["object_id"] == command["id"] for item in audit["items"])
        evidence = self.request("GET", "/api/acceptance/evidence", tokens["admin"])
        assert len(evidence["requirements"]) == 6
        self.record("审计链与年度任务证据", audit_object=command["id"], requirements=6)

        self.stop()
        self.start()
        restarted_admin = self.login("administrator", "Admin@2026")
        persisted_strategy = self.request(
            "GET", f"/api/strategies/{strategy['id']}", restarted_admin
        )
        persisted_command = self.request(
            "GET", f"/api/commands/{command['id']}", restarted_admin
        )
        assert persisted_strategy["status"] == "effective"
        assert persisted_command["state"] == "success" and len(persisted_command["receipts"]) == 5
        self.record(
            "服务重启后闭环记录保持", strategy_id=strategy["id"], command_id=command["id"]
        )


def arguments():
    parser = argparse.ArgumentParser(description="执行本机全链路验收巡检并输出 JSON 报告。")
    parser.add_argument("--output", type=Path, help="指定巡检报告文件")
    return parser.parse_args()


def main():
    options = arguments()
    started = datetime.now(SHANGHAI)
    report = {"started_at": started.isoformat(), "status": "failed", "stages": []}
    runner = None
    with tempfile.TemporaryDirectory(prefix="oil-well-acceptance-") as temporary:
        try:
            runner = SmokeRunner(Path(temporary) / "platform.db")
            runner.run()
            report["status"] = "passed"
        except BaseException as error:
            report["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        finally:
            if runner is not None:
                runner.stop()
                report["stages"] = runner.stages
    finished = datetime.now(SHANGHAI)
    report["finished_at"] = finished.isoformat()
    report["duration_seconds"] = round((finished - started).total_seconds(), 3)
    output = options.output or Path("logs") / f"acceptance-smoke-{started:%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output.resolve()), "stages": len(report["stages"])}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
