"""HTTP route adapter for dashboard and well query services."""

from http import HTTPStatus
import re
from urllib.parse import parse_qs, unquote, urlsplit

from backend.services.wells import WellNotFound, WellValidationError, WellVersionConflict, WellsService


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
        return tuple(unquote(segment) for segment in suffix.split("/"))

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
