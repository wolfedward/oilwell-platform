import math
import unittest

from backend.studies.contracts import StudyError
from backend.studies.spectrum import analyze_spectrum


def approx(actual, expected, tolerance=1e-8):
    unittest.TestCase().assertAlmostEqual(actual, expected, delta=tolerance)


def payload(*, n=2048, frequency=50.0, signal=None):
    fs = 1024.0
    if signal is None:
        signal = [7 + 3 * math.sin(2 * math.pi * frequency * i / fs) for i in range(n)]
    return {
        "schema": "motor.spectrum/1",
        "capture": {
            "capture_id": "c", "well_id": "w", "device_id": "d",
            "started_at": "2026-10-11T00:00:00+00:00", "sample_rate_hz": fs,
            "samples_A": signal, "quality": ["valid"] * n, "clipped": [False] * n,
            "provenance": {
                "sample_kind": "instantaneous", "uniform_sampling_confirmed": True,
                "clock_error_ppm": 10, "calibration_reference": "cal-1",
                "anti_alias_reference": "filter-1", "anti_alias_passband_hz": 400,
                "manifest_sha256": "a" * 64,
            },
        },
        "motor": {
            "type": "squirrel_cage_induction", "supply": "line_fed",
            "pole_count": 4, "speed_rpm": 1440, "speed_uncertainty_rpm": 0,
            "supply_frequency_hz": 50, "frequency_uncertainty_hz": 0,
            "steady_state_declared": True, "periodic_load_excluded": True,
            "basis_reference": "test fixture",
        },
        "policy": {
            "maximum_clock_error_ppm": 100, "maximum_rms_variation_ratio": 0.1,
            "minimum_fundamental_A": 1, "minimum_sideband_dbc": -35,
            "minimum_sideband_to_noise_db": 6,
        },
    }


def codes(result):
    return {item["code"] for item in result["reasons"]}


class SpectrumTests(unittest.TestCase):
 def test_exact_bin_fft_removes_dc_and_recovers_peak_amplitude(self):
    result = analyze_spectrum(payload())
    spectrum = result["spectrum"]
    assert result["status"] == "completed"
    approx(spectrum["removed_dc_A"], 7)
    approx(spectrum["ac_rms_A"], 3 / math.sqrt(2))
    assert spectrum["bin_spacing_hz"] == 0.5
    approx(spectrum["amplitude_A_peak"][100], 3)
    assert spectrum["frequency_hz"][100] == 50
    assert result["fault_confirmation"] is False
    assert result["control_eligible"] is False


 def test_nonbin_hann_preserves_finite_descriptive_amplitude(self):
    result = analyze_spectrum(payload(frequency=50.25))
    bins = result["spectrum"]["amplitude_A_peak"]
    assert 2.4 < max(bins[99:103]) < 3.1
    assert all(math.isfinite(value) for value in bins)


 def test_malformed_capture_is_rejected(self):
    mutations = [
        lambda p: p["capture"].pop("quality"),
        lambda p: p["capture"].update(samples_A=[0] * 300),
        lambda p: p["capture"]["samples_A"].__setitem__(0, True),
        lambda p: p["capture"]["samples_A"].__setitem__(0, 1e308),
        lambda p: p["capture"]["samples_A"].__setitem__(0, 10**400),
        lambda p: p["motor"].update(pole_count=3),
        lambda p: p["policy"].update(unexpected=1),
    ]
    for mutation in mutations:
        with self.subTest(mutation=mutation):
            p = payload()
            mutation(p)
            with self.assertRaises(StudyError):
                analyze_spectrum(p)


 def test_unsupported_evidence_stays_indeterminate(self):
    changes = [
        (lambda p: p["capture"]["quality"].__setitem__(3, "unknown"), "sample_quality"),
        (lambda p: p["capture"]["clipped"].__setitem__(3, True), "clipped_samples"),
        (lambda p: p["capture"]["provenance"].update(sample_kind="rms"), "not_instantaneous"),
        (lambda p: p["capture"]["provenance"].update(anti_alias_reference=None), "anti_alias_unverified"),
        (lambda p: p["capture"]["provenance"].update(anti_alias_passband_hz=None), "anti_alias_unverified"),
        (lambda p: p["motor"].update(speed_rpm=1500), "invalid_slip"),
        (lambda p: p["motor"].update(speed_uncertainty_rpm=100), "unresolvable_sidebands"),
        (lambda p: p["motor"].update(steady_state_declared=False), "nonsteady"),
    ]
    for change, reason in changes:
        with self.subTest(reason=reason):
            p = payload()
            change(p)
            result = analyze_spectrum(p)
            assert result["support"] == "spectrum_only"
            assert result["finding"] == "indeterminate"
            assert reason in codes(result)
            assert len(result["spectrum"]["frequency_hz"]) == 1025


 def test_both_resolved_sidebands_are_required_for_feature_presence(self):
    n, fs = 2048, 1024
    signal = [3 * math.sin(2 * math.pi * 50 * i / fs)
              + 0.12 * math.sin(2 * math.pi * 46 * i / fs)
              + 0.12 * math.sin(2 * math.pi * 54 * i / fs)
              for i in range(n)]
    p = payload(signal=signal)
    result = analyze_spectrum(p)
    assert result["support"] == "sideband_features_supported"
    assert result["finding"] == "sideband_features_present"
    approx(result["motor_features"]["slip"], 0.04)
    assert result["fault_confirmation"] is False


 def test_absent_sidebands_are_not_a_health_certificate(self):
    result = analyze_spectrum(payload())
    assert result["support"] == "sideband_features_supported"
    assert result["finding"] == "no_resolved_sideband"
    assert result["fault_confirmation"] is False


 def test_unexcluded_periodic_load_is_reported_as_confounder(self):
    p = payload()
    p["motor"]["periodic_load_excluded"] = False
    result = analyze_spectrum(p)
    assert "periodic_load_not_excluded" in result["confounders"]


 def test_inverter_supply_is_not_applicable(self):
    p = payload()
    p["motor"]["supply"] = "inverter_fed"
    result = analyze_spectrum(p)
    assert result["support"] == "not_applicable"
    assert result["finding"] == "indeterminate"


 def test_extreme_finite_samples_do_not_overflow_results(self):
    p = payload(signal=[100000.0 if i % 2 else -100000.0 for i in range(2048)])
    result = analyze_spectrum(p)
    assert all(math.isfinite(x) for x in result["spectrum"]["amplitude_A_peak"])
    assert result["finding"] == "indeterminate"

 def test_tiny_positive_frequency_cannot_overflow_derived_slip(self):
    p = payload()
    p["motor"]["supply_frequency_hz"] = 5e-324
    with self.assertRaises(StudyError):
        analyze_spectrum(p)

 def test_undersampled_sidebands_refuse_quantitative_interpretation(self):
    p = payload()
    p["capture"]["provenance"]["anti_alias_passband_hz"] = 52
    result = analyze_spectrum(p)
    assert result["support"] == "spectrum_only"
    assert "unresolvable_sidebands" in codes(result)
