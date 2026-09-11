"""HTTP route adapter for dashboard and well query services."""

from http import HTTPStatus
import csv
import io
import re
from urllib.parse import parse_qs, unquote, urlsplit

from backend.services.wells import WellNotFound, WellValidationError, WellVersionConflict, WellsService
from backend.services.devices import ChannelVersionConflict, DeviceNotFound, DevicesService, DeviceValidationError
from backend.services.acquisition import (
    AcquisitionNotFound, AcquisitionService, AcquisitionValidationError, AcquisitionVersionConflict,
)
from backend.services.analysis import (
    AnalysisNotFound, AnalysisService, AnalysisValidationError, AnalysisVersionConflict,
)
from backend.services.commands import CommandNotFound, CommandService, CommandValidationError
from backend.services.strategies import (
    StrategyNotFound, StrategyService, StrategyValidationError,
    StrategyVersionConflict,
)
from backend.services.workflows import (
    WorkflowNotFound, WorkflowService, WorkflowValidationError,
    WorkflowVersionConflict,
)
from backend.services.reports import ReportNotFound, ReportService, ReportValidationError
from backend.state_machine import InvalidState


class WellApi:
    """Keep endpoint parsing out of the generic HTTP/static server handler."""

    def __init__(self, handler):
        self.handler = handler
        self.service = WellsService(handler.server.database, handler.server.audit)

    def get(self, request_target: str) -> bool:
        parsed = urlsplit(request_target)
        path = parsed.path
        try:
            if path == "/api/dashboard":
                if parsed.query:
                    raise WellValidationError("dashboard does not accept query parameters")
                return self._dashboard()
            if path == "/api/wells.csv":
                return self._csv(parsed.query)
            if path == "/api/wells":
                return self._list(parsed.query)
            segments = self._well_segments(path)
            if segments is not None:
                if len(segments) == 1:
                    if parsed.query:
                        raise WellValidationError("well detail does not accept query parameters")
                    return self._detail(segments[0])
                if len(segments) == 2 and segments[1] == "telemetry":
                    return self._telemetry(segments[0], parsed.query)
                return False
        except WellValidationError as error:
            self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error)))
            return True
        except WellNotFound:
            self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Well not found"))
            return True
        return False

    def patch(self, request_target: str) -> bool:
        path = urlsplit(request_target).path
        segments = self._well_segments(path)
        if segments is None or len(segments) != 2 or segments[1] != "status":
            return False
        well_id = segments[0]
        actor = self.handler._require("wells.update")
        if actor is None:
            return True
        try:
            body = self.handler._json_body()
            result = self.service.update_status(actor.id, well_id, body.get("status"), body.get("reason"), body.get("version"))
        except WellNotFound:
            self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Well not found"))
        except WellVersionConflict:
            self.handler._send_json(HTTPStatus.CONFLICT, self.handler._error("version_conflict", "Record version is stale"))
        except WellValidationError as error:
            self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error)))
        except ValueError as error:
            self.handler._send_json(HTTPStatus.BAD_REQUEST, self.handler._error("invalid_request", str(error)))
        else:
            self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": result})
        return True

    def _dashboard(self):
        if self.handler._require("dashboard.read") is not None:
            self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.dashboard()})
        return True

    def _list(self, query):
        if self.handler._require("wells.read") is None:
            return True
        values = self._query(query, {"unit_id", "group_id", "status", "low_output", "keyword", "page", "page_size"})
        if "low_output" in values and values["low_output"] not in {"0", "1"}:
            raise WellValidationError("low_output must be 0 or 1")
        page = self._integer(values.get("page", "1"), "page", 1, self.MAX_PAGE)
        page_size = self._integer(values.get("page_size", "20"), "page_size", 1, 100)
        if (page - 1) * page_size > self.SQLITE_MAX_INTEGER:
            raise WellValidationError("page offset is out of range")
        self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.list_wells({**values, "page": page, "page_size": page_size})})
        return True

    def _csv(self, query):
        if self.handler._require("wells.read") is None:
            return True
        values = self._query(query, {"unit_id", "group_id", "status", "low_output", "keyword"})
        if "low_output" in values and values["low_output"] not in {"0", "1"}:
            raise WellValidationError("low_output must be 0 or 1")
        result = self.service.list_wells({**values, "page": 1, "page_size": self.SQLITE_MAX_INTEGER})
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(("井号", "油井名称", "单位", "井组", "运行状态", "低产井", "生产日期", "日产液(m3)", "日产油(t)", "最新诊断", "活动报警"))
        for item in result["items"]:
            def safe(value):
                text = "" if value is None else str(value)
                return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text
            production = item.get("latest_production") or {}
            diagnosis = item.get("latest_diagnosis") or {}
            writer.writerow(tuple(safe(value) for value in (
                item["code"], item["name"], item["unit"]["name"], item["group"]["name"],
                item["status"], "是" if item["low_output"] else "否", production.get("day"),
                production.get("daily_fluid"), production.get("daily_oil"), diagnosis.get("diagnosis_type"),
                item["active_alarm_count"],
            )))
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.handler.send_response(HTTPStatus.OK)
        self.handler.send_header("Content-Type", "text/csv; charset=utf-8")
        self.handler.send_header("Content-Disposition", 'attachment; filename="well-ledger.csv"')
        self.handler.send_header("Cache-Control", "no-store")
        self.handler.send_header("X-Content-Type-Options", "nosniff")
        self.handler.send_header("Content-Length", str(len(body)))
        self.handler.end_headers()
        self.handler.wfile.write(body)
        return True

    def _detail(self, well_id):
        if self.handler._require("wells.read") is None:
            return True
        if not self.WELL_ID_PATTERN.fullmatch(well_id):
            raise WellValidationError("invalid well id")
        self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.detail(well_id)})
        return True

    def _telemetry(self, well_id, query):
        if self.handler._require("telemetry.read") is None:
            return True
        if not self.WELL_ID_PATTERN.fullmatch(well_id):
            raise WellValidationError("invalid well id")
        values = self._query(query, {"from", "to", "interval"})
        self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.telemetry(well_id, values.get("from"), values.get("to"), values.get("interval", "raw"))})
        return True

    @staticmethod
    def _query(query, allowed):
        parsed = parse_qs(query, keep_blank_values=True, strict_parsing=False)
        if set(parsed) - allowed or any(len(values) != 1 for values in parsed.values()):
            raise WellValidationError("invalid query parameters")
        return {key: values[0] for key, values in parsed.items()}

    @staticmethod
    def _well_segments(path):
        prefix = "/api/wells/"
        if not path.startswith(prefix):
            return None
        suffix = path.removeprefix(prefix)
        if not suffix:
            return ()
        segments = tuple(unquote(segment) for segment in suffix.split("/"))
        # A decoded separator/control character changes route structure.  It
        # is therefore not a malformed well ID; it is not a route match.
        if any(
            "/" in segment or "\\" in segment or any(ord(character) < 32 or ord(character) == 127 for character in segment)
            for segment in segments
        ):
            return None
        return segments

    @staticmethod
    def _integer(value, name, minimum, maximum):
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise WellValidationError(f"{name} must be an integer") from None
        if str(number) != value or number < minimum or (maximum is not None and number > maximum):
            raise WellValidationError(f"{name} is out of range")
        return number
    MAX_PAGE = 1_000_000
    SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
    WELL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class DeviceApi:
    """Device routes use the same handler auth/error contract as well routes."""
    MAX_PAGE = 1_000_000
    SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
    def __init__(self, handler): self.handler = handler; self.service = DevicesService(handler.server.database, handler.server.audit, handler.server.simulator)
    def get(self, request_target):
        parsed = urlsplit(request_target); path = parsed.path
        try:
            if path == "/api/devices":
                if self.handler._require("devices.read") is None: return True
                query = WellApi._query(parsed.query, {"unit", "unit_id", "well", "well_id", "status", "keyword", "page", "page_size"})
                if {"unit", "unit_id"}.issubset(query) or {"well", "well_id"}.issubset(query):
                    raise DeviceValidationError("duplicate alias query parameters")
                if "unit" in query and "unit_id" not in query: query["unit_id"] = query.pop("unit")
                if "well" in query and "well_id" not in query: query["well_id"] = query.pop("well")
                page = WellApi._integer(query.get("page", "1"), "page", 1, self.MAX_PAGE); size = WellApi._integer(query.get("page_size", "20"), "page_size", 1, 100)
                if (page - 1) * size > self.SQLITE_MAX_INTEGER: raise DeviceValidationError("page offset is out of range")
                self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.list_devices({**query, "page": page, "page_size": size})}); return True
            segments = self._segments(path)
            if segments is None: return False
            actor = self.handler._require("devices.read")
            if actor is None: return True
            if parsed.query: raise DeviceValidationError("device detail does not accept query parameters")
            if len(segments) == 1:
                payload = self.service.detail(segments[0], include_commands="commands.read" in actor.permissions)
                payload["capabilities"]["scenario_control"] = "devices.control" in actor.permissions
                payload["capabilities"]["channel_configuration"] = "devices.configure" in actor.permissions
            elif len(segments) == 2 and segments[1] == "registers": payload = self.service.registers(segments[0])
            else: return False
            self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": payload}); return True
        except DeviceNotFound: self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Device not found")); return True
        except DeviceValidationError as error: self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error))); return True
    def post(self, request_target):
        parsed = urlsplit(request_target)
        segments = self._segments(parsed.path)
        if segments is None or len(segments) != 2 or segments[1] != "scenarios": return False
        actor = self.handler._require("devices.control")
        if actor is None: return True
        try:
            if parsed.query: raise DeviceValidationError("device scenario does not accept query parameters")
            body = self.handler._json_body()
            if not isinstance(body, dict) or set(body) != {"name", "duration_minutes"}:
                raise DeviceValidationError("scenario body must contain exactly name and duration_minutes")
            result = self.service.apply_scenario(actor.id, segments[0], body["name"], body["duration_minutes"])
            self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": result})
        except DeviceNotFound: self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Device not found"))
        except (DeviceValidationError, ValueError) as error: self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error)))
        return True
    def patch(self, request_target):
        parsed = urlsplit(request_target)
        segments = self._segments(parsed.path)
        if segments is None or len(segments) != 3 or segments[1] != "channels": return False
        actor = self.handler._require("devices.configure")
        if actor is None: return True
        try:
            if parsed.query: raise DeviceValidationError("device channel mutation does not accept query parameters")
            result = self.service.update_channel(actor.id, segments[0], segments[2], self.handler._json_body()); self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": result})
        except DeviceNotFound: self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Device or channel not found"))
        except ChannelVersionConflict: self.handler._send_json(HTTPStatus.CONFLICT, self.handler._error("version_conflict", "Record version is stale"))
        except (DeviceValidationError, ValueError) as error: self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error)))
        return True
    @staticmethod
    def _segments(path):
        prefix = "/api/devices/"
        if not path.startswith(prefix): return None
        parts = tuple(unquote(value) for value in path.removeprefix(prefix).split("/"))
        return None if not parts or any(not value or "/" in value or "\\" in value or any(ord(c) < 32 for c in value) for value in parts) else parts


