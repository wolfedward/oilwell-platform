"""Offline SOFTWARE vectors; no network, credentials, hardware or platform imports."""
import json
from pathlib import Path
import tempfile

from edge_protection_reference import EdgeProtectionReference, ReferenceStateError


def settings():
    return {'version': 'software-vector-1', 'basis_reference': 'software only, not field settings',
            'approved_by': 'software-vector-author',
            'effective_from': '2026-10-11T00:00:00+00:00',
            'effective_until': '2026-10-12T00:00:00+00:00',
            'maximum_frame_gap_ms': 2000, 'reset_safe_ms': 500,
            'rules': [{'id': 'overcurrent', 'metric': 'current_max_A', 'direction': 'high',
                       'pickup': 10, 'dropout': 8, 'delay_ms': 1000}]}


def frame(ms, amps, **changes):
    second, remainder = divmod(ms, 1000)
    result = {'at': f'2026-10-11T00:00:{second:02d}.{remainder:03d}+00:00',
              'sequence': ms, 'boot_id': 'vector-boot', 'quality': 'valid',
              'configuration_valid': True, 'hardware_trip': False, 'reset_request': 'none',
              'measurements': {'currents_A': [amps] * 3, 'voltages_V': None,
                               'temperature_C': None, 'speed_rpm': None, 'frequency_hz': None}}
    result.update(changes)
    return result


def run():
    checks = []
    def check(name, condition):
        if not condition:
            raise RuntimeError('Reference mismatch: ' + name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix='owp-software-vector-') as folder:
        root = Path(folder) / 'reference'
        config = settings()
        EdgeProtectionReference.initialize(root, config, commissioning_reference='offline software vector')
        adapter = EdgeProtectionReference(root, config)
        try:
            check('boot_inhibited', adapter.status()['shadow_inhibit'])
            adapter.receive(frame(0, 0))
            status = adapter.confirm_local_startup('local-vector', 'software test only')
            check('confirmation_never_enables_hardware', not status['control_enabled'] and not status['hardware_execution'])
            adapter.receive(frame(500, 11))
            row = adapter.receive(frame(1500, 11))
            check('continuous_delay_latches', row['rules']['overcurrent']['state'] == 'would_trip_latched')
        finally:
            adapter.close()
        adapter = EdgeProtectionReference(root, config)
        try:
            check('restart_preserves_latch', adapter.status()['state']['rules']['overcurrent']['state'] == 'would_trip_latched')
            row = adapter.receive(frame(1600, 11, reset_request='local'), local_reset=True)
            check('unsafe_reset_denied', row['reset_decision'] == 'denied')
            adapter.receive(frame(2000, 0))
            row = adapter.receive(frame(2500, 0, reset_request='local'), local_reset=True)
            check('continuous_safe_local_reset', row['reset_decision'] == 'accepted')
            adapter.mark_unavailable('watchdog_timeout')
            try:
                adapter.confirm_local_startup('local-vector', 'stale evidence must fail')
            except ReferenceStateError:
                check('watchdog_invalidates_confirmation', True)
            else:
                check('watchdog_invalidates_confirmation', False)
            row = adapter.receive(frame(3000, 0, quality='invalid', hardware_trip=True))
            check('hardware_trip_priority', row['hardware_inhibit'])
        finally:
            adapter.close()
    return {'status': 'passed', 'source': 'software_fixture', 'checks': checks,
            'hardware_execution': False, 'field_validation': False}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
