from pathlib import Path
import json
import unittest

from ops_agent.demo import DemoClient
from ops_agent.engine import AgentLoop, compact_tool_result
from ops_agent.harness import Harness
from ops_agent.materials import missing_materials, summarize_materials
from ops_agent.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
START, END = "2026-10-19T09:55:00+08:00", "2026-10-19T10:20:00+08:00"


class Memory:
    def add_event(self, *args): pass


class MaterialTests(unittest.TestCase):
    def collect(self, folder="data", environment="demo"):
        registry = SkillRegistry(ROOT / folder, "order-service", environment, START, END)
        result = registry.execute("collect_evidence", {"kinds": ["metrics", "logs", "traces", "changes"]})
        return registry, result

    def test_original_fixture_keeps_real_gaps(self):
        _, result = self.collect()
        self.assertTrue(missing_materials(result["evidence"]))
        self.assertFalse(summarize_materials(result["evidence"])["provided"]["traffic"])

    def test_complete_fixture_provided_materials_and_budget_are_real(self):
        _, result = self.collect("data/demo_complete")
        summary = summarize_materials(result["evidence"])
        self.assertTrue(all(summary["provided"].values()), summary)
        self.assertEqual([], missing_materials(result["evidence"]))
        self.assertTrue(all(r["synthetic"] is True for r in result["evidence"]))
        self.assertLess(len(json.dumps(compact_tool_result(result), ensure_ascii=False)), Harness(ROOT / "harness.yaml").max_output_chars)
        self.assertEqual("演示运维组", summary["incident_context"]["incident_owner"])
        self.assertIs(summary["incident_context"]["rollback_plan"]["executed"], False)
        self.assertIs(summary["incident_context"]["rollback_plan"]["approved"], False)

    def test_complete_demo_generates_evidence_candidate_without_gap_template(self):
        registry, result = self.collect("data/demo_complete")
        loop = AgentLoop(DemoClient(), registry, Harness(ROOT / "harness.yaml"), Memory(), "materials", "order-service", "demo")
        final = loop.run("diagnosis", "分析演示故障", {"evidence": result["evidence"], "workflow": "single"})
        self.assertTrue(final.complete, final.warnings)
        self.assertTrue(final.analysis["hypotheses"])
        self.assertTrue(final.analysis["recommendations"])
        self.assertEqual([], final.analysis["missing_evidence"])
        self.assertFalse(final.analysis["root_cause_confirmed"])
        self.assertFalse(final.analysis["recovery_verified"])
        self.assertIn("演示运维组", final.answer)
        self.assertNotIn("待填写", final.answer)
        self.assertNotIn("尚需核验当前配置、负载和容量", final.answer)
        draft = registry.execute("build_postmortem", {"summary": "待审核演练草稿", "evidence_ids": sorted(registry.observed)})
        self.assertEqual("演示运维组", draft["data"]["incident_owner"])
        self.assertEqual([], draft["data"]["root_cause"]["missing_checks"])
        self.assertFalse(draft["data"]["rollback_plan"]["executed"])

    def test_unrelated_errors_do_not_copy_connection_pool_template(self):
        rows = [
            {"id": "other-metric", "current_telemetry": True, "metric": "request_error_rate", "value": 0.2},
            {"id": "other-error", "current_telemetry": True, "level": "ERROR", "message": "Payment provider rejected an invalid signature."},
            {"id": "other-change", "current_telemetry": True, "change_type": "deployment", "description": "Updated payment signature validator."},
        ]
        summary = summarize_materials(rows)
        self.assertTrue(summary["has_fault_signals"])
        self.assertFalse(summary["has_pool_fault_signals"])
        client = DemoClient()
        response = client.complete([
            {"role": "system", "content": "[role:diagnosis]"},
            {"role": "user", "content": json.dumps({"context": {"evidence": rows, "workflow": "single"}})},
        ], tools=[])
        analysis = json.loads(response["content"])
        self.assertEqual([], analysis["hypotheses"])
        self.assertEqual([], analysis["recommendations"])
        self.assertEqual([], analysis["missing_evidence"])
        self.assertNotIn("tool_calls", response)

    def test_sparse_healthy_staging_does_not_receive_pool_fault_recommendations(self):
        registry, result = self.collect(environment="staging")
        loop = AgentLoop(DemoClient(), registry, Harness(ROOT / "harness.yaml"), Memory(), "staging", "order-service", "staging")
        final = loop.run("diagnosis", "检查预发布运行", {"evidence": result["evidence"], "workflow": "single"})
        self.assertFalse(final.complete)
        self.assertEqual([], final.analysis["hypotheses"])
        self.assertEqual([], final.analysis["recommendations"])
        self.assertTrue(final.analysis["missing_evidence"])
        self.assertFalse(any(r["name"] == "assess_remediation" for r in final.tool_records))


if __name__ == "__main__": unittest.main()