class AcquisitionApi:
    """Authenticated finite acquisition feed, evidence, and repair routes."""

    def __init__(self, handler):
        self.handler = handler
        self.service: AcquisitionService = handler.server.acquisition

    def get(self, request_target):
        parsed = urlsplit(request_target)
        path = parsed.path
        try:
            if path == "/api/acquisition/stream":
                if self.handler._require("acquisition.read") is None: return True
                query = WellApi._query(parsed.query, {"after_id", "limit"})
                after_id = WellApi._integer(query.get("after_id", "0"), "after_id", 0, WellApi.SQLITE_MAX_INTEGER)
                limit = WellApi._integer(query.get("limit", "100"), "limit", 1, 500)
                return self._ok(self.service.stream(after_id, limit))
            if path == "/api/acquisition/messages":
                if self.handler._require("acquisition.read") is None: return True
                query = WellApi._query(parsed.query, {"device_id", "status", "kind", "page", "page_size"})
                page, size = self._page(query)
                self._filters(query)
                return self._ok(self.service.list_messages({**query, "page": page, "page_size": size}))
            message_id = self._message_id(path)
            if message_id is not None:
                if self.handler._require("acquisition.read") is None: return True
                if parsed.query:
                    raise AcquisitionValidationError("message detail does not accept query parameters")
                return self._ok(self.service.message_detail(message_id))
            if path == "/api/data-quality":
                if self.handler._require("data_quality.read") is None: return True
                query = WellApi._query(parsed.query, {"device_id", "status", "kind", "page", "page_size"})
                page, size = self._page(query)
                self._filters(query)
                return self._ok(self.service.list_quality({**query, "page": page, "page_size": size}))
            return False
        except AcquisitionNotFound:
            self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Acquisition record not found")); return True
        except (AcquisitionValidationError, WellValidationError) as error:
            self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error))); return True

    def post(self, request_target):
        try:
            parsed = urlsplit(request_target)
            event_id = self._repair_event_id(parsed.path)
            if event_id is None: return False
            actor = self.handler._require("data_quality.repair")
            if actor is None: return True
            if parsed.query:
                raise AcquisitionValidationError("quality repair does not accept query parameters")
            body = self.handler._json_body()
            if not isinstance(body, dict) or set(body) != {"version"}:
                raise AcquisitionValidationError("repair body must contain exactly version")
            result = self.service.repair_event(actor.id, event_id, body["version"])
            return self._ok(result.__dict__)
        except AcquisitionNotFound:
            self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Quality event not found")); return True
        except AcquisitionVersionConflict:
            self.handler._send_json(HTTPStatus.CONFLICT, self.handler._error("version_conflict", "Quality event version is stale")); return True
        except (AcquisitionValidationError, ValueError) as error:
            self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error))); return True

    def _ok(self, data):
        self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": data})
        return True

    @staticmethod
    def _page(query):
        page = WellApi._integer(query.get("page", "1"), "page", 1, WellApi.MAX_PAGE)
        size = WellApi._integer(query.get("page_size", "20"), "page_size", 1, 100)
        if (page - 1) * size > WellApi.SQLITE_MAX_INTEGER:
            raise AcquisitionValidationError("page offset is out of range")
        return page, size

    @staticmethod
    def _filters(query):
        device_id = query.get("device_id")
        if device_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", device_id):
            raise AcquisitionValidationError("invalid device id")
        if query.get("status") and query["status"] not in {"received", "accepted", "replayed", "duplicate", "rejected", "open", "repaired", "closed"}:
            raise AcquisitionValidationError("invalid status")
        if query.get("kind") and not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", query["kind"]):
            raise AcquisitionValidationError("invalid kind")

    @staticmethod
    def _message_id(path):
        match = re.fullmatch(r"/api/acquisition/messages/([1-9][0-9]{0,18})", path)
        if not match: return None
        value = int(match.group(1))
        if value > WellApi.SQLITE_MAX_INTEGER: raise AcquisitionValidationError("message id is out of range")
        return value

    @staticmethod
    def _repair_event_id(path):
        match = re.fullmatch(r"/api/data-quality/([1-9][0-9]{0,18})/repair", path)
        if not match: return None
        value = int(match.group(1))
        if value > WellApi.SQLITE_MAX_INTEGER: raise AcquisitionValidationError("quality event id is out of range")
        return value


