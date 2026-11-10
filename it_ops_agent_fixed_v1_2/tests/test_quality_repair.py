"""One shared, budgeted correction opportunity; the final guard stays strict."""
import json
from pathlib import Path
import unittest

from ops_agent.engine import AgentLoop
from ops_agent.harness import Harness
from ops_agent.skills import SkillRegistry
from tests.test_real_protocol_engine import Memory, Scripted, final

ROOT = Path(__file__).resolve().parents[1]


def process_assertion():
    message = final()
    body = json.loads(message["content"])
    body["recommendations"] = [{"text": "配置未经容量评估即随发布上线。", "evidence_ids": ["c-order-001"]}]
    message["content"] = json.dumps(body, ensure_ascii=False)
    return message


class QualityRepairTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(ROOT / "harness.yaml")
        self.registry = SkillRegistry(ROOT / "data", "order-service", "demo",
            "2026-10-19T09:55:00+08:00", "2026-10-19T10:20:00+08:00")
        self.registry.execute("collect_evidence", {"kinds": ["metrics", "logs", "traces", "changes"]})
        self.memory = Memory()

    def run_client(self, client):
        loop = AgentLoop(client, self.registry, self.harness, self.memory, "quality-test", "order-service", "demo")
        return loop.run("triage", "研判异常", {"workflow": "swarm", "evidence": self.registry.observed_evidence()})

    def test_quality_feedback_rechecks_revised_output_without_tools(self):
        original = process_assertion()
        client = Scripted([original, final()])
        result = self.run_client(client)
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(result.quality_repair_count, 1)
        self.assertEqual(result.format_repair_count, 0)
        self.assertFalse(result.quality_issues)
        self.assertEqual(client.requests[1][0][2], original)
        feedback = client.requests[1][0][-1]["content"]
        self.assertIn("unverified_process_cause", feedback)
        self.assertIn("recommendations[0].text", feedback)
        self.assertIsNone(client.requests[1][1]["tools"])
        self.assertEqual(client.requests[1][1]["response_format"], {"type": "json_object"})
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.protocol_events[0]["code"], "quality_repair_requested")

    def test_quality_correction_does_not_accept_fabricated_references(self):
        invalid = final()
        body = json.loads(invalid["content"])
        body["hypotheses"][0]["evidence_ids"] = ["fabricated-001"]
        invalid["content"] = json.dumps(body)
        client = Scripted([process_assertion(), invalid, final()])
        result = self.run_client(client)
        self.assertFalse(result.complete)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result.quality_repair_count, 1)
        self.assertTrue(any(issue["code"] == "unknown_evidence_ids" for issue in result.quality_issues))
        self.assertEqual(len(client.messages), 1)

    def test_format_and_quality_corrections_share_one_opportunity(self):
        client = Scripted([{"role": "assistant", "content": "not JSON"}, process_assertion(), final()])
        result = self.run_client(client)
        self.assertFalse(result.complete)
        self.assertEqual(result.format_repair_count, 1)
        self.assertEqual(result.quality_repair_count, 0)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(len(client.messages), 1)

    def test_quality_feedback_cannot_exceed_model_step_budget(self):
        self.harness.max_steps = 1
        client = Scripted([process_assertion(), final()])
        result = self.run_client(client)
        self.assertFalse(result.complete)
        self.assertEqual(result.model_steps, 1)
        self.assertEqual(result.quality_repair_count, 0)
        self.assertEqual(len(client.messages), 1)


if __name__ == "__main__":
    unittest.main()
