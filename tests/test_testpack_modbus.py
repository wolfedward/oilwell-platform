import socket
import struct
import unittest
from backend.gateway.poller import ModbusDevice,ModbusPoller
from tools.testpack.history import POINTS,telemetry_point_map
from tools.testpack.modbus_device import ReadOnlyModbusDevice
from tools.testpack.gateway_adapter import SoftwareFixtureModbusPoller


class TestPackModbus(unittest.TestCase):
    def setUp(self):
        self.device=ReadOnlyModbusDevice({i:telemetry_point_map(i) for i in (1,2)},host='127.0.0.1',port=0)
        self.device.start();self.addCleanup(self.device.stop)
        self.device.set_frame(1,{p:float(i+1) for i,p in enumerate(POINTS)})
        self.device.set_frame(2,{p:float(101+i) for i,p in enumerate(POINTS)})

    def request(self,pdu,unit=1):
        with socket.create_connection(('127.0.0.1',self.device.port),timeout=2) as connection:
            connection.sendall(struct.pack('>HHHB',7,0,len(pdu)+1,unit)+pdu)
            raw=connection.recv(260)
            self.assertEqual(struct.unpack('>HHH',raw[:6]),(7,0,len(raw)-6))
            return raw[7:]

    def test_real_tcp_device_rejects_writes(self):
        before=self.request(struct.pack('>BHH',4,0,24))
        self.assertEqual(before[:2],bytes((4,48)))
        for pdu in (struct.pack('>BHH',6,0,9),struct.pack('>BHHBH',16,0,1,2,9)):
            self.assertEqual(self.request(pdu),bytes((pdu[0]|128,1)))
        self.assertEqual(self.request(struct.pack('>BHH',4,0,24)),before)
        self.assertEqual(self.request(struct.pack('>BHH',3,0,24))[1:],before[1:])

    def test_two_unit_banks_and_invalid_addresses(self):
        one=self.request(struct.pack('>BHH',4,0,2),1)
        two=self.request(struct.pack('>BHH',4,0,2),2)
        self.assertEqual(struct.unpack('>f',one[2:])[0],1)
        self.assertEqual(struct.unpack('>f',two[2:])[0],101)
        self.assertEqual(self.request(struct.pack('>BHH',4,24,1)),b'\x84\x02')
        self.assertEqual(self.request(struct.pack('>BHH',4,0,126)),b'\x84\x03')

    def test_provenance_changes_only_after_actual_read(self):
        device=ModbusDevice('TESTPACK-PUMP-001','tcp',telemetry_point_map(),host='127.0.0.1',port=self.device.port,retries=0)
        real=ModbusPoller('REVIEW-GW',device).poll(sequence=0)
        wrapped=SoftwareFixtureModbusPoller('REVIEW-GW',device).poll(sequence=0)
        self.assertFalse(real.errors or wrapped.errors)
        self.assertEqual([s.value for s in real.samples],[s.value for s in wrapped.samples])
        self.assertTrue(all(s.source=='modbus-tcp' for s in real.samples))
        self.assertTrue(all(s.source=='software-fixture-modbus-tcp' for s in wrapped.samples))
        self.assertGreaterEqual(wrapped.measured_at,real.measured_at)
        self.assertGreaterEqual(len(self.device.exchanges),2)

    def test_nonloopback_and_uninitialized_bank_refused(self):
        with self.assertRaises(ValueError):
            ReadOnlyModbusDevice({1:telemetry_point_map()},host='0.0.0.0',port=0)
        with self.assertRaises(ValueError):self.device.set_frame(1,{'flow_rate_m3_h':1})
        before=self.request(struct.pack('>BHH',4,0,2))
        with self.assertRaises(ValueError):self.device.set_frame(1,{p:float('nan') for p in POINTS})
        self.assertEqual(before,self.request(struct.pack('>BHH',4,0,2)))


if __name__=='__main__':unittest.main()