class AnalysisApi:
    """Traceable dynagraph conversion, diagnosis, production and review routes."""

    def __init__(self, handler):
        self.handler = handler
        self.service = AnalysisService(handler.server.database, handler.server.audit)

    def get(self, request_target):
        parsed = urlsplit(request_target)
        try:
            if parsed.path == "/api/diagnoses/review-catalog":
                if self.handler._require("reports.read") is None:
                    return True
                if parsed.query:
                    raise AnalysisValidationError(
                        "review catalog does not accept query parameters"
                    )
                return self._ok({
                    "items": [
                        {"code": code, "label": label}
                        for code, label in self.service.review_catalog()
                    ] + [{"code": "custom", "label": "自定义工况"}],
                })
            if parsed.path == "/api/dynagraphs":
                if self.handler._require("reports.read") is None:
                    return True
                query = WellApi._query(
                    parsed.query, {"well_id", "status", "page", "page_size"}
                )
                page, page_size = self._page(query)
                return self._ok(self.service.list_dynagraphs(
                    {**query, "page": page, "page_size": page_size}
                ))
            if parsed.path == "/api/model-runs":
                if self.handler._require("reports.read") is None:
                    return True
                query = WellApi._query(
                    parsed.query, {"dynagraph_id", "status", "page", "page_size"}
                )
                page, page_size = self._page(query)
                return self._ok(self.service.model_runs(
                    {**query, "page": page, "page_size": page_size}
                ))
            match = re.fullmatch(
                r"/api/dynagraphs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})",
                parsed.path,
            )
            if match:
                if self.handler._require("reports.read") is None:
                    return True
                if parsed.query:
                    raise AnalysisValidationError(
                        "dynagraph detail does not accept query parameters"
                    )
                return self._ok(self.service.detail(unquote(match.group(1))))
            return False
        except AnalysisNotFound:
            self.handler._send_json(
                HTTPStatus.NOT_FOUND,
                self.handler._error("not_found", "Analysis record not found"),
            )
        except (AnalysisValidationError, WellValidationError, ValueError) as error:
            self.handler._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self.handler._error("validation_error", str(error)),
            )
        return True

    def post(self, request_target):
        parsed = urlsplit(request_target)
        analyze = re.fullmatch(
            r"/api/dynagraphs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/analyze",
            parsed.path,
        )
        review = re.fullmatch(
            r"/api/diagnoses/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/review",
            parsed.path,
        )
        if analyze is None and review is None:
            return False
        try:
            if parsed.query:
                raise AnalysisValidationError(
                    "analysis mutation does not accept query parameters"
                )
            if analyze:
                actor = self.handler._require("diagnosis.run")
                if actor is None:
                    return True
                body = self.handler._json_body()
                if set(body):
                    raise AnalysisValidationError("analyze body must be an empty object")
                return self._ok(self.service.run(unquote(analyze.group(1)), actor.id))
            actor = self.handler._require("diagnosis.review")
            if actor is None:
                return True
            body = self.handler._json_body()
            allowed_review_fields = {"condition", "comment", "version", "custom_condition"}
            if set(body) - allowed_review_fields:
                raise AnalysisValidationError("review body contains unknown fields")
            required_review_fields = {"condition", "comment", "version"}
            if not required_review_fields.issubset(body):
                raise AnalysisValidationError(
                    "review body must contain condition, comment and version"
                )
            if body.get("condition") == "custom":
                if set(body) != required_review_fields | {"custom_condition"}:
                    raise AnalysisValidationError(
                        "custom review body must contain exactly condition, custom_condition, comment and version"
                    )
            elif set(body) != required_review_fields:
                raise AnalysisValidationError(
                    "standard review body must contain exactly condition, comment and version"
                )
            return self._ok(self.service.review(
                unquote(review.group(1)), actor.id, body["condition"],
                body["comment"], body["version"],
                custom_condition=body.get("custom_condition"),
            ))
        except AnalysisNotFound:
            self.handler._send_json(
                HTTPStatus.NOT_FOUND,
                self.handler._error("not_found", "Analysis record not found"),
            )
        except AnalysisVersionConflict:
            self.handler._send_json(
                HTTPStatus.CONFLICT,
                self.handler._error("version_conflict", "Diagnosis version is stale"),
            )
        except (AnalysisValidationError, ValueError) as error:
            self.handler._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self.handler._error("validation_error", str(error)),
            )
        return True

    def _ok(self, data):
        self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": data})
        return True

    @staticmethod
    def _page(query):
        page = WellApi._integer(query.get("page", "1"), "page", 1, WellApi.MAX_PAGE)
        page_size = WellApi._integer(query.get("page_size", "20"), "page_size", 1, 100)
        if (page - 1) * page_size > WellApi.SQLITE_MAX_INTEGER:
            raise AnalysisValidationError("page offset is out of range")
        return page, page_size


