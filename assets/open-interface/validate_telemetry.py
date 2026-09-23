#!/usr/bin/env python3
"""OWP-EDGE/1.0 manufacturer-side preflight. Python 3.10+, standard library only.

SPDX-License-Identifier: MIT
Does not connect, publish, provision credentials, or validate physical accuracy.
"""

import argparse
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re

MAX_BYTES = 1_048_576
MAX_SEQUENCE = 9_223_372_036_854_775_807
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


def _fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError(f"{label}: fields do not match OWP-EDGE/1.0")


def _name(value, label):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError(f"{label}: invalid identifier")


def _sequence(value):
    if type(value) is not int or not 0 <= value <= MAX_SEQUENCE:
        raise ValueError("sequence: expected unsigned value within signed 64-bit range")


def _time(value):
    if not isinstance(value, str) or not UTC_TIME.fullmatch(value):
        raise ValueError("timestamp: expected RFC3339 UTC with seconds")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp: non-UTC offset")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON: duplicate object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("JSON: NaN and Infinity are prohibited")


def _numeric_token(token):
    # Preserve lexical scale until jsonb compatibility and float underflow are checked.
    try:
        exact = Decimal(token)
    except InvalidOperation as error:
        raise ValueError("value: invalid decimal token") from error
    if exact.as_tuple().exponent < -16383 or exact.adjusted() > 131071:
        raise ValueError("value: numeric token exceeds PostgreSQL jsonb range")
    value = float(exact)
    if not math.isfinite(value) or (value == 0 and exact != 0):
        raise ValueError("value: numeric token overflows or underflows binary64")
    return value


def validate(topic, payload):
    """Return a small report or raise ValueError; input is exact UTF-8 bytes."""
    if not isinstance(payload, bytes) or len(payload) > MAX_BYTES:
        raise ValueError("payload: expected at most 1048576 UTF-8 bytes")
    try:
        body = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique,
                          parse_constant=_reject_constant, parse_float=_numeric_token)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("payload: invalid UTF-8 JSON") from error
    _fields(body, "schema message_id gateway_id device_id boot_id created_at sequence samples", "envelope")
    if body["schema"] != "oilwell.telemetry.v1":
        raise ValueError("schema: unsupported version")
    for key in ("message_id", "gateway_id", "device_id", "boot_id"):
        _name(body[key], key)
    parts = topic.split("/") if isinstance(topic, str) else []
    if len(parts) != 5 or parts[1:] != ["v1", body["gateway_id"], body["device_id"], "telemetry"]:
        raise ValueError("topic: namespace or device identity mismatch")
    _name(parts[0], "prefix")
    if len(parts[0]) > 64:
        raise ValueError("prefix: maximum length is 64")
    _time(body["created_at"])
    _fields(body["sequence"], "first last", "sequence")
    for value in body["sequence"].values():
        _sequence(value)
    rows = body["samples"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("samples: expected 1 to 5000 rows")
    previous = -1
    for row in rows:
        _fields(row, "point measured_at sequence value quality unit source", "sample")
        _name(row["point"], "point")
        _name(row["source"], "source")
        _time(row["measured_at"])
        _sequence(row["sequence"])
        if row["sequence"] < previous:
            raise ValueError("sequence: samples must be nondecreasing within a batch")
        previous = row["sequence"]
        value = row["value"]
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("value: expected finite numeric engineering value")
        if row["quality"] not in ("good", "uncertain", "bad"):
            raise ValueError("quality: expected good, uncertain or bad")
        unit = row["unit"]
        if unit is not None and (not isinstance(unit, str) or not 1 <= len(unit) <= 32):
            raise ValueError("unit: expected null or 1 to 32 characters")
        if unit is not None and any(char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in unit):
            raise ValueError("unit: NUL and unpaired Unicode surrogates are prohibited")
    if body["sequence"] != {"first": rows[0]["sequence"], "last": rows[-1]["sequence"]}:
        raise ValueError("sequence: envelope summary does not match samples")
    return {"valid": True, "profile": "OWP-EDGE/1.0", "sample_count": len(rows),
            "message_id": body["message_id"], "sequence_last": str(previous)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.file.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
        report = validate(args.topic, payload)
    except (OSError, ValueError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
