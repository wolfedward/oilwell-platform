import json
import re
import tempfile
import unittest
from pathlib import Path

from backend.audit import AuditService
from backend.database import Database
from backend.protocol import encode_frame
from backend.seed import seed_database
from backend.services.acquisition import AcquisitionService
from backend.services.analysis import AnalysisService, AnalysisValidationError
from backend.simulator import SimulatorRegistry
from backend.services import analysis
from backend.algorithms.dynagraph_profiles import build_profile_card


EXPECTED_REVIEW_CATALOG = (
    ("normal", "泵工作正常"),
    ("traveling_valve_leak", "游动凡尔漏失"),
    ("standing_valve_leak", "固定凡尔漏失"),
    ("double_valve_leak", "双凡尔漏失"),
    ("rod_break", "抽油杆断脱"),
    ("tubing_leak", "油管漏失"),
    ("supply_shortage", "供液不足"),
    ("gas_interference", "气体影响"),
    ("gas_lock", "气锁"),
    ("discharge_blockage", "出油管线部分蜡堵"),
    ("filter_blockage", "滤沙器及其它附件堵塞"),
    ("high_water_cut", "油井高含水"),
    ("tight_packing", "盘根盒过紧"),
    ("sand_obstruction", "砂阻"),
    ("partial_plunger_exit", "活塞部分脱出工作筒"),
    ("upstroke_hang", "上挂"),
    ("downstroke_hang", "下挂"),
    ("stuck_plunger", "活塞被卡"),
    ("loose_liner", "衬套松动"),
    ("flowing_well", "连抽带喷"),
)


class ReviewCatalogCustomTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.service = AnalysisService(self.database, AuditService(self.database))
        self.actor_id = self.database.scalar(
            "SELECT id FROM users WHERE username='reviewer'"
        )
        registry = SimulatorRegistry(self.database)
        acquisition = AcquisitionService(self.database, registry)
        card = build_profile_card(
            "normal", {"stroke_m": 2.8, "strokes_per_minute": 4.2, "static_rod_load_kn": 40.0}
        )
        card = {key: value for key, value in card.items() if key != "profile"}
        card.update(status="online", connection_mode="virtual", hardware_connected=False)
        ingested = acquisition.ingest(encode_frame("D-001", 9901, card))
        self.assertTrue(ingested.accepted, ingested.error_code)
        row = self.database.query_one(
            "SELECT id FROM dynagraphs WHERE raw_message_id=?", (ingested.raw_message_id,)
        )
        self.diagnosis = self.service.run(
            row["id"], self.database.scalar(
                "SELECT id FROM users WHERE username='algorithm_engineer'"
            )
        )["diagnosis"]

    def test_review_catalog_contains_exactly_the_twenty_standard_profile_codes(self):
        catalog = tuple(getattr(analysis, "REVIEW_CONDITION_CATALOG", ()))
        self.assertEqual(catalog, EXPECTED_REVIEW_CATALOG)

    def test_standard_review_preserves_machine_diagnosis_and_records_version_and_audit(self):
        before = self.database.query_one(
            "SELECT diagnosis_type,confidence,review_version FROM diagnoses WHERE id=?",
            (self.diagnosis["id"],),
        )
        reviewed = self.service.review(
            self.diagnosis["id"], self.actor_id, "rod_break", "现场核验为断脱", before["review_version"]
        )
        after = self.database.query_one(
            "SELECT diagnosis_type,confidence,review_version FROM diagnoses WHERE id=?",
            (self.diagnosis["id"],),
        )
        self.assertEqual(reviewed["reviewer_condition"], "rod_break")
        self.assertEqual((after["diagnosis_type"], after["confidence"]), (before["diagnosis_type"], before["confidence"]))
        self.assertEqual(after["review_version"], before["review_version"] + 1)
        review = self.database.query_one("SELECT * FROM diagnosis_reviews WHERE diagnosis_id=?", (self.diagnosis["id"],))
        self.assertEqual(review["actor_id"], self.actor_id)
        self.assertEqual(review["diagnosis_version"], before["review_version"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='diagnosis.review'"), 1)

    def test_custom_review_requires_safe_two_to_forty_character_name_and_persists_it(self):
        version = self.diagnosis["review_version"]
        for invalid in ("", "a", "x" * 41, "自定义\n工况", "<script>alert(1)</script>"):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(AnalysisValidationError):
                    self.service.review(
                        self.diagnosis["id"], self.actor_id, "custom", "复核意见", version,
                        custom_condition=invalid,
                    )
        reviewed = self.service.review(
            self.diagnosis["id"], self.actor_id, "custom", "现场复核确认", version,
            custom_condition="  井口异响工况  ",
        )
        self.assertEqual(reviewed["reviewer_condition"], "井口异响工况")
        self.assertEqual(self.database.scalar(
            "SELECT reviewer_condition FROM diagnosis_reviews WHERE diagnosis_id=?", (self.diagnosis["id"],)
        ), "井口异响工况")

    def test_frontend_exposes_custom_name_input_and_strict_payload_shape(self):
        source = Path("frontend/pages/diagnosis-review.js").read_text(encoding="utf-8")
        self.assertIn('["custom", "自定义工况"]', source)
        self.assertRegex(source, re.compile(r"custom_condition"))
        self.assertIn("condition === \"custom\"", source)
        for code, _ in EXPECTED_REVIEW_CATALOG:
            self.assertIn(f'"{code}"', source)


if __name__ == "__main__":
    unittest.main()