class CommandApi:
    """Signed control-command dispatch, receipt and rollback routes."""

    def __init__(self, handler):
        self.handler = handler
        self.service = CommandService(
            handler.server.database, handler.server.audit, handler.server.simulator,
        )

    def get(self, request_target):
        parsed = urlsplit(request_target)
        if parsed.path == "/api/commands":
            try:
                if self.handler._require("commands.read") is None:
                    return True
                query = WellApi._query(
                    parsed.query,
                    {"device_id", "strategy_id", "state", "page", "page_size"},
                )
                page, page_size = self._page(query)
                return self._ok(self.service.list_commands(
                    {**query, "page": page, "page_size": page_size}
                ))
            except (CommandValidationError, WellValidationError, ValueError) as error:
                return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)
        command_id = self._match_command(parsed.path)
        if command_id is None:
            return False
        try:
            if self.handler._require("commands.read") is None:
                return True
            if parsed.query:
                raise CommandValidationError("command detail does not accept query parameters")
            return self._ok(self.service.get(command_id))
        except CommandNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Command not found")
        except (CommandValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def post(self, request_target):
        parsed = urlsplit(request_target)
        if parsed.path == "/api/commands/process":
            action, strategy_id = "process", None
        else:
            matched = None
            for candidate in ("dispatch", "rollback"):
                identifier = self._match_strategy(parsed.path, candidate)
                if identifier is not None:
                    matched = candidate, identifier
                    break
            if matched is None:
                return False
            action, strategy_id = matched
        try:
            if parsed.query:
                raise CommandValidationError("command mutation does not accept query parameters")
            actor = self.handler._require("devices.control")
            if actor is None:
                return True
            body = self.handler._json_body()
            if action in {"dispatch", "process"} and set(body):
                raise CommandValidationError(f"{action} body must be an empty object")
            if action == "dispatch":
                return self._ok(
                    self.service.dispatch_strategy(strategy_id, actor.id),
                    HTTPStatus.ACCEPTED,
                )
            if action == "rollback":
                if set(body) != {"reason"}:
                    raise CommandValidationError(
                        "rollback body must contain exactly reason"
                    )
                return self._ok(self.service.rollback(
                    strategy_id, actor.id, body["reason"],
                ), HTTPStatus.ACCEPTED)
            return self._ok(self.service.process())
        except CommandNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Command not found")
        except InvalidState as error:
            return self._error(HTTPStatus.CONFLICT, "invalid_state", error)
        except (CommandValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def _ok(self, data, status=HTTPStatus.OK):
        self.handler._send_json(status, {"ok": True, "data": data})
        return True

    def _error(self, status, code, message):
        self.handler._send_json(status, self.handler._error(code, str(message)))
        return True

    @staticmethod
    def _match_command(path):
        match = re.fullmatch(
            r"/api/commands/([A-Za-z0-9][A-Za-z0-9_-]{0,127})", path,
        )
        return unquote(match.group(1)) if match else None

    @staticmethod
    def _match_strategy(path, action):
        match = re.fullmatch(
            rf"/api/strategies/([A-Za-z0-9][A-Za-z0-9_-]{{0,127}})/{action}", path,
        )
        return unquote(match.group(1)) if match else None

    @staticmethod
    def _page(query):
        page = WellApi._integer(query.get("page", "1"), "page", 1, WellApi.MAX_PAGE)
        page_size = WellApi._integer(
            query.get("page_size", "20"), "page_size", 1, 100,
        )
        if (page - 1) * page_size > WellApi.SQLITE_MAX_INTEGER:
            raise CommandValidationError("page offset is out of range")
        return page, page_size


class WorkflowApi:
    """Alarm confirmation, assignment, maintenance and trace routes."""

    def __init__(self, handler):
        self.handler = handler
        self.service = WorkflowService(handler.server.database, handler.server.audit)

    def get(self, request_target):
        parsed = urlsplit(request_target)
        path = parsed.path
        try:
            if path == "/api/alarms":
                if self.handler._require("workflow.read") is None:
                    return True
                query = WellApi._query(
                    parsed.query, {"well_id", "status", "severity", "page", "page_size"},
                )
                return self._ok(self.service.list_alarms(self._page(query)))
            alarm_id = self._match(path, "alarms")
            if alarm_id is not None:
                if self.handler._require("workflow.read") is None:
                    return True
                if parsed.query:
                    raise WorkflowValidationError("alarm detail does not accept query parameters")
                return self._ok(self.service.get_alarm(alarm_id))
            if path == "/api/work-orders":
                if self.handler._require("workflow.read") is None:
                    return True
                query = WellApi._query(
                    parsed.query, {"well_id", "status", "assignee_id", "page", "page_size"},
                )
                return self._ok(self.service.list_work_orders(self._page(query)))
            trace_id = self._match(path, "work-orders", "trace")
            if trace_id is not None:
                if self.handler._require("workflow.read") is None:
                    return True
                if parsed.query:
                    raise WorkflowValidationError("workflow trace does not accept query parameters")
                return self._ok(self.service.trace(trace_id))
            order_id = self._match(path, "work-orders")
            if order_id is not None:
                if self.handler._require("workflow.read") is None:
                    return True
                if parsed.query:
                    raise WorkflowValidationError("work-order detail does not accept query parameters")
                return self._ok(self.service.get_work_order(order_id))
            return False
        except WorkflowNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Workflow record not found")
        except (WorkflowValidationError, WellValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def post(self, request_target):
        parsed = urlsplit(request_target)
        path = parsed.path
        route = None
        resource_id = None
        if path == "/api/alarms":
            route = "create_alarm"
        elif path == "/api/work-orders":
            route = "create_order"
        elif path == "/api/work-orders/evaluate-overdue":
            route = "overdue"
        else:
            resource_id = self._match(path, "alarms", "transition")
            if resource_id is not None:
                route = "alarm_transition"
            else:
                resource_id = self._match(path, "work-orders", "transition")
                if resource_id is not None:
                    route = "order_transition"
        if route is None:
            return False
        try:
            if route == "order_transition":
                body = self.handler._json_body()
                if not isinstance(body, dict):
                    raise WorkflowValidationError("request body must be an object")
                permission = "diagnosis.review" if body.get("target") == "closed" else "maintenance.manage"
                actor = self.handler._require(permission)
            else:
                permission = "maintenance.manage" if route == "overdue" else "alarms.ack"
                actor = self.handler._require(permission)
                if actor is None:
                    return True
                body = self.handler._json_body()
            if actor is None:
                return True
            if parsed.query:
                raise WorkflowValidationError("workflow mutation does not accept query parameters")
            if not isinstance(body, dict):
                raise WorkflowValidationError("request body must be an object")
            if route == "create_alarm":
                if set(body) != {"source_type", "source_id", "severity", "details"}:
                    raise WorkflowValidationError(
                        "alarm body must contain source_type, source_id, severity and details"
                    )
                return self._ok(self.service.create_alarm(
                    body["source_type"], body["source_id"], body["severity"],
                    body["details"], actor.id,
                ), HTTPStatus.CREATED)
            if route == "alarm_transition":
                if set(body) != {"target", "comment"}:
                    raise WorkflowValidationError(
                        "alarm transition body must contain target and comment"
                    )
                return self._ok(self.service.transition_alarm(
                    resource_id, body["target"], actor.id, body["comment"],
                ))
            if route == "create_order":
                if not {"alarm_id", "kind", "assignee_id"}.issubset(body) or set(body) - {
                    "alarm_id", "kind", "assignee_id", "due_at",
                }:
                    raise WorkflowValidationError(
                        "work-order body must contain alarm_id, kind and assignee_id"
                    )
                return self._ok(self.service.create_work_order(
                    body["alarm_id"], body["kind"], body["assignee_id"], actor.id,
                    body.get("due_at"),
                ), HTTPStatus.CREATED)
            if route == "order_transition":
                if set(body) != {"target", "comment"}:
                    raise WorkflowValidationError(
                        "work-order transition body must contain target and comment"
                    )
                return self._ok(self.service.transition_work_order(
                    resource_id, body["target"], actor.id, body["comment"],
                ))
            if set(body):
                raise WorkflowValidationError("overdue evaluation body must be an empty object")
            return self._ok(self.service.evaluate_overdue())
        except WorkflowNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Workflow record not found")
        except WorkflowVersionConflict:
            return self._error(HTTPStatus.CONFLICT, "version_conflict", "Workflow version is stale")
        except InvalidState as error:
            return self._error(HTTPStatus.CONFLICT, "invalid_state", error)
        except (WorkflowValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def _ok(self, data, status=HTTPStatus.OK):
        self.handler._send_json(status, {"ok": True, "data": data})
        return True

    def _error(self, status, code, message):
        self.handler._send_json(status, self.handler._error(code, str(message)))
        return True

    @staticmethod
    def _match(path, resource, action=None):
        suffix = f"/{action}" if action else ""
        match = re.fullmatch(
            rf"/api/{resource}/([A-Za-z0-9][A-Za-z0-9_-]{{0,127}}){suffix}", path,
        )
        return unquote(match.group(1)) if match else None

    @staticmethod
    def _page(query):
        result = dict(query)
        result["page"] = WellApi._integer(
            result.get("page", "1"), "page", 1, WellApi.MAX_PAGE,
        )
        result["page_size"] = WellApi._integer(
            result.get("page_size", "20"), "page_size", 1, 100,
        )
        if (result["page"] - 1) * result["page_size"] > WellApi.SQLITE_MAX_INTEGER:
            raise WorkflowValidationError("page offset is out of range")
        return result


class ReportApi:
    """Computed JSON reports and spreadsheet-safe UTF-8 CSV downloads."""

    REPORT_PATTERN = re.compile(
        r"/api/reports/(production|diagnosis|data-quality|strategy|energy|work-orders)(\.csv)?"
    )
    QUERY_KEYS = {
        "unit_id", "well_id", "strategy_id", "from", "to", "status", "assignee_id",
    }

    def __init__(self, handler):
        self.handler = handler
        self.service = ReportService(handler.server.database)

    def get(self, request_target):
        parsed = urlsplit(request_target)
        match = self.REPORT_PATTERN.fullmatch(parsed.path)
        if match is None:
            return False
        if self.handler._require("reports.read") is None:
            return True
        try:
            filters = WellApi._query(parsed.query, self.QUERY_KEYS)
            if match.group(2):
                return self._csv(match.group(1), filters)
            self.handler._send_json(
                HTTPStatus.OK,
                {"ok": True, "data": self.service.report(match.group(1), filters)},
            )
        except ReportNotFound as error:
            self.handler._send_json(
                HTTPStatus.NOT_FOUND,
                self.handler._error("not_found", str(error)),
            )
        except (ReportValidationError, WellValidationError, ValueError) as error:
            self.handler._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self.handler._error("validation_error", str(error)),
            )
        return True

    def _csv(self, report_type, filters):
        body = self.service.csv(report_type, filters)
        self.handler.send_response(HTTPStatus.OK)
        self.handler.send_header("Content-Type", "text/csv; charset=utf-8")
        self.handler.send_header(
            "Content-Disposition", f'attachment; filename="{report_type}-report.csv"'
        )
        self.handler.send_header("Cache-Control", "no-store")
        self.handler.send_header("X-Content-Type-Options", "nosniff")
        self.handler.send_header("Content-Length", str(len(body)))
        self.handler.end_headers()
        self.handler.wfile.write(body)
        return True


class StrategyApi:
    """Adaptive pumping strategy versions and approval workflow routes."""

    def __init__(self, handler):
        self.handler = handler
        self.service = StrategyService(handler.server.database, handler.server.audit)

    def get(self, request_target):
        parsed = urlsplit(request_target)
        if parsed.path == "/api/strategies":
            try:
                if self.handler._require("reports.read") is None:
                    return True
                query = WellApi._query(
                    parsed.query, {"well_id", "status", "page", "page_size"},
                )
                page, page_size = self._page(query)
                return self._ok(self.service.list_strategies(
                    {**query, "page": page, "page_size": page_size}
                ))
            except (StrategyValidationError, WellValidationError, ValueError) as error:
                return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)
        match = self._match(parsed.path, "")
        if match is None:
            return False
        try:
            if self.handler._require("reports.read") is None:
                return True
            if parsed.query:
                raise StrategyValidationError("strategy detail does not accept query parameters")
            return self._ok(self.service.detail(match))
        except StrategyNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Strategy not found")
        except (StrategyValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def post(self, request_target):
        parsed = urlsplit(request_target)
        if parsed.path == "/api/strategies/optimize":
            action, strategy_id = "optimize", None
        else:
            matched = None
            for candidate in ("submit", "approve", "reject", "copy"):
                identifier = self._match(parsed.path, candidate)
                if identifier is not None:
                    matched = (candidate, identifier)
                    break
            if matched is None:
                return False
            action, strategy_id = matched
        try:
            permission = {
                "optimize": "strategy.create", "copy": "strategy.create",
                "submit": "strategy.submit", "approve": "strategy.approve",
                "reject": "strategy.approve",
            }[action]
            actor = self.handler._require(permission)
            if actor is None:
                return True
            body = self.handler._json_body()
            if parsed.query:
                raise StrategyValidationError("strategy mutation does not accept query parameters")
            if action == "optimize":
                if set(body) != {"well_id", "constraints"}:
                    raise StrategyValidationError(
                        "optimize body must contain exactly well_id and constraints"
                    )
                return self._ok(
                    self.service.optimize(body["well_id"], body["constraints"], actor.id),
                    HTTPStatus.CREATED,
                )
            if action == "copy":
                if set(body):
                    raise StrategyValidationError("copy body must be an empty object")
                return self._ok(self.service.copy(strategy_id, actor.id), HTTPStatus.CREATED)
            if action == "submit":
                if set(body) != {"version"}:
                    raise StrategyValidationError("submit body must contain exactly version")
                return self._ok(self.service.submit(strategy_id, body["version"], actor.id))
            if set(body) != {"version", "comment"}:
                raise StrategyValidationError(
                    "approval body must contain exactly version and comment"
                )
            return self._ok(self.service.decide(
                strategy_id, action, body["comment"], actor.id, body["version"],
            ))
        except StrategyNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Strategy not found")
        except StrategyVersionConflict:
            return self._error(
                HTTPStatus.CONFLICT, "version_conflict", "Strategy version is stale",
            )
        except InvalidState as error:
            return self._error(HTTPStatus.CONFLICT, "invalid_state", error)
        except (StrategyValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def patch(self, request_target):
        parsed = urlsplit(request_target)
        strategy_id = self._match(parsed.path, "")
        if strategy_id is None:
            return False
        try:
            if parsed.query:
                raise StrategyValidationError("strategy mutation does not accept query parameters")
            actor = self.handler._require("strategy.create")
            if actor is None:
                return True
            body = self.handler._json_body()
            if "version" not in body:
                raise StrategyValidationError("version is required")
            changes = {key: value for key, value in body.items() if key != "version"}
            return self._ok(self.service.update(
                strategy_id, changes, body["version"], actor.id,
            ))
        except StrategyNotFound:
            return self._error(HTTPStatus.NOT_FOUND, "not_found", "Strategy not found")
        except StrategyVersionConflict:
            return self._error(
                HTTPStatus.CONFLICT, "version_conflict", "Strategy version is stale",
            )
        except InvalidState as error:
            return self._error(HTTPStatus.CONFLICT, "invalid_state", error)
        except (StrategyValidationError, ValueError) as error:
            return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", error)

    def _ok(self, data, status=HTTPStatus.OK):
        self.handler._send_json(status, {"ok": True, "data": data})
        return True

    def _error(self, status, code, message):
        self.handler._send_json(status, self.handler._error(code, str(message)))
        return True

    @staticmethod
    def _match(path, action):
        suffix = f"/{action}" if action else ""
        match = re.fullmatch(
            rf"/api/strategies/([A-Za-z0-9][A-Za-z0-9_-]{{0,127}}){suffix}", path,
        )
        return unquote(match.group(1)) if match else None

    @staticmethod
    def _page(query):
        page = WellApi._integer(query.get("page", "1"), "page", 1, WellApi.MAX_PAGE)
        page_size = WellApi._integer(
            query.get("page_size", "20"), "page_size", 1, 100,
        )
        if (page - 1) * page_size > WellApi.SQLITE_MAX_INTEGER:
            raise StrategyValidationError("page offset is out of range")
        return page, page_size
