"""Synthetic offline imports through real receiver transactions."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from backend.waveforms.errors import ContractError, WaveformError
from backend.waveforms.repository import WaveformRepository
from tests.waveform_support import WaveformDatabaseCase, LIMITS
from tests.test_waveform_repository import capture


class ImportCommandTests(unittest.TestCase):
    def test_hash_vectors_and_identity_fields(self):
        from backend.waveforms.commands import Actor, Command, import_request_hash
        command = Command(Actor("local", "1", "synthetic", "operator"), UUID(int=1), 1, "synthetic test", "0"*64)
        target = UUID(int=9)
        self.assertEqual(import_request_hash("append", target, command, line=0, payload=b"{}"),
                         "edafb1ff245d164c99d5f9d0436b5537d3a98f51810097ecf3fe42a844eca2d5")
        self.assertEqual(import_request_hash("seal", target, command),
                         "d673abc9cf994b077bfdef4f6538d92f50e80fd6a396b1b16df3d719dc1fb55b")
        self.assertEqual(import_request_hash("cancel", target, command),
                         "97d5ece356c4e0c1ca5fe55474c4331d23c5262f9395f3ee5f6b571d4e8d0a30")
        first = import_request_hash("append", target, command, line=0, payload=b"{}")
        for altered, new_target, line, payload in (
                (replace(command, expected_version=2), target, 0, b"{}"),
                (replace(command, reason="changed"), target, 0, b"{}"),
                (replace(command, actor=replace(command.actor, user_id="2")), target, 0, b"{}"),
                (command, UUID(int=10), 0, b"{}"), (command, target, 1, b"{}"), (command, target, 0, b"{} ")):
            self.assertNotEqual(import_request_hash("append", new_target, altered, line=line, payload=payload), first)

    def test_actor_command_and_metadata_strict_validation(self):
        from backend.waveforms.commands import Actor, Command, import_metadata
        valid = dict(domain="local", user_id="1", username="synthetic", role_code="operator")
        for field in valid:
            for value in (True, 1, "", " ", "\x85", "x"*129):
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    Actor(**(valid | {field:value}))
        actor = Actor(**valid)
        for version in (True, -1, "0", 2**63):
            with self.assertRaises(ContractError): Command(actor, uuid4(), version, "reason", "a"*64)
        metadata = dict(file_sha256="a"*64, bytes=2, line_count=1, final_lf=False,
                        gateway_id="GW", device_id="D", capture_id=str(UUID(int=9)))
        for alteration in ({"bytes":True}, {"bytes":33554946}, {"line_count":514}, {"final_lf":0},
                           {"capture_id":"bad"}, {"extra":1}, {"gateway_id":"*"}):
            with self.assertRaises(ContractError): import_metadata(metadata | alteration)


class WaveformImportTests(WaveformDatabaseCase):
    def setUp(self):
        super().setUp()
        from backend.waveforms.commands import Actor
        from backend.waveforms.imports import ImportService
        self.actor = Actor("local", "1", "synthetic", "operator")
        self.service = ImportService(self.connect, limits=LIMITS, prefix="oil")
        self.payloads = capture()

    def metadata(self, *, final_lf=False, payloads=None):
        payloads = payloads or self.payloads
        raw = b"\n".join(payloads) + (b"\n" if final_lf else b"")
        body = json.loads(payloads[0])
        return dict(file_sha256=sha256(raw).hexdigest(), bytes=len(raw),
                    line_count=len(payloads), final_lf=final_lf,
                    gateway_id=body["gateway_id"], device_id=body["device_id"], capture_id=body["capture_id"])

    def command(self, operation, target=None, *, version=0, actor=None, key=None, **body):
        from backend.waveforms.commands import Command, import_request_hash
        cmd = Command(actor or self.actor, key or uuid4(), version, "synthetic test", "0"*64)
        return replace(cmd, request_hash=import_request_hash(operation, target, cmd, **body))

    def create(self, **kwargs):
        metadata = self.metadata(**kwargs)
        return self.service.create(metadata, self.command("create", metadata=metadata))

    def append(self, session, line, payload=None):
        payload = self.payloads[line] if payload is None else payload
        return self.service.append(session.import_id, line, payload,
            self.command("append", session.import_id, version=session.version, line=line, payload=payload))

    def seal(self, session):
        return self.service.seal(session.import_id, self.command("seal", session.import_id, version=session.version))

    def test_reimport_reuses_facts_not_provenance(self):
        first = self.create()
        first = self.append(first, 0)
        self.assertIsNone(first.receipt_id)
        self.assertEqual(first.state, "pending")
        first = self.append(first, 1)
        self.assertEqual(self.seal(first).state, "sealed")
        second = self.create()
        second = self.append(second, 1)
        second = self.append(second, 0)
        self.assertEqual(self.seal(second).state, "sealed")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 2)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.observations", role="receiver"), 4)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.receipts", role="receiver"), 0)

    def test_seal_exact_final_lf(self):
        for final_lf in (False, True):
            session = self.create(final_lf=final_lf)
            session = self.append(session, 0)
            session = self.append(session, 1)
            self.assertEqual(self.seal(session).state, "sealed")
        metadata = self.metadata() | {"final_lf": True, "bytes": self.metadata()["bytes"]+1}
        session = self.service.create(metadata, self.command("create", metadata=metadata))
        session = self.append(self.append(session, 0), 1)
        with self.assertRaises(WaveformError): self.seal(session)
        self.assertEqual(self.scalar("SELECT state FROM waveforms.imports WHERE import_id=%s", (session.import_id,), role="receiver"), "recorded")

    def test_line_commit_and_session_atomic(self):
        session = self.create()
        with patch.object(self.service, "_record", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaises(RuntimeError): self.append(session, 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines", role="receiver"), 0)
        self.assertEqual(self.scalar("SELECT version FROM waveforms.imports", role="receiver"), 1)

    def test_lost_response_retry_same_key(self):
        session = self.create()
        cmd = self.command("append", session.import_id, version=1, line=0, payload=self.payloads[0])
        commit = WaveformRepository.commit_pending
        def lost(repo, conn, pending):
            commit(repo, conn, pending)
            raise RuntimeError("synthetic lost response after durable commit")
        with patch.object(WaveformRepository, "commit_pending", lost):
            with self.assertRaises(RuntimeError): self.service.append(session.import_id, 0, self.payloads[0], cmd)
        result = self.service.append(session.import_id, 0, self.payloads[0], cmd)
        self.assertEqual(result.version, 2)
        self.assertEqual(result.missing_lines, (1,))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines", role="receiver"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.operation_receipts", role="receiver"), 2)

    def test_preexisting_duplicate_audit_failure_rolls_back_whole_operation(self):
        from tests.test_waveform_repository import observation
        session = self.create()
        repo = WaveformRepository(self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        repo.ingest(observation(self.payloads[0], transport="file", source_ref=session.import_id))
        with self.connect() as conn:
            before = conn.execute("SELECT * FROM waveforms.observations").fetchall()
            usage = conn.execute("SELECT * FROM waveforms.capacity ORDER BY bucket").fetchall()
        with patch.object(self.service, "_record", side_effect=RuntimeError("synthetic later failure")):
            with self.assertRaises(RuntimeError): self.append(session, 0)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT * FROM waveforms.observations").fetchall(), before)
            self.assertEqual(conn.execute("SELECT * FROM waveforms.capacity ORDER BY bucket").fetchall(), usage)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines", role="receiver"), 0)
        self.assertEqual(self.scalar("SELECT version FROM waveforms.imports", role="receiver"), 1)

    def test_raw_line_failure_also_rolls_back_prior_store(self):
        session = self.create()
        with self.connect("migration_owner") as conn:
            # Five metadata rows: outcome, capture, stream, job, observation.
            # The sixth (import line) must fail after the repository returns.
            conn.execute("UPDATE waveforms.capacity SET used_metadata_rows=metadata_rows-5 WHERE bucket='GW'")
        stored = []
        original = WaveformRepository.store_in_transaction
        def store(repo, cursor, observation):
            pending = original(repo, cursor, observation)
            stored.append(pending.fact_id)
            return pending
        with patch.object(WaveformRepository, "store_in_transaction", store):
            with self.assertRaises(WaveformError): self.append(session, 0)
        self.assertEqual(len(stored), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines", role="receiver"), 0)

    def test_missing_lines_are_zero_based_and_out_of_order_resumes(self):
        session = self.create()
        self.assertEqual(session.missing_lines, (0, 1))
        session = self.append(session, 1)
        self.assertEqual(session.missing_lines, (0,))
        with self.assertRaises(WaveformError): self.seal(session)
        session = self.append(session, 0)
        self.assertEqual(session.missing_lines, ())
        self.assertEqual(self.seal(session).state, "sealed")

    def test_last_public_line_maps_to_sql513_once(self):
        from tests.test_waveform_repository import capture_512
        raw, pieces = capture_512()
        session = self.create(payloads=(raw, *pieces))
        session = self.append(session, 512, pieces[-1])
        self.assertNotIn(512, session.missing_lines)
        self.assertEqual(session.missing_lines, tuple(range(512)))
        self.assertEqual(self.scalar("SELECT line_number FROM waveforms.import_lines", role="receiver"), 513)

    def test_commit_failure_never_exposes_saved_and_rolls_back(self):
        import psycopg
        session = self.create()
        with patch.object(WaveformRepository, "commit_pending", side_effect=psycopg.OperationalError("synthetic precommit failure")):
            with self.assertRaises(WaveformError): self.append(session, 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.operation_receipts", role="receiver"), 1)

    def test_durable_guard_rejects_unsafe_wal_before_import_writes(self):
        with patch("backend.waveforms.capacity._durability_settings", return_value=("off", "on", "on")):
            with self.assertRaises(WaveformError): self.create()
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.imports", role="receiver"), 0)

    def test_cross_operator_append_and_seal_are_audited(self):
        session = self.create()
        other = replace(self.actor, user_id="2", role_code="ordinary-capability")
        for line, payload in enumerate(self.payloads):
            session = self.service.append(session.import_id, line, payload,
                self.command("append", session.import_id, actor=other, version=session.version, line=line, payload=payload))
        self.service.seal(session.import_id, self.command("seal", session.import_id, actor=other, version=session.version))
        with self.connect("migration_owner") as conn:
            actors = conn.execute("SELECT actor->>'user_id' FROM waveforms.import_events ORDER BY version").fetchall()
            audits = conn.execute("SELECT actor->>'user_id' FROM waveforms.audit WHERE action IN ('imports.append','imports.seal')").fetchall()
        self.assertEqual(actors, [("1",),("2",),("2",),("2",)])
        self.assertEqual(audits, [("2",),("2",),("2",)])

    def test_import_metadata_and_event_rows_remain_immutable(self):
        import psycopg
        session = self.create()
        for statement in ("UPDATE waveforms.imports SET file_bytes=file_bytes+1",
                          "UPDATE waveforms.imports SET version=version+2",
                          "DELETE FROM waveforms.imports", "DELETE FROM waveforms.import_events",
                          "UPDATE waveforms.import_events SET reason='rewritten'"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                with self.connect("migration_owner") as conn: conn.execute(statement)
        self.assertEqual(self.scalar("SELECT version FROM waveforms.imports", role="receiver"), 1)

    def test_malformed_and_mixed_lines_leave_no_raw(self):
        session = self.create()
        bad_payloads = (b"\xef\xbb\xbf"+self.payloads[0], self.payloads[0]+b"\r\n", b"", b"{", capture()[0], self.payloads[1])
        for payload in bad_payloads:
            with self.subTest(payload=payload[:5]):
                with self.assertRaises((WaveformError, ContractError)):
                    self.append(session, 0, payload)
        for line in (True, -1, 513, "0", 2):
            with self.assertRaises((WaveformError, ContractError)):
                self.append(session, line, self.payloads[0])
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)

    def test_file_forged_digest_remains_pending(self):
        metadata = self.metadata() | {"file_sha256": "f"*64}
        session = self.service.create(metadata, self.command("create", metadata=metadata))
        session = self.append(self.append(session, 0), 1)
        with self.assertRaises(WaveformError): self.seal(session)
        self.assertEqual(self.scalar("SELECT state FROM waveforms.imports", role="receiver"), "recorded")

    def test_same_key_changed_body_and_untrusted_hash_conflict(self):
        metadata = self.metadata()
        cmd = self.command("create", metadata=metadata)
        first = self.service.create(metadata, cmd)
        self.assertEqual(self.service.create(metadata, cmd), first)
        altered = metadata | {"file_sha256": "f"*64}
        with self.assertRaises(WaveformError): self.service.create(altered, cmd)
        with self.assertRaises(WaveformError):
            self.service.create(altered, self.command("create", key=cmd.key, metadata=altered))
        with self.assertRaises(WaveformError):
            self.service.create(metadata, replace(cmd, reason="changed"))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.imports", role="receiver"), 1)

    def test_actor_labels_do_not_reset_identity_or_replay(self):
        metadata = self.metadata()
        cmd = self.command("create", metadata=metadata)
        first = self.service.create(metadata, cmd)
        updated_actor = replace(self.actor, username="renamed", role_code="another capability")
        self.assertEqual(self.service.create(metadata, replace(cmd, actor=updated_actor)), first)
        for _ in range(3): self.create()
        with self.assertRaises(WaveformError):
            self.service.create(metadata, self.command("create", actor=updated_actor, metadata=metadata))
        other = replace(self.actor, user_id="2")
        self.assertNotEqual(self.service.create(metadata, self.command("create", actor=other, key=cmd.key, metadata=metadata)).import_id, first.import_id)

    def test_actual_operator_and_creator_quota_with_cancel(self):
        sessions = [self.create() for _ in range(4)]
        session = sessions[0]
        other = replace(self.actor, user_id="someone-else", role_code="capability_holder")
        cmd = self.command("cancel", session.import_id, version=1, actor=other)
        result = self.service.cancel(session.import_id, cmd)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(self.service.cancel(session.import_id, cmd), result)
        self.create()
        with self.connect("migration_owner") as conn:
            creator = conn.execute("SELECT actor FROM waveforms.imports WHERE import_id=%s", (session.import_id,)).fetchone()[0]
            operator = conn.execute("SELECT actor FROM waveforms.import_events WHERE import_id=%s AND action='cancel'", (session.import_id,)).fetchone()[0]
        self.assertEqual(creator["user_id"], "1")
        self.assertEqual(operator["user_id"], "someone-else")

    def test_cancel_keeps_lines_and_terminal_retries_do_not_reopen(self):
        session = self.append(self.create(), 0)
        cancelled = self.service.cancel(session.import_id, self.command("cancel", session.import_id, version=2))
        with self.assertRaises(WaveformError): self.append(cancelled, 1)
        with self.assertRaises(WaveformError): self.seal(cancelled)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines", role="receiver"), 1)

    def test_new_key_duplicate_line_is_byte_and_version_neutral(self):
        session = self.append(self.create(), 0)
        again = self.append(session, 0)
        self.assertEqual(again, session)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_events", role="receiver"), 2)
        self.assertEqual(self.scalar("SELECT sum(duplicate_count) FROM waveforms.observations", role="receiver"), 0)
        with self.assertRaises(WaveformError): self.append(session, 0, self.payloads[0]+b" ")

    def test_capture_source_limit_survives_cancel(self):
        for index in range(8):
            session = self.create()
            self.service.cancel(session.import_id, self.command("cancel", session.import_id, version=1))
        metadata = self.metadata()
        with self.assertRaises(WaveformError):
            self.service.create(metadata, self.command("create", actor=replace(self.actor, user_id="new-actor"), metadata=metadata))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.imports", role="receiver"), 8)

    def test_ninth_source_race_across_actors(self):
        for _ in range(7):
            session = self.create()
            self.service.cancel(session.import_id, self.command("cancel", session.import_id, version=1))
        metadata = self.metadata()
        def create(user_id):
            try:
                return self.service.create(metadata, self.command("create", actor=replace(self.actor,user_id=user_id), metadata=metadata)).state
            except WaveformError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ("second", "third")))
        self.assertEqual(results.count("pending"), 1)
        self.assertEqual(results.count("waveform.import_source_limit"), 1)

    def test_duplicate_chunks_cannot_seal_and_seal_replay_is_neutral(self):
        session = self.create(payloads=(*self.payloads, self.payloads[1]))
        session = self.append(self.append(session, 0), 1)
        session = self.append(session, 2, self.payloads[1])
        with self.assertRaises(ContractError): self.seal(session)
        valid = self.append(self.append(self.create(), 0), 1)
        cmd = self.command("seal", valid.import_id, version=valid.version)
        result = self.service.seal(valid.import_id, cmd)
        usage = self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'", role="receiver")
        self.assertEqual(self.service.seal(valid.import_id, cmd), result)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'", role="receiver"), usage)

    def test_concurrent_versions_allow_only_one_append(self):
        session = self.create()
        def upload(line):
            try: return self.append(session, line).version
            except WaveformError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(upload, (0, 1)))
        self.assertEqual(results.count(2), 1)
        self.assertEqual(results.count("waveform.import_version"), 1)

    def test_actor_fifth_session_race_across_gateways(self):
        from backend.waveforms.config import CapacityLimits
        from backend.waveforms.imports import ImportService
        limits = CapacityLimits(LIMITS.global_quota, {"GW": LIMITS.gateways["GW"], "OTHER": LIMITS.gateways["GW"]})
        with self.capacity_fixture(limits):
            self.service = ImportService(self.connect, limits=limits, prefix="oil")
            for _ in range(3): self.create()
            def create(gateway):
                metadata = self.metadata(payloads=capture(gateway))
                try: return self.service.create(metadata, self.command("create", metadata=metadata)).state
                except WaveformError as error: return error.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(create, ("GW", "OTHER")))
            self.assertEqual(results.count("pending"), 1)
            self.assertEqual(results.count("waveform.import_session_limit"), 1)

    def test_online_bad_transport_does_not_poison_offline_outcome(self):
        from tests.test_waveform_repository import observation
        repo = WaveformRepository(self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        online = repo.ingest(observation(self.payloads[0], retain=True))
        offline = self.append(self.create(), 0)
        self.assertEqual(online.fact_id, offline.fact_id)
        self.assertNotEqual(online.outcome_id, offline.outcome_id)
        self.assertIsNone(offline.receipt_id)
        self.assertEqual(self.scalar("SELECT status FROM waveforms.message_outcomes WHERE outcome_id=%s", (online.outcome_id,), role="receiver"), "quarantined")

    def test_offline_assembly_completes_without_receipt(self):
        from backend.waveforms.assembly import Assembler
        assembler = Assembler(lambda:self.connect("assembler"), clock=None, limits=LIMITS)
        session = self.append(self.append(self.create(), 0), 1)
        self.seal(session)
        lease = assembler.claim(uuid4(), gateway_id="GW")
        self.assertIsNotNone(lease)
        self.assertEqual(assembler.run(lease).status, "complete")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.receipts", role="receiver"), 0)
