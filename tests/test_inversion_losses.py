"""Signed-grid hand calculations catch absolute-power and extrapolation bugs."""
import math
import unittest

from backend.inversion.parameter_contracts import parse_parameter_set
from tests.inversion_vectors import parameter_body, parameter_wire


def loss_parameters():
    body = parameter_body()
    body['domain']['power_W'] = {'min': '-10', 'max': '10'}
    body['drive']['loss_grid'].update(
        power_W=['-10', '0', '10'], motor_speed_rad_s=['1', '3'],
        loss_W=[['2', '6'], ['4', '8'], ['10', '14']],
        error_W=[['1', '3'], ['2', '4'], ['3', '5']])
    return parse_parameter_set(parameter_wire(body))


class LossTests(unittest.TestCase):
    def test_loss_is_nonnegative_and_signed_domain(self):
        from backend.inversion.losses import lookup_loss
        parameters = loss_parameters()
        for power, speed, loss, error in [(-5, 2, 5, 2.5), (5, 2, 9, 3.5),
                                          (-10, 1, 2, 1), (10, 3, 14, 5), (0, 2, 6, 3)]:
            with self.subTest(power=power, speed=speed):
                point = lookup_loss(parameters, power, speed)
                self.assertEqual(point.loss_W, loss)
                self.assertEqual(point.error_W, error)
                self.assertGreaterEqual(point.loss_W, 0)
        for power, speed in [(-11, 2), (11, 2), (0, .9), (0, 3.1), (math.nan, 2), (0, math.inf)]:
            with self.subTest(power=power, speed=speed), self.assertRaises(ValueError):
                lookup_loss(parameters, power, speed)


if __name__ == '__main__':
    unittest.main()
