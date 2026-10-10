"""Exact closed observation support in half-open applicability intervals."""
import unittest
from datetime import datetime,timezone
from fractions import Fraction
from backend.inversion.history import operating_conditions
from backend.inversion.parameter_contracts import parse_parameter_set
from backend.inversion.transactions import InversionError
from backend.waveforms.registry_context import datetime_ns
from tests.inversion_vectors import parameter_wire
from tests.inversion_governance_support import GovernanceCase


class HistoryTests(unittest.TestCase):
    def test_archive_timing_requires_explicit_budget_and_drift_excludes_independence(self):
        from backend.inversion.history import calibration_independence
        parameters=parse_parameter_set(parameter_wire()); declared=parameters.provenance.calibration_cycles[0]
        archive=dict(absolute_start_ns=str(declared.start_ns),absolute_end_ns=str(declared.end_ns),
            cycle_start_ns=str(declared.start_ns),cycle_end_ns=str(declared.end_ns),physical_cycle_key=declared.physical_cycle_id,
            utc_uncertainty_ns='0',source_ranges=[])
        def result(value):
            return calibration_independence(parameters,[value],'new-cycle',Fraction(10000000000),Fraction(11000000000),Fraction(0),[])
        self.assertIn('independence_unproven',result(archive))
        self.assertIn('calibration_time_overlap',result(archive|{'timing':{'total_bound_ns':'20000000000'}}))

    def test_independent_conditions_cover_observation_not_parameter_declaration(self):
        parameters=parse_parameter_set(parameter_wire())
        instant=datetime_ns(datetime(2026,9,29,tzinfo=timezone.utc))
        conditions=operating_conditions(parameters,('condition-1',),Fraction(instant),Fraction(instant+1),Fraction(0))
        self.assertEqual(tuple(item.id for item in conditions),('condition-1',))
        end=datetime_ns(datetime(2027,1,1,tzinfo=timezone.utc))
        with self.assertRaises(InversionError):
            operating_conditions(parameters,('condition-1',),Fraction(end-1),Fraction(end)-Fraction(1,2),Fraction(1,2))
        with self.assertRaises(InversionError):
            operating_conditions(parameters,('forged',),Fraction(instant),Fraction(instant+1),Fraction(0))

    def test_calibration_cycles_cannot_claim_independence_from_unlinked_epoch_times(self):
        from backend.inversion.history import calibration_independence
        parameters=parse_parameter_set(parameter_wire())
        archive=[dict(absolute_start_ns='1790640000000000000',absolute_end_ns='1790640002000000000',
                      physical_cycle_key='actual-cycle',source_ranges=[])]
        result=calibration_independence(parameters,archive,'new-cycle',Fraction(1790640010000000000),
                                        Fraction(1790640012000000000),Fraction(0),[])
        self.assertIn('independence_unproven',result)


class ModbusHistoryTests(GovernanceCase):
    def test_modbus_map_covers_uncertain_observation(self):
        from dataclasses import replace
        from backend.inversion.history import modbus_maps
        from backend.inversion.parameter_contracts import ModbusBinding
        device=self.registry_command('device.create',gateway_id='gateway-1',device_id='device-1',name='Test device',transport='modbus_tcp',usage='commissioning')['entity']
        mapping=self.registry_command('map.create',device['id'],expected_version=device['version'],parent_version=None,
            points=[dict(point_code='load',unit_id=1,function_code=3,address=0,data_type='int16',unit='N')])['entity']
        parameters=parse_parameter_set(parameter_wire()); bindings=list(parameters.provenance.channel_bindings)
        bindings[0]=replace(bindings[0],source='modbus',modbus=ModbusBinding(device['id'],1,mapping['sha256'],'load'))
        parameters=replace(parameters,provenance=replace(parameters.provenance,channel_bindings=tuple(bindings)))
        def evaluate(start,end,u=Fraction(0),value=parameters):
            with self.inversion_fixture.connect('reader') as conn: return modbus_maps(conn.cursor(),value,start,end,u)
        with self.assertRaises(InversionError): evaluate(Fraction(0),Fraction(1))
        reviewed=self.registry_command('map.review',device['id'],expected_version=mapping['device_version'],map_version=mapping['map_version'],sha256=mapping['sha256'],decision='approved')['entity']
        with self.assertRaises(InversionError): evaluate(Fraction(0),Fraction(1))
        well=self.registry_command('well.create',area_code='A',well_code='modbus',name='Synthetic well')['entity']
        bound=self.registry_command('device.bind',device['id'],expected_version=reviewed['device_version'],well_id=well['id'])['entity']
        assigned=self.registry_command('map.assign',device['id'],expected_version=bound['version'],map_version=mapping['map_version'])
        with self.registry_connect() as conn:
            start=Fraction(datetime_ns(conn.execute('SELECT valid_from FROM device_registry.point_map_assignments').fetchone()[0]))
        self.assertEqual(evaluate(start,start+1)[0]['point_id'],'load')
        self.assertEqual(evaluate(start,start+1)[0]['version']['status'],'approved')
        from backend.inversion.series import ProducerEndpoint
        # Map ownership follows the physical producer, not the capture recorder.
        physical=replace(bindings[0].channel_pin,gateway_id='recorder-gateway',device_id='recorder',
                         producer_endpoint=ProducerEndpoint('gateway-1','device-1'))
        distinct=replace(parameters,provenance=replace(parameters.provenance,channel_bindings=(
            replace(bindings[0],channel_pin=physical),*bindings[1:])))
        self.assertEqual(evaluate(start,start+1,value=distinct)[0]['version']['device_id'],device['id'])
        wrong=replace(distinct,provenance=replace(distinct.provenance,channel_bindings=(
            replace(bindings[0],channel_pin=replace(physical,producer_endpoint=ProducerEndpoint('gateway-1','other'))),*bindings[1:])))
        with self.assertRaises(InversionError): evaluate(start,start+1,value=wrong)
        with self.assertRaises(InversionError): evaluate(start,start+1,Fraction(1,2))
        bad=replace(parameters,provenance=replace(parameters.provenance,channel_bindings=(
            replace(bindings[0],modbus=replace(bindings[0].modbus,map_sha256='f'*64)),*bindings[1:])))
        with self.assertRaises(InversionError): evaluate(start,start+1,value=bad)
        self.assertEqual(evaluate(start,start+1,value=parse_parameter_set(parameter_wire())),[])
        successor=self.registry_command('map.create',device['id'],expected_version=assigned['entity']['version'],parent_version='1',
            points=[dict(point_code='load',unit_id=1,function_code=3,address=1,data_type='int16',unit='N')])['entity']
        reviewed=self.registry_command('map.review',device['id'],expected_version=successor['device_version'],map_version='2',sha256=successor['sha256'],decision='approved')['entity']
        self.registry_command('map.assign',device['id'],expected_version=reviewed['device_version'],map_version='2')
        with self.registry_connect() as conn:
            end=Fraction(datetime_ns(conn.execute('SELECT valid_to FROM device_registry.point_map_assignments WHERE map_version=1').fetchone()[0]))
        self.assertEqual(evaluate(start+1,end-Fraction(1,2))[0]['version']['version'],1)
        with self.assertRaises(InversionError): evaluate(start+1,end-Fraction(1,2),Fraction(1,2))
