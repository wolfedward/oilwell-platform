"""The complete PostgreSQL DDL must enforce immutable evidence and relationships."""

from hashlib import sha256
from dataclasses import replace
import json
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase, LIMITS, TEST_DATABASE
from tests.ingest_vectors import observation, receipt_for, FIXED_TIME


class SchemaTests(IngestDatabaseCase):
    def receipt_values(self):
        obs = observation()
        body = json.loads(obs.payload)
        body["samples"] = []
        rejected = replace(obs, payload=json.dumps(body).encode())
        fact_id = self.insert_fact(payload=rejected.payload, reason_code="contract_invalid",
                                   gateway_id="GW", device_id="D", message_id="M-1")
        receipt = replace(receipt_for(rejected, status="quarantined", reason_code="contract_invalid"),
                          receipt_id=str(uuid4()))
        return dict(receipt_id=receipt.receipt_id, fact_id=fact_id, topic=receipt.topic("oilfield"),
                    payload=receipt.to_bytes(), gateway_id="GW", device_id="D", message_id="M-1",
                    payload_sha256=receipt.payload_sha256, status="quarantined", reason_code="contract_invalid",
                    recorded_at=FIXED_TIME)

    def insert_receipt(self, values):
        statement = sql.SQL("INSERT INTO ingest_reliability.receipts ({}) VALUES ({})").format(
            sql.SQL(",").join(map(sql.Identifier, values)), sql.SQL(",").join(sql.Placeholder() for _ in values))
        with self.connect("ingestor") as conn:
            conn.execute(statement, tuple(values.values()))

    def test_migration_rerun_keeps_version_and_does_not_seed_business_data(self):
        result = self.migrations.migrate(self.settings["migrator"], expected_database=TEST_DATABASE, limits=LIMITS)
        self.assertFalse(result["applied"])
        self.assertEqual(result["version"], "2")
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.schema_migrations", role="migrator"), 2)
        self.assertEqual(self.scalar("SELECT sha256 FROM ingest_reliability.schema_migrations WHERE version=1", role="migrator"),
                         self.migrations._source(1)[1])
        for table in ("facts", "cases", "receipts", "deliveries"):
            self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability." + table, role="migrator"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.ingest_messages", role="migrator"), 0)

    def test_migration_rejects_wrong_database_before_any_ddl(self):
        with self.assertRaises(ReliabilityError) as caught:
            self.migrations.migrate(self.settings["migrator"], expected_database="oilwell", limits=LIMITS)
        self.assertEqual(caught.exception.code, "ingest.migration_target")
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.schema_migrations", role="migrator"), 2)

    def test_reader_cannot_obtain_original_bytes(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connect("reader") as conn:
                conn.execute("SELECT raw_payload FROM ingest_reliability.facts")
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.case_metadata"), 0)

    def test_immutable_fact_cannot_be_changed_even_by_its_table_owner(self):
        fact = self.insert_fact()
        for statement in ("UPDATE ingest_reliability.facts SET reason_code='contract_invalid' WHERE fact_id=%s",
                          "DELETE FROM ingest_reliability.facts WHERE fact_id=%s"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                with self.connect("migrator") as conn:
                    conn.execute(statement, (fact,))
        self.assertEqual(self.scalar("SELECT raw_payload FROM ingest_reliability.facts WHERE fact_id=%s",
                                     (fact,), role="migrator"), b"bad")

    def test_digest_size_and_outcome_constraints_reject_false_evidence(self):
        cases = ({"payload_bytes": 0}, {"payload_sha256": "0" * 64}, {"topic_sha256": "0" * 64},
                 {"status": "stored", "reason_code": "accepted"}, {"reason_code": "accepted"},
                 {"reason_code": "arbitrary"}, {"status": "success"},
                 {"status": "stored", "reason_code": "accepted", "accepted_message_id": "MISSING"},
                 {"gateway_id": "GW"}, {"qos": 3})
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(psycopg.Error):
                self.insert_fact(**fields)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 0)

    def test_quarantine_preserves_empty_and_maximum_binary_payloads(self):
        for payload in (b"", b"\xff\x00" * 1048576):
            fact = self.insert_fact(payload=payload, reason_code="utf8_invalid" if payload else "json_invalid")
            self.assertEqual(self.scalar("SELECT raw_payload FROM ingest_reliability.facts WHERE fact_id=%s",
                                         (fact,), role="migrator"), payload)
        with self.assertRaises(psycopg.Error):
            self.insert_fact(payload=b"x" * 2097153)

    def test_long_distinct_topics_can_be_durably_isolated_without_oversize_index_keys(self):
        topic = "".join(sha256(str(index).encode()).hexdigest() for index in range(1024))[:65535]
        other = topic[:-1] + ("0" if topic[-1] != "0" else "1")
        first, second = self.insert_fact(topic=topic), self.insert_fact(topic=other)
        self.assertNotEqual(first, second)
        self.assertEqual(self.scalar("SELECT topic FROM ingest_reliability.facts WHERE fact_id=%s",
                                     (first,), role="migrator"), topic)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 2)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.insert_fact(topic=topic)

    def test_metadata_views_do_not_expose_binary_columns(self):
        for view in ("case_metadata", "receipt_metadata", "health_metadata"):
            with self.subTest(view=view), self.connect("reader") as conn:
                cursor = conn.execute("SELECT * FROM ingest_reliability." + view)
                self.assertTrue(cursor.description)
                self.assertNotIn("raw_payload", {item.name for item in cursor.description})
                self.assertNotIn(17, {item.type_code for item in cursor.description})

    def test_capacity_update_is_rolled_back_with_its_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with self.connect("ingestor") as conn:
                conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=3 WHERE singleton=1")
                raise RuntimeError("injected rollback")
        self.assertEqual(self.scalar("SELECT used_total_bytes FROM ingest_reliability.capacity", role="migrator"), 0)

    def test_relations_require_a_real_parent(self):
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.connect("migrator") as conn:
                conn.execute("INSERT INTO ingest_reliability.fact_observations(fact_id,first_seen,last_seen) "
                             "VALUES (%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", (uuid4(),))

    def test_stored_fact_requires_matching_accepted_identity_topic_and_hash(self):
        from backend.center.postgres import PostgresTelemetryRepository
        from backend.gateway.contracts import UplinkBatch
        from tests.ingest_vectors import observation
        obs = observation()
        repository = PostgresTelemetryRepository(connect_kwargs=self.settings["migrator"])
        repository.store(UplinkBatch.from_json(obs.payload), topic=obs.topic,
                         payload_sha256=sha256(obs.payload).hexdigest(), raw_payload=obs.payload,
                         received_at=obs.received_at)
        fields = dict(payload=obs.payload, status="stored", reason_code="accepted",
                      accepted_message_id="M-1", gateway_id="GW", device_id="D", message_id="M-1")
        first = self.insert_fact(**fields)
        self.assertEqual(self.scalar("SELECT status FROM ingest_reliability.facts WHERE fact_id=%s",
                                     (first,), role="migrator"), "stored")
        for changes in ({"payload": obs.payload + b" "}, {"gateway_id": "OTHER"},
                        {"topic": "oilfield/v1/GW/OTHER/telemetry"}, {"message_id": "M-2"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(psycopg.Error):
                self.insert_fact(**dict(fields, **changes))

    def test_receipt_original_bytes_and_committed_identity_are_immutable(self):
        values = self.receipt_values()
        self.insert_receipt(values)
        self.assertEqual(self.scalar("SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s",
                                     (values["receipt_id"],), role="publisher"), values["payload"])
        for statement in ("UPDATE ingest_reliability.receipts SET reason_code='json_invalid' WHERE receipt_id=%s",
                          "DELETE FROM ingest_reliability.receipts WHERE receipt_id=%s"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                with self.connect("migrator") as conn:
                    conn.execute(statement, (values["receipt_id"],))

    def test_receipt_must_match_fact_and_its_own_strict_wire_body(self):
        values = self.receipt_values()
        body = json.loads(values["payload"])
        cases = ({"gateway_id": "OTHER"}, {"status": "stored", "reason_code": "accepted"},
                 {"payload_sha256": "0" * 64}, {"fact_id": uuid4()},
                 {"topic": "oilfield/v2/GW/OTHER/ingest-ack"},
                 {"payload": json.dumps(dict(body, status="stored", reason_code="accepted")).encode()},
                 {"payload": values["payload"].replace(b'"status":', b'"status":"stored","status":')},
                 {"payload": json.dumps(dict(body, extra="forbidden")).encode()},
                 {"payload": b"\xff"}, {"payload": values["payload"] + b" " * 4096})
        for changes in cases:
            with self.subTest(changes=tuple(changes)), self.assertRaises(psycopg.Error):
                self.insert_receipt(dict(values, **changes))
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.receipts", role="migrator"), 0)

    def test_audit_and_operation_results_are_append_only(self):
        fact_id = self.insert_fact()
        actor = {"domain": "platform-local", "user_id": "101", "username": "operator",
                 "display_name": "隔离测试操作者", "role_code": "maintenance"}
        with self.connect("operator") as conn:
            conn.execute("INSERT INTO ingest_reliability.audit_events(fact_id,actor,action,reason,before_data,after_data) "
                         "VALUES (%s,%s,'raw.preview','原文访问授权',%s,%s)",
                         (fact_id, Jsonb(actor), Jsonb({}), Jsonb({"granted": True})))
            conn.execute("INSERT INTO ingest_reliability.operation_receipts(actor_domain,actor_id,idempotency_key,request_hash,result) "
                         "VALUES ('platform-local','101',%s,%s,%s)",
                         (uuid4(), "a" * 64, Jsonb({"entity": {"id": str(fact_id), "version": "1", "state": "open"}})))
        for statement in ("UPDATE ingest_reliability.audit_events SET reason='改写历史说明'",
                          "DELETE FROM ingest_reliability.audit_events",
                          "UPDATE ingest_reliability.operation_receipts SET request_hash='" + "0" * 64 + "'",
                          "DELETE FROM ingest_reliability.operation_receipts"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                with self.connect("migrator") as conn:
                    conn.execute(statement)
