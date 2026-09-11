"""Visible acceptance contracts for the dual dynagraph operations workspace."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DualDynagraphUiContractTests(unittest.TestCase):
    def test_analysis_page_exposes_two_independently_selectable_cards(self):
        source = (PROJECT_ROOT / "frontend" / "pages" / "dynagraph-analysis.js").read_text(
            encoding="utf-8"
        )
        for marker in (
            'id="toggleInferred"',
            'id="toggleMeasured"',
            "电功图反演示功图",
            "载荷位移传感器示功图",
            "sensor_points",
            "comparison",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_analysis_page_shows_profile_diagnosis_and_engineering_parameters(self):
        source = (PROJECT_ROOT / "frontend" / "pages" / "dynagraph-analysis.js").read_text(
            encoding="utf-8"
        )
        for label in (
            "传感器工况样本",
            "机器诊断",
            "冲程",
            "冲次",
            "最大载荷",
            "最小载荷",
            "载荷差",
            "功图面积",
            "充满度",
            "泵效",
            "产液量",
            "置信度",
            "处置建议",
        ):
            with self.subTest(label=label):
                self.assertIn(label, source)

    def test_analysis_page_shows_eight_algorithm_comparison_metrics(self):
        source = (PROJECT_ROOT / "frontend" / "pages" / "dynagraph-analysis.js").read_text(
            encoding="utf-8"
        )
        for metric in (
            "mae_kn",
            "max_error_kn",
            "rmse_kn",
            "area_error_percent",
            "stroke_error_m",
            "correlation",
            "upstroke_rmse_kn",
            "downstroke_rmse_kn",
        ):
            with self.subTest(metric=metric):
                self.assertIn(metric, source)

    def test_archive_page_has_operational_card_actions(self):
        source = (PROJECT_ROOT / "frontend" / "pages" / "dynagraphs.js").read_text(
            encoding="utf-8"
        )
        for label in ("工况样本", "报警等级", "查看对比", "运行分析"):
            with self.subTest(label=label):
                self.assertIn(label, source)


if __name__ == "__main__":
    unittest.main()
