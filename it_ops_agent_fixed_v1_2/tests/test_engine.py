from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ops_agent.application import choose_mode, execution_summary
from ops_agent.demo import DemoClient
from ops_agent.engine import AgentLoop, AgentResult, compact_tool_result
from ops_agent.harness import Harness
from ops_agent.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]


def final(ids=None, text="当前异常机制尚待验证。"):
    return {"role": "assistant", "content": json.dumps({
        "hypotheses": [{"text": text, "evidence_ids": ids or ["m-order-001"],
                        "validation": ["补充连续监控并验证候选机制。"]}],
        "recommendations": [], "missing_evidence": [], "process_checks": []}, ensure_ascii=False)}


class Memory:
    def __init__(self): self.events = []
    def add_event(self, *args): self.events.append(args)


class Scripted:
    def __init__(self, messages): self.messages, self.requests = list(messages), []
    def complete(self, messages, **kwargs):
        self.requests.append((deepcopy(messages), kwargs))
        item = self.messages.pop(0)
        if isinstance(item, Exception): raise item
        return item


def calls(names):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call-{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for i, name in enumerate(names)]}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(ROOT / "harness.yaml")
        self.registry = SkillRegistry(ROOT / "data", "order-service", "demo",
            "2026-10-19T09:55:00+08:00", "2026-10-19T10:20:00+08:00")
        self.memory = Memory()

    def loop(self, client):
        return AgentLoop(client, self.registry, self.harness, self.memory, "test", "order-service", "demo")

    def test_offline_diagnosis_batches_real_skills(self):
        result = self.loop(DemoClient()).run("diagnosis", "分析问题")
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual(result.model_steps, 3)
        self.assertEqual(self.registry.cache_stats()["backend_executions"], 7)
        self.assertIn("[c-order-001]", result.answer)

    def test_untrusted_context_cannot_invent_evidence(self):
        result = self.loop(Scripted([final(["fake-01"])] )).run("diagnosis", "test", {
            "evidence": [{"id": "fake-01", "current_telemetry": True}]})
        self.assertFalse(result.complete)
        self.assertFalse(result.evidence)

    def test_context_uses_canonical_content(self):
        self.registry.execute("query_metrics", {})
        client = Scripted([final()])
        result = self.loop(client).run("diagnosis", "test", {"evidence": [
            {"id": "m-order-001", "current_telemetry": True, "value": 999999}]})
        self.assertFalse(result.complete)  # One trusted point still is not a full investigation.
        self.assertIn("insufficient_current_material", {issue["code"] for issue in result.quality_issues})
        self.assertEqual(result.evidence[0]["value"], 0.002)
        seed = json.loads(client.requests[0][0][1]["content"])
        self.assertEqual(seed["context"]["evidence"][0]["value"], 0.002)

    def test_unstructured_model_assertions_do_not_become_report_facts(self):
        self.registry.execute("query_metrics", {})
        result = self.loop(Scripted([{"role": "assistant", "content": "根因已确定，已恢复。"}])).run(
            "diagnosis", "test", {"evidence": self.registry.observed_evidence()})
        self.assertFalse(result.complete)
        self.assertNotIn("根因已确定", result.answer)

    def test_unknown_evidence_reference_is_flagged(self):
        result = self.loop(Scripted([calls(["query_metrics"]), final(["fake-001"])])).run("diagnosis", "test")
        self.assertFalse(result.complete)
        self.assertTrue(result.quality_issues)

    def test_budget_finalization_and_oversized_batch(self):
        self.harness.max_tool_calls = 1
        client = Scripted([calls(["query_metrics"]), final()])
        result = self.loop(client).run("diagnosis", "test")
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(client.requests[1][1]["tool_choice"], "none")
        self.assertEqual(result.tool_calls, 1)
        result = self.loop(Scripted([calls(["query_metrics", "search_logs"])])).run("diagnosis", "test")
        self.assertFalse(result.complete)
        self.assertEqual(result.tool_calls, 0)

    def test_remediation_cannot_repeat_readonly_queries(self):
        self.registry.execute("query_metrics", {})
        client = Scripted([calls(["query_metrics"]), final()])
        result = self.loop(client).run("remediation", "test", {"evidence": self.registry.observed_evidence()})
        self.assertEqual(result.tool_calls, 1)
        output = json.loads(client.requests[1][0][-1]["content"])
        self.assertFalse(output["ok"])
        self.assertEqual(self.registry.cache_stats()["backend_executions"], 1)

    def test_answer_over_budget_is_not_success(self):
        self.registry.execute("query_metrics", {})
        self.harness.max_output_chars = 80
        result = self.loop(Scripted([final()])).run("diagnosis", "test", {
            "evidence": self.registry.observed_evidence()})
        self.assertFalse(result.complete)

    def test_private_reasoning_stays_in_protocol(self):
        first = calls(["query_metrics"])
        first["reasoning_content"] = "provider-private-field"
        client = Scripted([first, final()])
        self.loop(client).run("diagnosis", "test")
        self.assertEqual(client.requests[1][0][2]["reasoning_content"], "provider-private-field")

    def test_failed_model_call_still_has_latency_event(self):
        result = self.loop(Scripted([RuntimeError("offline failure")])).run("diagnosis", "test")
        self.assertFalse(result.complete)
        self.assertEqual(result.model_steps, 1)
        self.assertEqual(len(result.model_calls), 1)
        self.assertFalse(result.model_calls[0]["success"])
        self.assertTrue(any(e[3] == "model_call" for e in self.memory.events))

    def test_reused_evidence_is_not_repeated_in_model_payload(self):
        output = self.registry.execute("query_metrics", {})
        compact = compact_tool_result(output, {r["id"] for r in output["evidence"]})
        self.assertFalse(compact["evidence"])
        self.assertEqual(compact["reused_evidence_count"], len(output["evidence"]))
        self.assertTrue(compact["observed_evidence_ids"])

    def test_simple_postmortem_does_not_force_swarm(self):
        self.assertEqual(choose_mode("订单服务异常，请生成复盘"), "single")
        self.assertEqual(choose_mode("跨服务排查"), "swarm")
        self.assertEqual(choose_mode("order-service与payment-service关联排查"), "swarm")

    def test_partial_usage_is_not_treated_as_complete_or_zero(self):
        result = AgentResult("diagnosis", "", model_steps=1, model_calls=[{
            "usage_reported": True, "attempts": 1,
            "usage": {"prompt_tokens": None, "completion_tokens": 12, "total_tokens": None,
                      "cached_prompt_tokens": None}}])
        metrics = execution_summary([result], self.registry, 1000, None)
        self.assertFalse(metrics["usage_complete"])
        self.assertIsNone(metrics["usage"]["prompt_tokens"])
        self.assertEqual(metrics["usage"]["completion_tokens"], 12)
        self.assertIsNone(metrics["cost"])
        self.assertEqual(metrics["usage_partial_calls"], 1)
        self.assertFalse(metrics["cache_usage_complete"])

    def test_partial_collection_keeps_successful_canonical_evidence(self):
        call = calls(["collect_evidence"])
        call["tool_calls"][0]["function"]["arguments"] = json.dumps({"kinds": ["metrics", "logs"]})
        with patch.object(self.registry, "_search_logs", side_effect=ValueError("simulated adapter failure")):
            result = self.loop(Scripted([call, final()])).run("diagnosis", "test")
        self.assertTrue(result.complete, result.warnings)
        self.assertTrue(any(r["id"] == "m-order-001" for r in result.evidence))


if __name__ == "__main__": unittest.main()
