"""Replay sanitized provider shapes through the real, permissioned tool loop."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ops_agent.engine import AgentLoop
from ops_agent.harness import Harness
from ops_agent.skills import SkillRegistry


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "qwen_real_responses.json").read_text(encoding="utf-8"))


class Memory:
    def __init__(self):
        self.events = []

    def add_event(self, *args):
        self.events.append(deepcopy(args))


class Scripted:
    def __init__(self, messages, json_mode=True):
        self.messages, self.requests = deepcopy(list(messages)), []
        self.json_mode = json_mode

    def supports_json_mode(self):
        return self.json_mode

    def complete(self, messages, **kwargs):
        self.requests.append((deepcopy(messages), deepcopy(kwargs)))
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def captured(name):
    return dict(deepcopy(FIXTURE["responses"][name]), role="assistant")


def final():
    return {"role": "assistant", "content": json.dumps({
        "hypotheses": [{"text": "当前异常机制尚待验证。", "evidence_ids": ["m-order-001"],
                        "validation": ["补充连续监控并验证候选机制。"]}],
        "recommendations": [], "missing_evidence": [], "process_checks": [],
    }, ensure_ascii=False)}


def text_plan(items):
    return {"role": "assistant", "content": json.dumps(items, ensure_ascii=False)}


class RealProtocolEngineTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(ROOT / "harness.yaml")
        scope = FIXTURE["scope"]
        self.registry = SkillRegistry(ROOT / "data", scope["service"], scope["environment"],
                                      scope["start"], scope["end"])
        self.memory = Memory()

    def loop(self, client):
        return AgentLoop(client, self.registry, self.harness, self.memory,
                         "real-protocol", "order-service", "demo")

    def context(self, workflow="single", runbooks=True):
        kinds = ["metrics", "logs", "traces", "changes"]
        if runbooks:
            kinds.append("runbooks")
        output = self.registry.execute("collect_evidence", {"kinds": kinds})
        self.assertTrue(output["ok"])
        return {"workflow": workflow, "evidence": self.registry.observed_evidence()}

    def trajectory(self):
        return [event[-1] for event in self.memory.events if event[3] == "trajectory"][-1]

    def test_real_single_text_plan_runs_runbooks_without_fabricating_native_messages(self):
        context = self.context(runbooks=False)
        original = captured("single_diagnosis")
        client = Scripted([original, final()])
        result = self.loop(client).run("diagnosis", "分析订单服务连接池异常", context)
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.tool_records[0]["name"], "collect_evidence")
        self.assertEqual(result.tool_records[0]["arguments"]["kinds"], ["runbooks"])
        self.assertEqual(result.tool_records[0]["origin"], "text_plan")
        self.assertTrue(result.tool_records[0]["ok"])
        self.assertTrue(any(row["id"] == "rb-order-pool-v3" for row in result.evidence))
        history = client.requests[1][0]
        self.assertEqual(history[2], original)
        self.assertTrue(all(message["role"] != "tool" for message in history))
        self.assertTrue(all(not message.get("tool_calls") for message in history))
        self.assertTrue(all("tool_call_id" not in message for message in history))
        self.assertEqual(history[3]["role"], "user")
        observations = json.loads(history[3]["content"])
        self.assertEqual(observations["origin"], "validated_text_plan_adapter")
        self.assertEqual(observations["runtime_tool_observations"][0]["id"], "call_1")
        self.assertTrue(observations["runtime_tool_observations"][0]["result"]["ok"])
        self.assertEqual(self.trajectory()["messages"][2], original)
        self.assertEqual(set(json.loads(self.trajectory()["normalized_final"])),
                         {"hypotheses", "recommendations", "missing_evidence", "process_checks"})

    def test_real_swarm_preface_and_six_process_checks_normalize_without_retry(self):
        context = self.context(workflow="swarm")
        for name, role in [("swarm_diagnosis", "diagnosis"), ("swarm_triage", "triage")]:
            with self.subTest(response=name):
                original = captured(name)
                client = Scripted([original])
                result = self.loop(client).run(role, "并行排查连接池异常", context)
                self.assertTrue(result.complete, result.warnings)
                self.assertEqual(result.model_steps, 1)
                self.assertEqual(result.format_repair_count, 0)
                self.assertEqual(len(client.requests), 1)
                trajectory = self.trajectory()
                self.assertEqual(trajectory["messages"][-1], original)
                normalized = json.loads(trajectory["normalized_final"])
                if name == "swarm_diagnosis":
                    self.assertTrue(any(event["code"] == "plain_prose_removed"
                                        for event in result.protocol_events))
                    self.assertTrue(trajectory["normalized_final"].startswith("{"))
                else:
                    self.assertEqual(len(json.loads(original["content"])["process_checks"]), 6)
                    self.assertEqual(len(normalized["process_checks"]), 5)
                    self.assertTrue(any(event["code"] == "top_level_array_limited"
                                        and event["path"] == "process_checks" and event["dropped_count"] == 1
                                        for event in result.protocol_events))

    def test_invalid_plan_or_report_gets_at_most_one_tool_free_json_repair(self):
        context = self.context()
        invalid_responses = [
            text_plan([{"id": "invalid", "name": "query_metrics", "args": "not-an-object"}]),
            {"role": "assistant", "content": "这不是最终JSON。"},
        ]
        for invalid in invalid_responses:
            with self.subTest(content=invalid["content"]):
                client = Scripted([invalid, invalid, final()])
                with patch.object(self.registry, "execute", wraps=self.registry.execute) as execute:
                    result = self.loop(client).run("diagnosis", "检查异常", context)
                self.assertFalse(result.complete)
                self.assertEqual(result.format_repair_count, 1)
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(len(client.messages), 1)
                self.assertEqual(result.tool_calls, 0)
                self.assertEqual(execute.call_count, 0)
                repair = client.requests[1][1]
                self.assertIsNone(repair["tools"])
                self.assertEqual(repair["tool_choice"], "none")
                self.assertEqual(repair["response_format"], {"type": "json_object"})

    def test_invalid_report_can_repair_and_honors_json_mode_support(self):
        context = self.context()
        cases = ((mode, raw) for mode in (True, False)
                 for raw in ("非JSON报告", final()["content"][:-1]))
        for json_mode, malformed in cases:
            with self.subTest(json_mode=json_mode, malformed=malformed):
                client = Scripted([{"role": "assistant", "content": malformed}, final()], json_mode=json_mode)
                result = self.loop(client).run("diagnosis", "检查异常", context)
                self.assertTrue(result.complete, result.warnings)
                self.assertEqual(result.format_repair_count, 1)
                repair = client.requests[1][1]
                self.assertIsNone(repair["tools"])
                self.assertEqual(repair["tool_choice"], "none")
                if json_mode:
                    self.assertEqual(repair["response_format"], {"type": "json_object"})
                else:
                    self.assertNotIn("response_format", repair)

    def test_swarm_diagnosis_rejects_text_plan_assessment_and_postmortem(self):
        context = self.context(workflow="swarm")
        plan = text_plan([
            {"id": "assess-1", "name": "assess_remediation", "args": {}},
            {"id": "draft-1", "name": "build_postmortem", "args": {}},
        ])
        client = Scripted([plan, final()])
        with patch.object(self.registry, "execute", wraps=self.registry.execute) as execute:
            result = self.loop(client).run("diagnosis", "诊断仅提供证据与候选机制", context)
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(len(result.tool_records), 2)
        self.assertTrue(all(not record["ok"] for record in result.tool_records))
        advertised = {tool["function"]["name"] for tool in client.requests[0][1]["tools"]}
        self.assertNotIn("assess_remediation", advertised)
        self.assertNotIn("build_postmortem", advertised)
        observations = json.loads(client.requests[1][0][-1]["content"])["runtime_tool_observations"]
        self.assertTrue(all(not item["result"]["ok"] for item in observations))
        self.assertTrue(all("not allowed" in item["result"]["error"] for item in observations))

    def test_force_final_text_plan_is_not_executed(self):
        context = self.context()
        self.harness.max_steps = 1
        client = Scripted([captured("single_diagnosis")])
        with patch.object(self.registry, "execute", wraps=self.registry.execute) as execute:
            result = self.loop(client).run("diagnosis", "只生成最终结论", context)
        self.assertFalse(result.complete)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(execute.call_count, 0)
        self.assertIsNone(client.requests[0][1]["tools"])
        self.assertTrue(any("最终答复步骤返回文本工具计划" in warning for warning in result.warnings))

    def test_text_plan_over_budget_rejects_entire_batch(self):
        context = self.context()
        self.harness.max_tool_calls = 1
        client = Scripted([text_plan([
            {"id": "metric-1", "name": "query_metrics", "args": {}},
            {"id": "log-1", "name": "search_logs", "args": {}},
        ]), final()])
        with patch.object(self.registry, "execute", wraps=self.registry.execute) as execute:
            result = self.loop(client).run("diagnosis", "检查异常", context)
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.tool_records, [])
        self.assertEqual(result.format_repair_count, 1)
        self.assertTrue(any(event["code"] == "format_repair_requested" and event["invalid_plan"]
                            for event in result.protocol_events))

    def test_duplicate_text_plan_ids_reject_entire_batch(self):
        context = self.context()
        client = Scripted([text_plan([
            {"id": "same", "name": "query_metrics", "args": {}},
            {"id": "same", "name": "search_logs", "args": {}},
        ]), final()])
        with patch.object(self.registry, "execute", wraps=self.registry.execute) as execute:
            result = self.loop(client).run("diagnosis", "检查异常", context)
        self.assertEqual(execute.call_count, 0)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.format_repair_count, 1)
        self.assertFalse(any(event["code"] == "text_tool_plan" for event in result.protocol_events))


if __name__ == "__main__":
    unittest.main()
