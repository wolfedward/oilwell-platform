"""Modbus point-map batching and decoding tests."""

import unittest

from backend.gateway.contracts import PointDefinition, Quality
from backend.gateway.modbus import DecodeError, build_read_blocks, decode_point


class ModbusReadPlanTests(unittest.TestCase):
    def point(self, code, address, *, unit=1, function=3, data_type="uint16"):
        return PointDefinition(code, unit, function, address, data_type)

    def test_contiguous_points_share_block_but_protocol_boundaries_split(self):
        points = [
            self.point("p.a", 0, data_type="float32"),
            self.point("p.b", 2),
            self.point("p.c", 130),
            self.point("p.d", 0, unit=2),
            self.point("p.e", 0, function=4),
        ]
        blocks = build_read_blocks(points, max_gap=0)
        self.assertEqual(
            [(item.unit_id, item.function_code, item.start_address, item.quantity) for item in blocks],
            [(1, 3, 0, 3), (1, 3, 130, 1), (1, 4, 0, 1), (2, 3, 0, 1)],
        )
        self.assertTrue(all(block.quantity <= 125 for block in blocks))

    def test_requested_gap_is_bounded_and_never_creates_oversized_read(self):
        points = [self.point(f"p.{index}", index * 10) for index in range(20)]
        blocks = build_read_blocks(points, max_gap=9)
        self.assertEqual([block.quantity for block in blocks], [121, 61])


class ModbusDecodeTests(unittest.TestCase):
    def test_signed_scaled_value_and_quality_limits(self):
        point = PointDefinition(
            "motor.current", 1, 4, 0, "int16",
            scale=0.1, offset=1.0, engineering_min=-10, engineering_max=10,
        )
        good = decode_point(point, [0xFFCE])  # -50 * 0.1 + 1 = -4
        uncertain = decode_point(point, [0x03E8])
        self.assertEqual((good.value, good.quality), (-4.0, Quality.GOOD))
        self.assertEqual(uncertain.quality, Quality.UNCERTAIN)

    def test_float_word_and_byte_order_are_explicit(self):
        normal = PointDefinition("power.normal", 1, 3, 0, "float32")
        swapped_words = PointDefinition(
            "power.word_swap", 1, 3, 0, "float32", word_order="little"
        )
        swapped_bytes = PointDefinition(
            "power.byte_swap", 1, 3, 0, "float32", byte_order="little"
        )
        self.assertAlmostEqual(decode_point(normal, [0x4148, 0x0000]).value, 12.5)
        self.assertAlmostEqual(decode_point(swapped_words, [0x0000, 0x4148]).value, 12.5)
        self.assertAlmostEqual(decode_point(swapped_bytes, [0x4841, 0x0000]).value, 12.5)

    def test_bad_register_count_or_non_finite_result_is_rejected(self):
        point = PointDefinition("power", 1, 3, 0, "float32")
        with self.assertRaises(DecodeError):
            decode_point(point, [0x4148])
        with self.assertRaises(DecodeError):
            decode_point(point, [0x7F80, 0x0000])


if __name__ == "__main__":
    unittest.main()
