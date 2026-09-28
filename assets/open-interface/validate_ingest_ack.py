#!/usr/bin/env python3
"""OWP-INGEST-ACK/1.0 exact-byte correlation check, Python 3.10+, stdlib only.

SPDX-License-Identifier: MIT
This offline check does not authenticate a sender, connect to a broker, or
prove field measurement accuracy. Use authenticated TLS and topic ACLs.
"""
import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from pathlib import Path
import re

MAX_RECEIPT_BYTES = 4096
MAX_TELEMETRY_BYTES = 2_097_152  # Includes safely correlatable rejected input.
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
PREFIX = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z')
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
UTC = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}(?:Z|\+00:00)\Z')
FIELDS = frozenset('schema receipt_id gateway_id device_id message_id payload_sha256 status reason_code recorded_at'.split())
REASONS = frozenset(('topic_invalid', 'payload_too_large', 'utf8_invalid', 'json_invalid',
                     'json_duplicate_key', 'contract_invalid', 'identity_mismatch',
                     'idempotency_conflict', 'qos_not_supported', 'retained_publish'))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key; correlation is ambiguous')
        result[key] = value
    return result


def _constant(_token):
    raise ValueError('non-finite JSON token')


def _number(token):
    # Match lexical PostgreSQL/binary64 safety before float loses information.
    try:
        exact = Decimal(token)
    except InvalidOperation as error:
        raise ValueError('invalid numeric token') from error
    if exact.as_tuple().exponent < -16383 or exact.adjusted() > 131071:
        raise ValueError('numeric token exceeds JSON storage range')
    value = float(exact)
    if not math.isfinite(value) or (value == 0 and exact != 0):
        raise ValueError('numeric token overflow or underflow')
    return value


def _parse(payload, limit, *, telemetry=False):
    if not isinstance(payload, bytes) or len(payload) > limit:
        raise ValueError(f'expected exact UTF-8 bytes, maximum {limit}')
    try:
        options = {'parse_float': _number, 'parse_constant': _constant} if telemetry else {}
        return json.loads(payload.decode('utf-8'), object_pairs_hook=_unique, **options)
    except (ValueError, RecursionError) as error:
        raise ValueError('invalid or ambiguous UTF-8 JSON') from error


def validate(topic: str, payload: bytes, *, telemetry_topic: str, telemetry_payload: bytes) -> dict:
    """Check all receipt fields, topics, identity and the exact original SHA-256.

    Quarantined input need not contain valid samples; its whole JSON must still
    be unambiguous with the correct schema, safe identity and telemetry topic.
    Transport validation (QoS 1, retain false, authenticated sender) is separate.
    """
    body = _parse(payload, MAX_RECEIPT_BYTES)
    if (not isinstance(body, dict) or set(body) != FIELDS
            or any(not isinstance(value, str) for value in body.values())):
        raise ValueError('receipt requires exactly nine string fields')
    if body['schema'] != 'OWP-INGEST-ACK/1.0' or not UUID.fullmatch(body['receipt_id']):
        raise ValueError('invalid receipt schema or UUID')
    if any(not NAME.fullmatch(body[key]) for key in ('gateway_id', 'device_id', 'message_id')):
        raise ValueError('invalid receipt identity')
    if not SHA256.fullmatch(body['payload_sha256']):
        raise ValueError('invalid lower-case SHA-256')
    if not ((body['status'] == 'stored' and body['reason_code'] == 'accepted') or
            (body['status'] == 'quarantined' and body['reason_code'] in REASONS)):
        raise ValueError('invalid status and reason combination')
    if not UTC.fullmatch(body['recorded_at']):
        raise ValueError('receipt time requires UTC with six fractional digits')
    datetime.fromisoformat(body['recorded_at'].replace('Z', '+00:00'))
    parts = topic.split('/') if isinstance(topic, str) else []
    if (len(parts) != 5 or not PREFIX.fullmatch(parts[0])
            or parts[1:] != ['v2', body['gateway_id'], body['device_id'], 'ingest-ack']):
        raise ValueError('invalid receipt topic or identity')
    if telemetry_topic != '/'.join([parts[0], 'v1', body['gateway_id'], body['device_id'], 'telemetry']):
        raise ValueError('telemetry namespace or identity mismatch')
    original = _parse(telemetry_payload, MAX_TELEMETRY_BYTES, telemetry=True)
    if (not isinstance(original, dict) or original.get('schema') != 'oilwell.telemetry.v1'
            or any(original.get(key) != body[key] for key in ('gateway_id', 'device_id', 'message_id'))):
        raise ValueError('telemetry has no matching unambiguous identity')
    if sha256(telemetry_payload).hexdigest() != body['payload_sha256']:
        raise ValueError('receipt does not match the exact telemetry bytes')
    return {key: body[key] for key in ('schema', 'status', 'message_id', 'payload_sha256')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', required=True)
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--telemetry-topic', required=True)
    parser.add_argument('--telemetry-file', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.file.open('rb') as stream:
            payload = stream.read(MAX_RECEIPT_BYTES + 1)
        with args.telemetry_file.open('rb') as stream:
            telemetry = stream.read(MAX_TELEMETRY_BYTES + 1)
        report = validate(args.topic, payload, telemetry_topic=args.telemetry_topic, telemetry_payload=telemetry)
    except (OSError, ValueError) as error:
        print(json.dumps({'valid': False, 'error': str(error)}, ensure_ascii=True))
        return 2
    print(json.dumps(dict(valid=True, **report), ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
