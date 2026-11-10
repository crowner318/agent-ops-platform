"""Complete synthetic material, risk and intentional missing-data regressions.

All checks are offline. No user settings or real model endpoint is loaded.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from langgraph.types import Command

from release_guard.analysis import review_analysis, validate_analysis
from release_guard.config import Settings
from release_guard.evidence import EvidenceStore, timestamp, tool_names
from release_guard.policy import evaluate
from release_guard.workflow import build_workflow


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "demo_complete"


def material(change):
    store = EvidenceStore(DATA)
    evidence = []
    for name in tool_names:
        args = {"change_id": change}
        if name.startswith("search_"):
            args["query"] = "pool capacity 连接池 发布"
        response = store.execute(name, args)
        if response["ok"]:
            evidence.extend(response["evidence"])
    return evidence, store.scope(change)


class CompleteDemoMaterialTests(unittest.TestCase):
    def test_complete_package_is_separate_and_original_negative_fixture_remains(self):
        original = json.loads((ROOT / "data" / "fixtures.json").read_text(encoding="utf-8"))
        complete = json.loads((DATA / "fixtures.json").read_text(encoding="utf-8"))
        self.assertNotEqual(original["snapshot_id"], complete["snapshot_id"])
        self.assertEqual(set(complete["changes"]), {"safe", "risk-pool", "missing", "legacy-demo"})
        self.assertNotIn("release_purpose", original["changes"]["safe"]["evidence"]["release_snapshot"][0]["content"])
        self.assertNotIn("ci_checks", original["changes"]["missing"]["evidence"])

    def test_all_records_are_synthetic_and_history_is_valid_at_review_time(self):
        payload = json.loads((DATA / "fixtures.json").read_text(encoding="utf-8"))
        EvidenceStore(DATA)  # Validate every fixed record and scope, not one tool.
        for change in payload["changes"].values():
            for kind, rows in change["evidence"].items():
                for row in rows:
                    self.assertIs(row["synthetic"], True)
                    if kind == "runbook":
                        self.assertLessEqual(timestamp(row["content"]["valid_from"]), timestamp(change["as_of"]))
                        self.assertGreaterEqual(timestamp(row["content"]["valid_until"]), timestamp(change["as_of"]))
                    if kind == "incident_case":
                        self.assertLess(timestamp(row["timestamp"]), timestamp(change["as_of"]))

    def test_release_purpose_owner_rollback_and_validation_are_explicit_plans(self):
        for change_id in ("safe", "risk-pool", "missing", "legacy-demo"):
            evidence, _ = material(change_id)
            release = next(row["content"] for row in evidence if row["kind"] == "release_snapshot")
            for key in ("release_purpose", "change_summary", "owner", "rollback_plan", "validation_plan"):
                self.assertTrue(release[key], (change_id, key))
            self.assertIn("虚构", release["owner"]["name"])
            self.assertEqual(release["rollback_plan"]["status"], "planned_not_executed")
            self.assertEqual(release["validation_plan"]["status"], "planned_not_executed")

    def test_safe_explains_unchanged_pool_and_risk_retains_real_demo_arithmetic(self):
        evidence, _ = material("safe")
        release = next(row["content"] for row in evidence if row["kind"] == "release_snapshot")
        self.assertIn("应用", release["change_summary"])
        self.assertIn("不修改", release["configuration_unchanged_reason"])
        self.assertEqual(evaluate(evidence), [])
        evidence, _ = material("risk-pool")
        findings = evaluate(evidence)
        budget = next(item for item in findings if item["code"] == "DEPENDENCY_BUDGET_EXCEEDED")
        self.assertEqual(budget["severity"], "blocker")
        self.assertIn("40", budget["detail"])
        self.assertIn("20", budget["detail"])


class CompleteDemoAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def analyze(self, change):
        evidence, scope = material(change)
        with patch("release_guard.analysis.ChatOpenAI", side_effect=AssertionError("offline test must not construct API client")):
            result = await review_analysis(Settings(), evidence, scope, evaluate(evidence), demo=True)
        return result

    async def test_safe_has_no_fabricated_risk_or_repeated_request_for_provided_material(self):
        result = await self.analyze("safe")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["analysis"]["risks"], [])
        self.assertEqual(result["analysis"]["missing_evidence"], [])
        self.assertTrue(result["analysis"]["process_checks"])
        self.assertEqual(result["model_call"]["attempts"], 0)
        self.assertTrue(all(value is None for value in result["model_call"]["usage"].values()))

    async def test_risk_summary_and_candidate_reference_actual_capacity_blocker(self):
        result = await self.analyze("risk-pool")
        self.assertEqual(result["issues"], [])
        self.assertIn("阻断", result["analysis"]["summary"])
        self.assertEqual(result["analysis"]["missing_evidence"], [])
        self.assertTrue(any("risk-pool-capacity" in risk["evidence_ids"] and "40" in risk["detail"]
                            and "20" in risk["detail"] for risk in result["analysis"]["risks"]))

    async def test_script_itself_does_not_hide_intentionally_missing_ci_and_capacity(self):
        result = await self.analyze("missing")
        gaps = " ".join(result["analysis"]["missing_evidence"])
        self.assertIn("CI", gaps)
        self.assertIn("容量", gaps)
        self.assertNotIn("发布目的", gaps)
        self.assertNotIn("责任人", gaps)
        self.assertNotIn("回退", gaps)

    async def test_provided_plans_are_not_treated_as_facts_after_fields_removed(self):
        evidence, scope = material("safe")
        evidence = deepcopy(evidence)
        release = next(row["content"] for row in evidence if row["kind"] == "release_snapshot")
        del release["rollback_plan"]
        del release["validation_plan"]
        result = await review_analysis(Settings(), evidence, scope, [], demo=True)
        gaps = " ".join(result["analysis"]["missing_evidence"])
        self.assertIn("回退方案", gaps)
        self.assertIn("验证计划", gaps)

    def test_model_reported_gap_is_preserved_instead_of_cleared_for_demo(self):
        evidence, scope = material("safe")
        gap = "未提供演示业务验收人的独立签字记录。"
        candidate = {"summary": "仍有待人工核对的材料说明。", "risks": [],
                     "missing_evidence": [gap], "process_checks": []}
        checked = validate_analysis(candidate, evidence, scope)
        self.assertTrue(checked["ok"], checked["issues"])
        self.assertEqual(checked["analysis"]["missing_evidence"], [gap])


class CompleteDemoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_blocked_and_missing_remain_distinct_after_human_ack(self):
        root = Path(tempfile.gettempdir()) / ("release-complete-test-" + uuid4().hex)
        root.mkdir()
        try:
            with patch.dict(os.environ, {"RELEASE_MCP_TRANSPORT": "inprocess"}):
                async with build_workflow(Settings(), root, DATA, demo=True) as graph:
                    for change, outcome in (("safe", "review_acknowledged"), ("risk-pool", "blocked"), ("missing", "incomplete")):
                        config = {"configurable": {"thread_id": "complete-" + change}}
                        paused = await graph.ainvoke({"change_id": change}, config)
                        self.assertTrue(paused.get("__interrupt__"))
                        self.assertEqual(len(paused["tool_records"]), 7)
                        if change == "missing":
                            self.assertEqual(paused["status"], "incomplete")
                            self.assertTrue(paused["quality_issues"])
                            self.assertIsNone(paused["analysis"])
                            self.assertEqual(paused["model_calls"], [])
                        else:
                            self.assertEqual(paused["quality_issues"], [])
                        finished = await graph.ainvoke(Command(resume={"reviewer": "自动测试演示署名",
                            "decision": "acknowledge", "comment": "离线测试，不是生产批准",
                            "proposed_digest": paused["review_digest"]}), config)
                        self.assertEqual(finished["status"], outcome)
                        self.assertFalse(finished["deployment_approved"])
        finally:
            # Fixture owns only SQLite files directly under its UUID directory.
            for child in root.iterdir():
                child.unlink()
            root.rmdir()


if __name__ == "__main__":
    unittest.main()
