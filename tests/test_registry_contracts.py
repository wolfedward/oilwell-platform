"""Input trust-boundary and content identity for the production registry."""

from decimal import Decimal
import hashlib
import importlib
import json
import unittest
from uuid import UUID


KEY = "d6da6069-b1cd-41b6-9544-a0fb21e5d6bd"
TARGET = UUID("821b9b02-7784-4667-a382-c959560b18c6")
WELL = "831b9b02-7784-4667-a382-c959560b18c7"
MODBUS_POINT = {"point_code": "load", "unit_id": 1, "function_code": 3,
                "address": 0, "data_type": "float32", "unit": "kN"}


class RegistryContractsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.contracts = importlib.import_module("backend.registry.contracts")
            self.maps = importlib.import_module("backend.registry.point_maps")
            self.error = importlib.import_module("backend.registry.errors").RegistryError
        except ModuleNotFoundError as error:
            if not (error.name or "").startswith("backend.registry"):
                raise
            self.fail("Production registry contracts have not been implemented")
        self.actor = self.contracts.Actor("platform-local", "101", "operator", "值班员", "reviewer")

    def assert_rejected(self, operation, *args, **kwargs):
        with self.assertRaises(self.error) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.status, 422)
        self.assertTrue(caught.exception.code.startswith("registry."))

    def command(self, action="device.create", **changes):
        bodies = {
            "well.create": {"area_code": "A", "well_code": "W1", "name": "一号井"},
            "well.change": {"expected_version": "1", "name": "改名井"},
            "device.create": {"gateway_id": "GW", "device_id": "D", "name": "终端",
                              "transport": "mqtt_direct", "usage": "commissioning"},
            "device.change": {"expected_version": "1", "name": "新终端名称"},
            "map.create": {"expected_version": "1", "parent_version": None,
                           "points": [{"point_code": "load", "unit": "kN"}]},
            "map.review": {"expected_version": "1", "map_version": "2",
                           "sha256": "a" * 64, "decision": "approved"},
            "device.bind": {"expected_version": "1", "well_id": WELL},
            "device.unbind": {"expected_version": "1"},
            "map.assign": {"expected_version": "1", "map_version": "2"},
            "device.state": {"expected_version": "1", "status": "active", "check_note": "铭牌现场核对"},
        }
        body = {"reason": "核对接入信息", "idempotency_key": KEY} | bodies[action] | changes
        target = None if action in {"well.create", "device.create"} else TARGET
        return self.contracts.validate_command(action, target, self.actor, body,
                                               method="POST", route="/api/registry/test")

    def test_json_rejects_duplicate_keys_at_every_depth(self):
        for raw in (b'{"reason":"ok","reason":"no"}',
                    b'{"points":[{"unit":"kN","unit":"N"}]}'):
            with self.subTest(raw=raw):
                self.assert_rejected(self.contracts.parse_json, raw)

    def test_json_byte_limit_not_character_limit(self):
        exact = b'{"x":"' + b'a' * (65536 - 8) + b'"}'
        self.assertEqual(len(exact), 65536)
        self.assertEqual(len(self.contracts.parse_json(exact)["x"]), 65528)
        self.assert_rejected(self.contracts.parse_json, exact + b' ')
        chinese = json.dumps({"x": "井" * 22000}, ensure_ascii=False).encode()
        self.assert_rejected(self.contracts.parse_json, chinese)

    def test_json_requires_utf8_object_and_safe_depth(self):
        for raw in (b'\xff', b'null', b'[]', b'3', b'"text"', b'{',
                    b'\xef\xbb\xbf{}', b'{"x":' + b'[' * 2000 + b']' * 2000 + b'}'):
            with self.subTest(size=len(raw)):
                self.assert_rejected(self.contracts.parse_json, raw)

    def test_json_preserves_decimal_and_large_integer(self):
        item = self.contracts.parse_json(b'{"a":0.1,"b":9223372036854775807}')
        self.assertEqual(item["a"], Decimal("0.1"))
        self.assertIs(type(item["b"]), int)
        self.assertEqual(item["b"], 9223372036854775807)

    def test_json_depth_is_bounded_even_when_decoder_accepts_it(self):
        allowed = b'{"x":' + b'[' * 63 + b']' * 63 + b'}'
        self.assertIsInstance(self.contracts.parse_json(allowed), dict)
        deeper = b'{"x":' + b'[' * 64 + b']' * 64 + b'}'
        self.assert_rejected(self.contracts.parse_json, deeper)
        self.assertEqual(self.contracts.parse_json(b'{"x":"[[[[{{{{"}')["x"], "[[[[{{{{")

    def test_json_nonfinite_and_oversized_integer_are_rejected(self):
        for token in (b'NaN', b'Infinity', b'-Infinity', b'9' * 5000):
            self.assert_rejected(self.contracts.parse_json, b'{"value":' + token + b'}')

    def test_versions_are_ascii_decimal_strings_without_precision_loss(self):
        for value, expected in (("1", 1), ("9007199254740993", 9007199254740993),
                                ("9223372036854775807", 9223372036854775807)):
            self.assertEqual(self.contracts.version(value), expected)
        for value in (True, False, 1, 1.0, None, "", "0", "01", "+1", "-1", "1.0", "1e3",
                      "１", "١", "1\n", " 1", "9223372036854775808", "9" * 5000):
            with self.subTest(value=str(value)[:25]):
                self.assert_rejected(self.contracts.version, value)

    def test_command_identity_and_input_copy(self):
        command = self.command()
        self.assertEqual(command.key, UUID(KEY))
        self.assertIsNone(command.target)
        self.assertEqual(command.actor, self.actor)
        self.assertEqual(command.body["manufacturer"], "")
        self.assertEqual(command.body["serial_number"], "")
        points = [{"point_code": "load", "unit": "kN"}]
        map_command = self.command("map.create", points=points)
        points[0]["unit"] = "N"
        self.assertEqual(map_command.body["points"][0]["unit"], "kN")

    def test_every_write_action_accepts_valid_required_fields(self):
        for action in ("well.create", "well.change", "device.create", "device.change", "map.create",
                       "map.review", "device.bind", "device.unbind", "map.assign", "device.state"):
            with self.subTest(action=action):
                command = self.command(action)
                self.assertEqual(command.action, action)
                self.assertEqual(len(command.request_hash), 64)
        self.assertEqual(self.command("device.change", expected_version="9007199254740993")
                         .body["expected_version"], "9007199254740993")

    def test_unknown_and_privileged_fields_are_rejected_for_every_action(self):
        for action in ("well.create", "well.change", "device.create", "device.change", "map.create",
                       "map.review", "device.bind", "device.unbind", "map.assign", "device.state"):
            for field in ("actor", "role", "effective_at", "password", "sql", "unexpected"):
                with self.subTest(action=action, field=field):
                    self.assert_rejected(self.command, action, **{field: "untrusted"})

    def test_required_command_fields_cannot_be_missing(self):
        original = self.command().body
        for field in ("gateway_id", "device_id", "name", "transport", "usage", "reason", "idempotency_key"):
            body = original.copy()
            del body[field]
            with self.subTest(field=field):
                self.assert_rejected(self.contracts.validate_command, "device.create", None,
                                     self.actor, body, method="POST", route="/api/registry/devices")

    def test_action_target_key_and_mutation_shape_are_strict(self):
        for key in (None, 1, "not-a-uuid", "{d6da6069-b1cd-41b6-9544-a0fb21e5d6bd}", KEY.replace("-", "")):
            self.assert_rejected(self.command, idempotency_key=key)
        body = self.command().body
        self.assert_rejected(self.contracts.validate_command, "device.create", TARGET,
                             self.actor, body, method="POST", route="/api/registry/devices")
        self.assert_rejected(self.contracts.validate_command, "delete", None,
                             self.actor, body, method="POST", route="/api/registry/devices")
        for action in ("well.change", "device.change"):
            self.assert_rejected(self.contracts.validate_command, action, TARGET, self.actor,
                                 {"expected_version": "1", "reason": "核对信息", "idempotency_key": KEY},
                                 method="POST", route="/api/registry/test")
        self.assert_rejected(self.command, "device.bind", well_id="not-a-uuid")
        self.assert_rejected(self.command, "device.state", status="pending")
        self.assert_rejected(self.command, "map.review", decision="draft")
        self.assert_rejected(self.command, "map.review", sha256="z" * 64)

    def test_ascii_identifiers_keep_case_and_reject_separators(self):
        self.assertEqual(self.command(gateway_id="GW:Ab_1.2-3").body["gateway_id"], "GW:Ab_1.2-3")
        self.assertEqual(len(self.command(device_id="A" * 128).body["device_id"]), 128)
        for field in ("gateway_id", "device_id"):
            for value in ("", "A" * 129, "中文", "A/B", "A B", "../A", "_A", "A\n", None, True):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(self.command, **{field: value})
        self.assert_rejected(self.command, "well.create", area_code="中文")
        self.assert_rejected(self.command, "well.create", well_code="W/1")

    def test_unicode_limits_count_codepoints_and_reject_controls(self):
        for field, limit in (("name", 120), ("manufacturer", 120), ("model", 120), ("serial_number", 128)):
            self.assertEqual(self.command(**{field: "井💧" * (limit // 2)}).body[field], "井💧" * (limit // 2))
            self.assert_rejected(self.command, **{field: "井" * (limit + 1)})
        for bad in ("井\x00名", "井\x1f名", "井\x7f名", "井\x85名", "井\ud800名", "井\udfff名"):
            self.assert_rejected(self.command, name=bad)
        for bad in ("", "  ", None, 123):
            self.assert_rejected(self.command, name=bad)
        label = '<img src=x onerror="alert(1)">井'
        self.assertEqual(self.command(name=label).body["name"], label)

    def test_reason_and_manual_evidence_limits(self):
        for reason in ("井检", "核" * 500):
            self.assertEqual(self.command(reason=reason).body["reason"], reason)
        for reason in ("", "a", " " * 2, "核" * 501, "核\n查"):
            self.assert_rejected(self.command, reason=reason)
        self.assertEqual(len(self.command("device.state", check_note="核" * 1000).body["check_note"]), 1000)
        for note in ("", "a", " " * 20, "核" * 1001):
            self.assert_rejected(self.command, "device.state", check_note=note)

    def test_canonical_modbus_expands_defaults(self):
        expected_point = MODBUS_POINT | {"byte_order": "big", "word_order": "big", "scale": 1.0,
            "offset": 0.0, "writable": False, "poll_group": "normal",
            "engineering_min": None, "engineering_max": None}
        document, raw, digest = self.maps.canonical_point_map("modbus_tcp", [MODBUS_POINT.copy()])
        self.assertEqual(document, {"rule": "registry.point-map.canonical.v1",
                                   "transport": "modbus_tcp", "points": [expected_point]})
        self.assertEqual((document, raw, digest), self.maps.canonical_point_map("modbus_tcp", [expected_point]))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        self.assertEqual(json.loads(raw), document)

    def test_canonical_mqtt_has_no_invented_registers(self):
        document, raw, digest = self.maps.canonical_point_map("mqtt_direct", [{"point_code": "load", "unit": "载荷💧"}])
        self.assertEqual(document["points"], [{"point_code": "load", "unit": "载荷💧",
                                              "engineering_min": None, "engineering_max": None}])
        self.assertIn("载荷💧".encode(), raw)
        self.assertNotIn(b'\\u', raw)
        self.assertEqual(len(digest), 64)
        for field in ("address", "unit_id", "scale", "function_code", "writable", "poll_group"):
            self.assert_rejected(self.maps.canonical_point_map, "mqtt_direct", [{"point_code": "load", field: 1}])

    def test_point_order_default_forms_and_numeric_forms_share_identity(self):
        a = MODBUS_POINT | {"point_code": "Z", "scale": 2}
        b = MODBUS_POINT | {"point_code": "a", "scale": Decimal("2.00")}
        first = self.maps.canonical_point_map("modbus_tcp", [a, b])
        second = self.maps.canonical_point_map("modbus_tcp", [b.copy(), a.copy()])
        self.assertEqual(first, second)
        self.assertEqual([p["point_code"] for p in first[0]["points"]], ["Z", "a"])
        changed = self.maps.canonical_point_map("modbus_tcp", [a | {"scale": 3}, b])
        self.assertNotEqual(first[2], changed[2])
        self.assertNotEqual(first[2], self.maps.canonical_point_map("modbus_rtu", [a, b])[2])

    def test_point_count_uniqueness_and_shape_are_bounded(self):
        points = [{"point_code": f"P{i}"} for i in range(256)]
        self.assertEqual(len(self.maps.canonical_point_map("mqtt_direct", points)[0]["points"]), 256)
        for values in ([], points + [{"point_code": "P256"}], [{"point_code": "P"}] * 2,
                       [1], [None], {"point_code": "P"}, None, [{"point_code": "P", "actor": "evil"}]):
            self.assert_rejected(self.maps.canonical_point_map, "mqtt_direct", values)
        for transport in (None, True, "modbus", [], "mqtt_direct\n"):
            self.assert_rejected(self.maps.canonical_point_map, transport, [{"point_code": "P"}])

    def test_modbus_integer_fields_never_accept_boolean_or_float(self):
        for field, value in (("unit_id", True), ("unit_id", 1.0), ("unit_id", 0), ("unit_id", 248),
                             ("function_code", True), ("function_code", 3.0), ("function_code", 6),
                             ("function_code", 16), ("address", False), ("address", 0.0), ("address", -1),
                             ("writable", True), ("writable", 0), ("writable", None), ("writable", "false")):
            with self.subTest(field=field, value=value):
                self.assert_rejected(self.maps.canonical_point_map, "modbus_tcp", [MODBUS_POINT | {field: value}])

    def test_modbus_width_and_byte_word_order(self):
        for dtype, width in (("uint16", 1), ("int16", 1), ("uint32", 2), ("int32", 2), ("float32", 2), ("float64", 4)):
            point = MODBUS_POINT | {"data_type": dtype, "address": 65536 - width, "function_code": 4}
            self.maps.canonical_point_map("modbus_rtu", [point])
            self.assert_rejected(self.maps.canonical_point_map, "modbus_rtu", [point | {"address": 65537 - width}])
        for field, value in (("data_type", "bool"), ("byte_order", "reverse"), ("word_order", []), ("poll_group", "a/b")):
            self.assert_rejected(self.maps.canonical_point_map, "modbus_tcp", [MODBUS_POINT | {field: value}])

    def test_point_numbers_finite_binary64_without_silent_underflow(self):
        for field in ("scale", "offset", "engineering_min", "engineering_max"):
            for value in (True, "1.0", float("nan"), float("inf"), -float("inf"),
                          Decimal("1e9999"), Decimal("1e-9999"), 10 ** 400):
                with self.subTest(field=field, value=str(value)[:30]):
                    self.assert_rejected(self.maps.canonical_point_map, "modbus_tcp", [MODBUS_POINT | {field: value}])
        point = MODBUS_POINT | {"scale": Decimal("5e-324"), "offset": -1.7976931348623157e308}
        result = self.maps.canonical_point_map("modbus_tcp", [point])[0]["points"][0]
        self.assertEqual(result["scale"], 5e-324)
        self.assertEqual(result["offset"], -1.7976931348623157e308)
        self.assert_rejected(self.maps.canonical_point_map, "mqtt_direct",
                             [{"point_code": "P", "engineering_min": 10, "engineering_max": 9}])

    def test_unit_range_and_unicode_are_exact(self):
        for unit in (None, "kN", "kN ", "井" * 32):
            result = self.maps.canonical_point_map("mqtt_direct", [{"point_code": "P", "unit": unit}])
            self.assertEqual(result[0]["points"][0]["unit"], unit)
        for unit in ("", "井" * 33, "k\x00N", "k\ud800N", "k\x85N", True):
            self.assert_rejected(self.maps.canonical_point_map, "mqtt_direct", [{"point_code": "P", "unit": unit}])

    def test_map_command_normalizes_points_before_request_digest(self):
        first = self.command("map.create", points=[MODBUS_POINT.copy()])
        explicit = MODBUS_POINT | {"scale": 1.0, "offset": 0, "writable": False}
        second = self.command("map.create", points=[explicit])
        self.assertEqual(first.request_hash, second.request_hash)
        self.assert_rejected(self.command, "map.create", points=[MODBUS_POINT, {"point_code": "power"}])
        self.assert_rejected(self.command, "map.create", parent_version=2)
        self.assert_rejected(self.command, "map.create", transport="mqtt_direct")

    def test_digest_binds_method_route_and_body(self):
        body = {"reason": "核对", "expected_version": "1"}
        first = self.contracts.command_digest("POST", "/api/registry/A", body)
        self.assertEqual(first, self.contracts.command_digest("POST", "/api/registry/A", dict(reversed(list(body.items())))))
        for method, route, value in (("PUT", "/api/registry/A", body), ("POST", "/api/registry/B", body),
                                      ("POST", "/api/registry/A", body | {"expected_version": "2"})):
            self.assertNotEqual(first, self.contracts.command_digest(method, route, value))


if __name__ == "__main__":
    unittest.main()
