"""Tag real loopback reads before uplink, never rewrite stored telemetry."""
from dataclasses import replace
from backend.gateway.poller import ModbusPoller


class SoftwareFixtureModbusPoller:
    def __init__(self,gateway_id,device):
        if gateway_id!='REVIEW-GW' or device.transport!='tcp' or device.host!='127.0.0.1' or device.device_id not in (
                'TESTPACK-PUMP-001','TESTPACK-PUMP-002'):
            raise ValueError('only the isolated testpack software device may use this adapter')
        self.poller=ModbusPoller(gateway_id,device)
        self.device=device

    def poll(self,*,sequence):
        result=self.poller.poll(sequence=sequence)
        return replace(result,samples=tuple(replace(sample,source='software-fixture-modbus-tcp') for sample in result.samples))
