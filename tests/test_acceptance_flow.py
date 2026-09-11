"""Source contracts for the energy-control acceptance workflow pages."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AcceptanceFlowPageTests(unittest.TestCase):
    PAGES = {
        "energy-overview.js": ("renderEnergyOverview", "/api/reports/energy"),
        "recovery-analysis.js": ("renderRecoveryAnalysis", "/api/strategies"),
        "strategy-editor.js": ("renderStrategyEditor", "/api/strategies/optimize"),
        "strategy-center.js": ("renderStrategyCenter", "/api/strategies"),
        "approvals.js": ("renderApprovals", "/api/strategies"),
        "commands.js": ("renderCommands", "/api/commands"),
        "alarms.js": ("renderAlarms", "/api/alarms"),
        "work-orders.js": ("renderWorkOrders", "/api/work-orders"),
    }

    def source(self, filename):
        return (PROJECT_ROOT / "frontend" / "pages" / filename).read_text(encoding="utf-8")

    def test_pages_export_renderers_and_use_real_api_routes(self):
        for filename, (renderer, endpoint) in self.PAGES.items():
            with self.subTest(filename=filename):
                source = self.source(filename)
                self.assertIn(f"export async function {renderer}", source)
                self.assertIn(endpoint, source)
                self.assertNotIn("Math.random", source)
                self.assertNotIn("演示数据", source)
                self.assertNotIn("演示系统", source)

    def test_mutating_actions_are_guarded_by_server_permission_names(self):
        expected = {
            "strategy-editor.js": ("strategy.create", "strategy.submit"),
            "strategy-center.js": ("strategy.create", "strategy.submit", "devices.control"),
            "approvals.js": ("strategy.approve",),
            "commands.js": ("devices.control",),
            "alarms.js": ("alarms.ack",),
            "work-orders.js": ("maintenance.manage", "diagnosis.review"),
        }
        for filename, permissions in expected.items():
            source = self.source(filename)
            with self.subTest(filename=filename):
                self.assertIn("permissions.has", source)
                for permission in permissions:
                    self.assertIn(permission, source)

    def test_pages_expose_the_complete_traceable_acceptance_chain(self):
        required_copy = {
            "energy-overview.js": ("节电量", "产量保持", "数据覆盖率", "效果评估"),
            "recovery-analysis.js": ("恢复观测点", "时间常数", "拟合优度", "淘汰原因", "候选制度"),
            "strategy-editor.js": ("安全约束", "优化计算", "保存草稿", "提交审批"),
            "strategy-center.js": ("不可变版本", "审批记录", "下发策略", "效果追踪"),
            "approvals.js": ("独立审批", "审批意见", "批准", "驳回"),
            "commands.js": ("签名载荷", "有效期", "安全校验", "分阶段回执"),
            "alarms.js": ("确认报警", "创建工单", "来源对象", "处置状态"),
            "work-orders.js": ("统一追溯链", "处理记录", "独立关闭", "验收复核"),
        }
        for filename, labels in required_copy.items():
            source = self.source(filename)
            with self.subTest(filename=filename):
                for label in labels:
                    self.assertIn(label, source)


if __name__ == "__main__":
    unittest.main()
