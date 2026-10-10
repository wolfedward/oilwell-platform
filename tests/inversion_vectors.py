"""Hand-authored valid wire examples for the inversion contract tests."""

import json
from hashlib import sha256


PIN = {"id": "11111111-1111-4111-8111-111111111111", "content_sha256": "a" * 64, "review_version": 1}
CHANNEL = {
    "gateway_id": "gateway-1", "device_id": "device-1",
    "capture_id": "22222222-2222-4222-8222-222222222222",
    "stream_id": "stream-1", "channel_id": "channel-1", "manifest_sha256": "b" * 64,
    "calibration_pin": PIN, "sampling_pin": PIN,
    "first_index": 0, "last_index": 10,
}


def policy_body():
    return {
        "schema": "inversion.algorithm-policy/1", "algorithm": "beam_energy_balance/1",
        "model_version": "beam_energy_balance/1", "numerical_margin": "0.001",
        "motion_max_time_span_ns": "100000000", "motion_max_angle_span_rad": "0.5",
        "motion_max_residual_rad": "0.01", "motion_max_condition": "10000",
        "max_load_error_N": "1000", "min_coverage_fraction": "0.8",
        "reference_max_gap_ns": "100000000", "fill_max_time_span_ns": "50000000",
        "fill_max_angle_span_rad": "0.2", "relative_error_min_denominator_N": "1",
    }


def policy_wire():
    return json.dumps(policy_body(), sort_keys=True, separators=(",", ":")).encode()


def parameter_body():
    policy = policy_body()
    policy_sha = sha256(policy_wire()).hexdigest()
    body = json.loads(json.dumps({
        "schema": "inversion.parameter-set/1",
        "assembly_id": "33333333-3333-4333-8333-333333333333",
        "assembly_version": 1, "source_kind": "synthetic",
        "valid_from": "2026-01-01T00:00:00.000000Z",
        "valid_to": "2027-01-01T00:00:00.000000Z", "parent_version": None,
        "provenance": {
            "source_organization": "Software test", "source_date": "2026-01-01", "notes": "Synthetic software evidence",
            "attachment_pins": [PIN],
            "calibration_cycles": [{"start_ns": "0", "end_ns": "1000000000", "physical_cycle_id": "cycle-1"}],
            "operating_conditions": [{
                "id": "condition-1", "valid_from": "2026-01-01T00:00:00.000000Z",
                "valid_to": "2027-01-01T00:00:00.000000Z",
                "temperature_K": {"min": "290", "max": "310"},
                "supply_voltage_V": {"min": "380", "max": "420"},
                "supply_frequency_Hz": {"min": "49", "max": "51"},
                "source_description": "Controlled software case", "attachment_pins": [PIN],
            }],
            "channel_bindings": [{
                "role": role, "source": "native_waveform", "channel_pin": CHANNEL,
                "installation_evidence_refs": [PIN], "modbus": None,
            } for role in ("active_power", "crank_angle", "load", "displacement")],
        },
        "geometry": {
            "pivot_x_m": "1", "pivot_y_m": "2", "crank_radius_m": "0.5",
            "pitman_length_m": "2", "rear_arm_m": "1", "horsehead_radius_m": "1",
            "branch": 1, "rotation_sign": 1, "displacement_sign": 1,
            "evidence_refs": [PIN],
        },
        "phase": {
            "channel_pin": CHANNEL, "zero_reading_rad": "0", "encoder_direction": 1,
            "unwrap_kind": "wrapped_2pi", "evidence_refs": [PIN],
        },
        "bodies": {
            "crank_inertia_kg_m2": "1", "crank_first_moment_x_kg_m": "0",
            "crank_first_moment_y_kg_m": "0", "beam_inertia_kg_m2": "1",
            "beam_first_moment_x_kg_m": "0", "beam_first_moment_y_kg_m": "0",
            "pitman_mass_kg": "1", "pitman_com_fraction": "0.5",
            "pitman_inertia_kg_m2": "1", "gravity_m_s2": "9.80665",
            "component_ledger": [{"id": "motor", "name": "Motor", "owner": "upstream"}],
            "evidence_refs": [PIN],
        },
        "drive": {
            "supply_kind": "direct_line_induction", "ratio": "10",
            "upstream_inertia_kg_m2": "1",
            "loss_grid": {"power_W": ["-10", "10"], "motor_speed_rad_s": ["1", "2"],
                          "loss_W": [["0", "0"], ["0", "0"]],
                          "error_W": [["0", "0"], ["0", "0"]], "evidence_refs": [PIN]},
            "crank_friction_Nm": "0", "viscous_friction_Nm_s": "0",
            "evidence_refs": [PIN],
        },
        "domain": {
            **{name: {"min": "0", "max": "10"} for name in (
                "power_W", "speed_rad_s", "acceleration_rad_s2", "temperature_K",
                "supply_voltage_V", "supply_frequency_Hz", "bandwidth_Hz", "window_ns", "load_N")},
            "evidence_refs": [PIN],
        },
        "uncertainty": {
            "torque_bound_Nm": "0", "factor_bound_m": "0", "speed_bound_rad_s": "0",
            "numerical_budget_Nm": "0", "numerical_factor_budget_m": "0", "evidence_refs": [PIN],
        },
        "policy": {"body": policy, "content_sha256": policy_sha},
    }))


    def attachment(purpose):
        return {'id': PIN['id'], 'content_sha256': PIN['content_sha256'],
                'purpose': purpose, 'source_description': 'Explicit synthetic report section for ' + purpose}
    for group in ('geometry', 'phase', 'bodies', 'drive', 'domain', 'uncertainty'):
        body[group]['evidence_refs'] = [attachment(group)]
    body['drive']['loss_grid']['evidence_refs'] = [attachment('loss_grid')]
    body['provenance']['attachment_pins'] = [attachment('provenance')]
    for item in body['provenance']['operating_conditions']:
        item['attachment_pins'] = [attachment('operating_conditions')]
    for item in body['provenance']['channel_bindings']:
        item['installation_evidence_refs'] = [attachment('installation')]
    canonical = lambda item: json.dumps(item, sort_keys=True, separators=(',', ':')).encode()
    body['provenance']['coverage'] = {
        'policy_sha256': policy_sha,
        'domain_sha256': sha256(canonical(body['domain'])).hexdigest(),
        'uncertainty_sha256': sha256(canonical(body['uncertainty'])).hexdigest(),
        'max_time_alignment_error_ns': '0',
        'scopes': [{'scope': scope, 'attachment_pin': attachment(scope),
                    'basis': 'Known synthetic bound; uN/uJ contribution described in section ' + scope}
                   for scope in ('loss_calibration', 'time_alignment', 'averaged_power_mismatch',
                     'motion_angle_policy', 'upstream_dynamics', 'load_range', 'geometry_envelope', 'crank_friction')]}
    return body


def parameter_wire(body=None):
    return json.dumps(parameter_body() if body is None else body, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")
