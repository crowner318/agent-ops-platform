"""Locally supplied sources may use custom scopes without becoming trusted data."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from langgraph.types import Command

from release_guard.analysis import evidence_scope_issues
from release_guard.config import Settings
from release_guard.evidence import EvidenceStore, tool_names
from release_guard.reporting import _source_notice, save_report
from release_guard.workflow import build_workflow

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "data/demo_complete/fixtures.json").read_text(encoding="utf-8"))


def local_payload(source="safe"):
    change = deepcopy(BASE["changes"][source])
    change_id = "pay-release-001"
    change["scope"].update(change_id=change_id, service="payment-service", environment="production", snapshot_id="user-local-001")
    for kind, rows in change["evidence"].items():
        for row in rows:
            row.update(change_id=change_id, service="payment-service", environment="production", synthetic=False)
    return {"snapshot_id": "user-local-001", "changes": {change_id: change}}


class LocalFileFixture:
    def setUpFiles(self):
        self.base = (ROOT / "tests/.local_sources_tmp").resolve()
        self.base.mkdir(exist_ok=True)
        self.root = self.base / uuid4().hex
        self.root.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.write(local_payload())

    def tearDownFiles(self):
        resolved = self.root.resolve()
        if resolved != self.base and resolved.is_relative_to(self.base):
            shutil.rmtree(resolved)

    def write(self, payload):
        (self.data / "fixtures.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class LocalSourceTests(LocalFileFixture, unittest.TestCase):
    def setUp(self):
        self.setUpFiles()

    def tearDown(self):
        self.tearDownFiles()

    def test_all_tools_accept_custom_local_scope_and_preserve_false_marker(self):
        store = EvidenceStore(self.data)
        for name in tool_names:
            arguments = {"change_id": "pay-release-001"}
            if name.startswith("search_"):
                arguments["query"] = "pool connection 发布 容量"
            result = store.execute(name, arguments)
            self.assertTrue(result["ok"], result)
            self.assertEqual("production", result["scope"]["environment"])
            self.assertEqual("payment-service", result["scope"]["service"])
            self.assertTrue(result["evidence"], name)
            self.assertTrue(all(row["synthetic"] is False for row in result["evidence"]))
            issues = evidence_scope_issues(result["evidence"], result["scope"])
            self.assertEqual(["no_current_evidence"] if name.startswith("search_") else [], [item["code"] for item in issues])
        self.assertFalse(store.execute("deploy", {"change_id": "pay-release-001"})["ok"])
        self.assertFalse(store.execute("get_config_diff", {"change_id": "pay-release-001", "environment": "demo"})["ok"])

    def test_custom_identifiers_are_bounded_and_cannot_be_paths_or_commands(self):
        for key in ("service", "environment", "change_id"):
            for value in ("../secrets", "prod;whoami", "https://example.com", "prod\n", "a" * 81, 12):
                with self.subTest(key=key, value=value):
                    payload = local_payload()
                    payload["changes"]["pay-release-001"]["scope"][key] = value
                    self.write(payload)
                    with self.assertRaises(ValueError):
                        EvidenceStore(self.data)

    def test_local_source_marker_must_be_boolean_and_json_numbers_finite(self):
        for flag in (None, "false", 0, 1):
            payload = local_payload()
            payload["changes"]["pay-release-001"]["evidence"]["release_snapshot"][0]["synthetic"] = flag
            self.write(payload)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                EvidenceStore(self.data)
        payload = local_payload()
        payload["changes"]["pay-release-001"]["evidence"]["dependency_budget"][0]["content"]["available_connections"] = float("nan")
        self.write(payload)
        with self.assertRaisesRegex(ValueError, "finite"):
            EvidenceStore(self.data)
        raw = json.dumps(local_payload()).replace('"available_connections": 100', '"available_connections": 1e309')
        # Use an existing finite field to ensure the JSON exponent branch is exercised.
        raw = raw.replace('"pool_after": 50', '"pool_after": 1e309')
        self.assertIn("1e309", raw)
        (self.data / "fixtures.json").write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "finite"):
            EvidenceStore(self.data)

    def test_knowledge_filters_are_retained_for_custom_sources(self):
        payload = local_payload()
        groups = payload["changes"]["pay-release-001"]["evidence"]
        original = groups["runbook"][0]
        for suffix in ("expired", "other-service", "other-env"):
            row = deepcopy(original)
            row["id"] += suffix
            if suffix == "expired":
                row["content"]["valid_until"] = "2020-01-01T00:00:00+08:00"
            elif suffix == "other-service":
                row["service"] = "other-service"
            else:
                row["environment"] = "staging"
            groups["runbook"].append(row)
        row = deepcopy(groups["incident_case"][0])
        row["id"] += "unverified"
        row["content"]["verified"] = False
        groups["incident_case"].append(row)
        self.write(payload)
        store = EvidenceStore(self.data)
        self.assertEqual(1, len(store.execute("search_runbooks", {"change_id": "pay-release-001", "query": "pool 发布"})["evidence"]))
        self.assertEqual(1, len(store.execute("search_incident_cases", {"change_id": "pay-release-001", "query": "pool 发布"})["evidence"]))

    def test_markdown_discloses_unverified_local_origin_independently_of_model(self):
        change = local_payload()["changes"]["pay-release-001"]
        evidence = [row for group in change["evidence"].values() for row in group]
        state = {"change_id": "pay-release-001", "scope": change["scope"], "evidence": evidence, "status": "waiting_human"}
        for mode in ("demo", "api"):
            markdown, document = save_report("local-" + mode, state, self.root / "reports-output", {"mode": mode})
            text = markdown.read_text(encoding="utf-8")
            self.assertIn("用户提供的本地记录", text)
            self.assertIn("尚未独立核实", text)
            self.assertIn("未证明已连接生产系统", text)
            self.assertNotIn("模拟材料标记synthetic=true", text)
            self.assertEqual(evidence, json.loads(document.read_text(encoding="utf-8"))["evidence"])
            self.assertIn("固定脚本" if mode == "demo" else "API调用", text)
        evidence[0]["synthetic"] = True
        self.assertIn("其中synthetic=true的记录仍是模拟资料", _source_notice(state, "demo"))


class LocalSourceWorkflowTests(LocalFileFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setUpFiles()

    async def asyncTearDown(self):
        self.tearDownFiles()

    async def test_real_mcp_workflow_for_local_payment_production_material_preserves_human_and_rules(self):
        with patch("release_guard.analysis.ChatOpenAI", side_effect=AssertionError("offline must not construct API model")):
            for source, expected in (("safe", "review_acknowledged"), ("risk-pool", "blocked"), ("missing", "incomplete")):
                with self.subTest(source=source):
                    self.write(local_payload(source))
                    config = {"configurable": {"thread_id": "local-" + source}}
                    async with build_workflow(Settings(), self.root / source, self.data, demo=True) as graph:
                        paused = await graph.ainvoke({"change_id": "pay-release-001"}, config)
                        self.assertTrue(paused.get("__interrupt__"))
                        self.assertEqual(7, len(paused["tool_records"]))
                        self.assertTrue(all(row["ok"] for row in paused["tool_records"]))
                        self.assertEqual("production", paused["scope"]["environment"])
                        self.assertTrue(all(row["synthetic"] is False for row in paused["evidence"]))
                        self.assertFalse(paused["deployment_approved"])
                        self.assertTrue(all(call["attempts"] == 0 for call in paused["model_calls"]))
                        if source == "missing":
                            self.assertTrue(paused["quality_issues"])
                            self.assertIsNone(paused.get("analysis"))
                        else:
                            self.assertFalse(paused["quality_issues"])
                        resumed = await graph.ainvoke(Command(resume={"reviewer": "local-test-reviewer", "decision": "acknowledge",
                            "comment": "用户本地资料流程测试，不是生产审批。", "proposed_digest": paused["review_digest"]}), config)
                        self.assertEqual(expected, resumed["status"])
                        self.assertFalse(resumed["deployment_approved"])
                        self.assertFalse(resumed["review_record"]["production_approval"])


if __name__ == "__main__":
    unittest.main()
