"""Strict parameter, policy and exact series boundary tests."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from fractions import Fraction
from types import MappingProxyType
from uuid import UUID

from backend.inversion.contracts import (AuthorityRef, AuthoritySnapshot, InversionCommand,
                                         VersionPrecondition, canonical_bytes)
from backend.inversion.parameter_contracts import missing_fields, parse_parameter_set
from backend.inversion.policy import parse_policy
from backend.inversion.series import ChannelPin, PredictionInputs, Sample, SampleSeries
from tests.inversion_vectors import CHANNEL, parameter_body, parameter_wire, policy_body, policy_wire
from backend.waveforms.commands import Actor


class InversionContractTests(unittest.TestCase):
    def test_partial_coverage_rejects_duplicate_scope_and_wrong_purpose(self):
        for mutation in ('duplicate','purpose','nested_duplicate','nested_purpose'):
            body=parameter_body(); del body['provenance']['source_date']
            scopes=body['provenance']['coverage']['scopes']
            if mutation.startswith('nested'): del body['provenance']['coverage']['max_time_alignment_error_ns']
            if mutation.endswith('duplicate'): scopes[1]=scopes[0].copy()
            else: scopes[0]['attachment_pin']['purpose']='geometry'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body),draft=True)

    def test_attachment_pins_are_not_reviewed_waveform_evidence(self):
        body = parameter_body()
        pin = {'id': body['provenance']['attachment_pins'][0]['id'],
               'content_sha256': 'a' * 64, 'purpose': 'provenance',
               'source_description': 'Synthetic calculation report, section 1'}
        body['provenance']['attachment_pins'] = [pin]
        parsed = parse_parameter_set(parameter_wire(body))
        self.assertEqual(parsed.provenance.attachment_pins[0].purpose, 'provenance')
        self.assertFalse(hasattr(parsed.provenance.attachment_pins[0], 'review_version'))
        pin['review_version'] = 1
        with self.assertRaises(ValueError): parse_parameter_set(parameter_wire(body))

    def test_parameter_parent_is_independent_of_mechanical_version(self):
        for mechanical, parent in ((1, 1), (2, None), (2, 4)):
            body = parameter_body()
            body.update(assembly_version=mechanical, parent_version=parent)
            parsed = parse_parameter_set(parameter_wire(body))
            self.assertEqual(parsed.parent_version, parent)
        for parent in (0, -1, True, "1"):
            body = parameter_body(); body['parent_version'] = parent
            with self.assertRaises(ValueError): parse_parameter_set(parameter_wire(body))

    def test_command_preconditions_and_authority_are_exact(self):
        actor = Actor("local", "7", "Tester", "engineer")
        command = InversionCommand(actor, UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                                   VersionPrecondition("absent"), "Create")
        self.assertEqual(command.expected.version, None)
        self.assertEqual(VersionPrecondition("match", 1).version, 1)
        for kind, version in (("match", 0), ("match", True), ("absent", 1), ("other", None)):
            with self.subTest(kind=kind, version=version), self.assertRaises(ValueError):
                VersionPrecondition(kind, version)
        with self.assertRaises(ValueError):
            InversionCommand(actor, command.key, VersionPrecondition("absent"), " ")
        ref = AuthorityRef("local", 7, 9)
        snapshot = AuthoritySnapshot(ref, frozenset({"inversion.run"}),
                                     datetime(2026, 1, 1, tzinfo=timezone.utc), 1)
        self.assertEqual(snapshot.ref.user_id, 7)
        with self.assertRaises(ValueError):
            AuthorityRef("local", "7", 9)
        with self.assertRaises(ValueError):
            AuthoritySnapshot(ref, frozenset({"inversion.run"}), datetime(2026, 1, 1), 1)

    def test_parameter_contract_is_strict(self):
        good = parse_parameter_set(parameter_wire())
        self.assertEqual(good.schema, "inversion.parameter-set/1")
        self.assertEqual(good.raw_sha256, __import__("hashlib").sha256(parameter_wire()).hexdigest())
        self.assertEqual(good.raw, parameter_wire())
        for raw in (b'{"schema":1,"schema":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_parameter_set(raw)
        for mutation in (
            lambda p: p.update(extra=True),
            lambda p: p.update(schema="unknown"),
            lambda p: p["geometry"].update(evidence_refs=[]),
            lambda p: p["geometry"].update(crank_radius_m="1+2"),
        ):
            body = parameter_body(); mutation(body)
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body))
        with self.assertRaises(FrozenInstanceError):
            good.geometry = None

    def test_loss_grid_boundaries(self):
        body = parameter_body()
        self.assertEqual(len(parse_parameter_set(parameter_wire(body)).drive.loss_grid.power_W), 2)
        for size in (1, 65):
            body = parameter_body(); grid = body["drive"]["loss_grid"]
            grid["power_W"] = [str(i) for i in range(size)]
            grid["loss_W"] = [["0", "0"] for _ in range(size)]
            grid["error_W"] = [["0", "0"] for _ in range(size)]
            with self.subTest(size=size), self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body))
        body = parameter_body(); grid = body["drive"]["loss_grid"]
        grid["power_W"] = [str(i) for i in range(64)]
        grid["motor_speed_rad_s"] = [str(i) for i in range(64)]
        grid["loss_W"] = [["0"] * 64 for _ in range(64)]
        grid["error_W"] = [["0"] * 64 for _ in range(64)]
        self.assertEqual(len(parse_parameter_set(parameter_wire(body)).drive.loss_grid.power_W), 64)
        for change in (lambda g: g["loss_W"][0].__setitem__(0, "-1"),
                       lambda g: g["loss_W"].__setitem__(0, ["0"])):
            body = parameter_body(); change(body["drive"]["loss_grid"])
            with self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body))

    def test_no_implicit_parameters(self):
        body = parameter_body(); del body["geometry"]["crank_radius_m"]
        draft = parse_parameter_set(parameter_wire(body), draft=True)
        self.assertEqual(missing_fields(draft), ("geometry.crank_radius_m",))
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))
        body = parameter_body(); body["geometry"]["crank_radius_m"] = None
        self.assertEqual(missing_fields(parse_parameter_set(parameter_wire(body), draft=True)),
                         ("geometry.crank_radius_m",))
        self.assertEqual(missing_fields(parse_parameter_set(parameter_wire())), ())
        body = parameter_body(); del body["assembly_id"]
        self.assertEqual(missing_fields(parse_parameter_set(parameter_wire(body), draft=True)),
                         ("assembly_id",))
        body = parameter_body(); del body["geometry"]["crank_radius_m"]
        body["geometry"]["pivot_x_m"] = "1+2"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["provenance"]["source_date"]
        body["provenance"]["operating_conditions"][0]["temperature_K"]["min"] = "300+1"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["provenance"]["source_date"]
        body["provenance"]["calibration_cycles"][0]["end_ns"] = "1+2"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["bodies"]["gravity_m_s2"]
        body["bodies"]["component_ledger"].append(body["bodies"]["component_ledger"][0])
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["drive"]["ratio"]
        body["drive"]["crank_friction_Nm"] = "-1"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["provenance"]["source_date"]
        body["provenance"]["channel_bindings"][0]["source"] = "script"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["policy"]["content_sha256"]
        body["policy"]["body"]["numerical_margin"] = "1+2"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); body["phase"]["unwrap_kind"] = ["wrapped_2pi"]
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))
        body = parameter_body()
        body["provenance"]["operating_conditions"][0]["temperature_K"] = {"min": "300", "max": "300"}
        self.assertEqual(parse_parameter_set(parameter_wire(body)).provenance.operating_conditions[0].temperature_K.min,
                         __import__("decimal").Decimal("300"))

    def test_draft_lists_nested_missing_paths_without_computable_groups(self):
        body = parameter_body()
        del body["geometry"]["evidence_refs"][0]["content_sha256"]
        body["provenance"]["calibration_cycles"][0]["end_ns"] = None
        del body["provenance"]["operating_conditions"][0]["temperature_K"]["max"]
        body["provenance"]["channel_bindings"][0]["channel_pin"]["sampling_pin"]["review_version"] = None
        del body["policy"]["body"]["motion_max_condition"]
        body["drive"]["loss_grid"]["loss_W"][0][1] = None
        draft = parse_parameter_set(parameter_wire(body), draft=True)
        self.assertEqual(missing_fields(draft), (
            "drive.loss_grid.loss_W[0][1]",
            "geometry.evidence_refs[0].content_sha256",
            "policy.body.motion_max_condition",
            "provenance.calibration_cycles[0].end_ns",
            "provenance.channel_bindings[0].channel_pin.sampling_pin.review_version",
            "provenance.operating_conditions[0].temperature_K.max",
        ))
        for group in (draft.drive, draft.geometry, draft.policy, draft.provenance):
            self.assertIsNone(group)
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))

    def test_draft_rejects_bad_supplied_nested_values(self):
        for mutate in (
            lambda p: p["geometry"]["evidence_refs"][0].update(review_version="1+2"),
            lambda p: p["provenance"]["calibration_cycles"][0].update(start_ns="1+2"),
            lambda p: p["provenance"]["operating_conditions"][0]["temperature_K"].update(min="300+1"),
            lambda p: p["provenance"]["channel_bindings"][0]["channel_pin"].update(stream_id=["stream-1"]),
            lambda p: p["policy"]["body"].update(numerical_margin="1+2"),
            lambda p: p["drive"]["loss_grid"]["loss_W"][0].__setitem__(0, "1+2"),
            lambda p: p["geometry"]["evidence_refs"][0].update(unknown="x"),
        ):
            body = parameter_body(); del body["policy"]["body"]["motion_max_condition"]
            mutate(body)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); del body["policy"]["body"]["motion_max_condition"]
        body["policy"]["body"]["numerical_margin"] = "1"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)
        body = parameter_body(); body["provenance"]["operating_conditions"][0]["source_description"] = None
        body["provenance"]["operating_conditions"][0]["temperature_K"] = {"min": "310", "max": "290"}
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

    def test_modbus_draft_requires_mapping_identity(self):
        body = parameter_body()
        body["provenance"]["channel_bindings"][0]["source"] = "modbus"
        draft = parse_parameter_set(parameter_wire(body), draft=True)
        self.assertEqual(missing_fields(draft), ("provenance.channel_bindings[0].modbus",))
        self.assertIsNone(draft.provenance)
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))

    def test_partial_lists_still_enforce_supplied_id_uniqueness(self):
        body = parameter_body()
        body["bodies"]["component_ledger"].append({"id": "motor", "name": None, "owner": "beam"})
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

        body = parameter_body()
        body["geometry"]["evidence_refs"].append({
            "id": body["geometry"]["evidence_refs"][0]["id"],
            "content_sha256": None, "review_version": 1})
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

        body = parameter_body()
        body["geometry"]["evidence_refs"][0]["id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        body["geometry"]["evidence_refs"].append({
            "id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "content_sha256": None, "review_version": 1})
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

        body = parameter_body()
        condition = body["provenance"]["operating_conditions"][0].copy()
        condition["source_description"] = None
        body["provenance"]["operating_conditions"].append(condition)
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

        body = parameter_body()
        body["provenance"]["channel_bindings"][0]["channel_pin"]["sampling_pin"]["review_version"] = None
        body["provenance"]["channel_bindings"][1]["role"] = "active_power"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

    def test_partial_loss_grid_still_enforces_axis_dimensions(self):
        for matrix in ("loss_W", "error_W"):
            body = parameter_body(); grid = body["drive"]["loss_grid"]
            grid[matrix][0].append("0")
            grid[matrix][1][0] = None
            with self.subTest(matrix=matrix), self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body), draft=True)

    def test_partial_axes_order_supplied_nodes_across_null_slots(self):
        bad_axes = {
            0: (None, "3", "2"),
            1: ("2", None, "1"),
            2: ("2", "2", None),
        }
        for axis in ("power_W", "motor_speed_rad_s"):
            for null_index in (0, 1, 2):
                body = parameter_body(); grid = body["drive"]["loss_grid"]
                grid[axis] = ["1", "2", "3"]
                if axis == "power_W":
                    for matrix in ("loss_W", "error_W"):
                        grid[matrix].append(["0", "0"])
                else:
                    for matrix in ("loss_W", "error_W"):
                        for row in grid[matrix]: row.append("0")
                grid[axis][null_index] = None
                with self.subTest(axis=axis, null_index=null_index, state="valid"):
                    draft = parse_parameter_set(parameter_wire(body), draft=True)
                    self.assertEqual(missing_fields(draft), (f"drive.loss_grid.{axis}[{null_index}]",))
                    self.assertIsNone(draft.drive)
                grid[axis] = list(bad_axes[null_index])
                with self.subTest(axis=axis, null_index=null_index, state="invalid"), self.assertRaises(ValueError):
                    parse_parameter_set(parameter_wire(body), draft=True)
            body = parameter_body(); grid = body["drive"]["loss_grid"]
            grid[axis] = ["1", None, "1"]
            if axis == "power_W":
                for matrix in ("loss_W", "error_W"): grid[matrix].append(["0", "0"])
            else:
                for matrix in ("loss_W", "error_W"):
                    for row in grid[matrix]: row.append("0")
            with self.subTest(axis=axis, state="equal_across_null"), self.assertRaises(ValueError):
                parse_parameter_set(parameter_wire(body), draft=True)

    def test_missing_axis_preserves_compatible_matrices_but_rejects_conflicts(self):
        body = parameter_body(); grid = body["drive"]["loss_grid"]
        grid["power_W"] = None
        self.assertEqual(missing_fields(parse_parameter_set(parameter_wire(body), draft=True)),
                         ("drive.loss_grid.power_W",))
        grid["error_W"].append(["0", "0"])
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

        body = parameter_body(); grid = body["drive"]["loss_grid"]
        grid["motor_speed_rad_s"] = None
        self.assertEqual(missing_fields(parse_parameter_set(parameter_wire(body), draft=True)),
                         ("drive.loss_grid.motor_speed_rad_s",))
        grid["loss_W"][0].append("0")
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

    def test_load_domain_is_explicit_and_strict(self):
        body = parameter_body(); del body["domain"]["load_N"]
        self.assertEqual(missing_fields(parse_parameter_set(parameter_wire(body), draft=True)),
                         ("domain.load_N",))
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))
        body = parameter_body(); body["domain"]["load_N"] = {"min": "10", "max": "10"}
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))
        body = parameter_body(); body["domain"]["load_N"]["max"] = "10+2"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body), draft=True)

    def test_exact_index_and_reference_separation(self):
        self.assertEqual(Sample(Fraction(1, 3), 9007199254741001, Fraction(2), 0).index,
                         9007199254741001)
        self.assertFalse({"load", "displacement"} & set(PredictionInputs.__dataclass_fields__))
        pin = ChannelPin.from_wire(CHANNEL)
        large_channel = CHANNEL | {"first_index": 9007199254741001, "last_index": 9007199254741001}
        series = SampleSeries("active_power", (Sample(Fraction(1, 3), 9007199254741001, Fraction(2), 0),),
                              ChannelPin.from_wire(large_channel))
        self.assertEqual(series.samples[0].time_ns, Fraction(1, 3))
        with self.assertRaises(ValueError):
            SampleSeries("active_power", (Sample(Fraction(2), 1, Fraction(1), 0),
                                          Sample(Fraction(1), 2, Fraction(1), 0)), pin)
        with self.assertRaises(ValueError):
            Sample(Fraction(0), True, Fraction(1), 0)
        with self.assertRaises(ValueError):
            Sample(Fraction(0), 1, Fraction(1), 5)
        with self.assertRaises(ValueError):
            Sample(Fraction(0), 1, Fraction(1), 1)
        with self.assertRaises(ValueError):
            SampleSeries("active_power", (Sample(Fraction(0), 11, Fraction(1), 0),), pin)

    def test_policy_and_canonical_decimal_are_strict(self):
        self.assertEqual(parse_policy(policy_wire()).algorithm, "beam_energy_balance/1")
        self.assertEqual(canonical_bytes({"amount": "0.125"}), b'{"amount":"0.125"}')
        self.assertEqual(canonical_bytes(MappingProxyType({"amount": "0.125"})),
                         b'{"amount":"0.125"}')
        with self.assertRaises(ValueError):
            canonical_bytes({"amount": 0.125})
        with self.assertRaises(ValueError):
            parse_policy(policy_wire().replace(b'"0.001"', b'"1+2"'))
        model_policy = policy_body(); model_policy["model_version"] = "baseline-2026.1"
        self.assertEqual(parse_policy(canonical_bytes(model_policy)).model_version, "baseline-2026.1")

    def test_uuid_rule_matches_existing_waveform_contract(self):
        body = parameter_body(); body["assembly_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
        self.assertEqual(str(parse_parameter_set(parameter_wire(body)).assembly_id),
                         body["assembly_id"].lower())

    def test_body_size_limit_checks_bytes_before_json_decode(self):
        body = parameter_body()
        body["provenance"]["notes"] = "X" * 10
        baseline = len(parameter_wire(body))
        body["provenance"]["notes"] = "X" * (10 + 262144 - baseline)
        self.assertEqual(len(parameter_wire(body)), 262144)
        self.assertEqual(len(parse_parameter_set(parameter_wire(body)).raw), 262144)
        body["provenance"]["notes"] += "X"
        with self.assertRaises(ValueError):
            parse_parameter_set(parameter_wire(body))


if __name__ == "__main__":
    unittest.main()
