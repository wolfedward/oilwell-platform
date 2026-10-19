from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

from tools.testpack.telemetry_run import run_steps, STEPS, stored_complete, measurement_mapping


class TestPackTelemetryRun(unittest.TestCase):
    def test_registry_then_actual_poll_then_history(self):
        calls=[]
        class Recorder:
            def step(self,name):
                calls.append(name)
                return {'passed':True,'baseline_messages':16,'baseline_samples':192}
        with TemporaryDirectory() as folder:
            checks=run_steps(Recorder(),Path(folder))
        self.assertTrue(all(c.status=='passed' for c in checks))
        self.assertTrue(all(c.layer=='software_network' for c in checks))
        self.assertLess(calls.index('registry'),calls.index('poll'))
        self.assertLess(calls.index('poll'),calls.index('stored'))
        self.assertLess(calls.index('stored'),calls.index('history'))

    def test_simulated_clock_is_not_registration_clock(self):
        registered=datetime.now(timezone.utc)
        row={'message_id':'m','payload_sha256':'a'*64}
        actual=(registered+timedelta(seconds=1)).isoformat()
        mapping=measurement_mapping('history-001',0,'2026-10-01T00:00:00Z',actual,row,registered)
        self.assertEqual(mapping['simulated_at'],'2026-10-01T00:00:00Z')
        self.assertEqual(mapping['measured_at'],actual)
        with self.assertRaises(ValueError):measurement_mapping('h',0,actual,(registered-timedelta(seconds=1)).isoformat(),row,registered)

    def test_puback_is_not_database_success(self):
        self.assertFalse(stored_complete({'counts':{'stored':0,'broker_confirmed':16},'pending_application':16},16))
        self.assertFalse(stored_complete({'counts':{'stored':16},'pending_application':1},16))
        self.assertTrue(stored_complete({'counts':{'stored':16},'pending_application':0},16))

    def test_failure_keeps_prior_receipts_and_not_run_steps(self):
        class Failing:
            def step(self,name):
                if name=='stored':raise TimeoutError('do not leak private detail')
                return {'passed':True}
        with TemporaryDirectory() as folder:
            checks=run_steps(Failing(),Path(folder))
            self.assertEqual([c.status for c in checks[:4]],['passed','passed','failed','not_run'])
            text=(Path(folder)/'telemetry-checks.json').read_text()
            self.assertNotIn('do not leak private detail',text)
            self.assertIn('TimeoutError',text)
            self.assertEqual(len(checks),len(STEPS))

    def test_wrapped_poller_keeps_runtime_device_identity(self):
        from tools.testpack.gateway_adapter import SoftwareFixtureModbusPoller
        from backend.gateway.poller import ModbusDevice
        from tools.testpack.history import telemetry_point_map
        device=ModbusDevice('TESTPACK-PUMP-001','tcp',telemetry_point_map(),host='127.0.0.1',port=15020)
        self.assertEqual(SoftwareFixtureModbusPoller('REVIEW-GW',device).device,device)

    def test_registry_creation_requires_actual_201_contract(self):
        from tools.testpack.telemetry_run import TelemetrySession
        session=object.__new__(TelemetrySession)
        session.http=Mock()
        session.http.request.return_value={'data':{'entity':{'id':'created'}}}
        for path,status in (('/api/registry/wells',201),('/api/registry/devices',201),
                            ('/api/registry/devices/id/point-maps',201),('/api/registry/devices/id/state',200)):
            session.post(path)
            self.assertEqual(session.http.request.call_args.kwargs['expected'],status)


if __name__=='__main__':unittest.main()
