"""HTTP route adapter for dashboard and well query services."""

from http import HTTPStatus
import re
from urllib.parse import parse_qs, unquote, urlsplit

from backend.services.wells import WellNotFound, WellValidationError, WellVersionConflict, WellsService
from backend.services.devices import ChannelVersionConflict, DeviceNotFound, DevicesService, DeviceValidationError
from backend.services.acquisition import (
    AcquisitionNotFound, AcquisitionService, AcquisitionValidationError, AcquisitionVersionConflict,
)


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
                return self._dashboard()
            if path == "/api/wells":
                return self._list(parsed.query)
            segments = self._well_segments(path)
            if segments is not None:
                if len(segments) == 1:
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
        values = self._query(query, {"unit_id", "group_id", "status", "keyword", "page", "page_size"})
        page = self._integer(values.get("page", "1"), "page", 1, self.MAX_PAGE)
        page_size = self._integer(values.get("page_size", "20"), "page_size", 1, 100)
        if (page - 1) * page_size > self.SQLITE_MAX_INTEGER:
            raise WellValidationError("page offset is out of range")
        self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.list_wells({**values, "page": page, "page_size": page_size})})
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
                if "unit" in query and "unit_id" not in query: query["unit_id"] = query.pop("unit")
                if "well" in query and "well_id" not in query: query["well_id"] = query.pop("well")
                page = WellApi._integer(query.get("page", "1"), "page", 1, self.MAX_PAGE); size = WellApi._integer(query.get("page_size", "20"), "page_size", 1, 100)
                if (page - 1) * size > self.SQLITE_MAX_INTEGER: raise DeviceValidationError("page offset is out of range")
                self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": self.service.list_devices({**query, "page": page, "page_size": size})}); return True
            segments = self._segments(path)
            if segments is None: return False
            if self.handler._require("devices.read") is None: return True
            if len(segments) == 1: payload = self.service.detail(segments[0])
            elif len(segments) == 2 and segments[1] == "registers": payload = self.service.registers(segments[0])
            else: return False
            self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": payload}); return True
        except DeviceNotFound: self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Device not found")); return True
        except DeviceValidationError as error: self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error))); return True
    def post(self, request_target):
        segments = self._segments(urlsplit(request_target).path)
        if segments is None or len(segments) != 2 or segments[1] != "scenarios": return False
        actor = self.handler._require("devices.control")
        if actor is None: return True
        try:
            body = self.handler._json_body()
            if not isinstance(body, dict) or set(body) != {"name", "duration_minutes"}:
                raise DeviceValidationError("scenario body must contain exactly name and duration_minutes")
            result = self.service.apply_scenario(actor.id, segments[0], body["name"], body["duration_minutes"])
            self.handler._send_json(HTTPStatus.OK, {"ok": True, "data": result})
        except DeviceNotFound: self.handler._send_json(HTTPStatus.NOT_FOUND, self.handler._error("not_found", "Device not found"))
        except (DeviceValidationError, ValueError) as error: self.handler._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self.handler._error("validation_error", str(error)))
        return True
    def patch(self, request_target):
        segments = self._segments(urlsplit(request_target).path)
        if segments is None or len(segments) != 3 or segments[1] != "channels": return False
        actor = self.handler._require("devices.configure")
        if actor is None: return True
        try:
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
            event_id = self._repair_event_id(urlsplit(request_target).path)
            if event_id is None: return False
            actor = self.handler._require("data_quality.repair")
            if actor is None: return True
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
