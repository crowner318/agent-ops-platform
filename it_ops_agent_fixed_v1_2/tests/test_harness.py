import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ops_agent.harness import Harness, REDACTED, redact
from ops_agent.prompts import system_prompt


ROOT = Path(__file__).resolve().parents[1]


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ROOT / "harness.yaml").read_text(encoding="utf-8"))
        self.harness = Harness(ROOT / "harness.yaml")

    def load(self, config):
        with patch.object(Path, "read_text", return_value=yaml.safe_dump(config, allow_unicode=True)):
            return Harness(ROOT / "harness.yaml")

    def test_scope_and_role_capabilities(self):
        self.harness.validate_scope("order-service", "production")
        for service, environment in [("foreign-service", "production"), ("order-service", "foreign")]:
            with self.assertRaises(ValueError):
                self.harness.validate_scope(service, environment)
        self.assertEqual(set(self.harness.allowed_tools("triage")), {"query_metrics", "search_logs", "collect_evidence"})
        self.assertEqual(len(self.harness.allowed_tools("diagnosis")), 8)
        self.assertEqual(set(self.harness.allowed_tools("remediation")), {"assess_remediation", "build_postmortem"})
        result = self.harness.allowed_tools("triage")
        result.clear()
        self.assertEqual(len(self.harness.allowed_tools("triage")), 3)

    def test_unknown_and_unauthorized_tools_are_rejected(self):
        for role, name in [("triage", "assess_remediation"), ("triage", "analyze_traces"),
                           ("triage", "search_runbooks"), ("remediation", "query_metrics"),
                           ("remediation", "collect_evidence"), ("remediation", "get_changes"),
                           ("remediation", "restart_service"), ("remediation", "rollback_release")]:
            with self.assertRaises(ValueError):
                self.harness.validate_tool(role, name, {})
        with self.assertRaises(ValueError):
            self.harness.allowed_tools("administrator")

    def test_yaml_cannot_enable_execution_or_expand_roles(self):
        for role, name in [("remediation", "restart_service"), ("triage", "assess_remediation"),
                           ("triage", "get_changes"), ("remediation", "collect_evidence")]:
            config = copy.deepcopy(self.config)
            config["roles"][role].append(name)
            with self.assertRaises(ValueError):
                self.load(config)

    def test_collect_permissions_are_checked_for_each_kind(self):
        self.harness.validate_tool("triage", "collect_evidence", {"kinds": ["metrics", "logs"]})
        self.harness.validate_tool("diagnosis", "collect_evidence", {"kinds": ["metrics", "logs", "traces", "changes", "runbooks"]})
        self.harness.validate_tool("diagnosis", "build_postmortem", {"summary": "draft", "evidence_ids": ["sample-id"]})
        for arguments in ({"kinds": ["metrics", "traces"]}, {"kinds": ["changes"]},
                          {"kinds": ["runbooks"]}, {"kinds": []}, {"kinds": ["metrics", "metrics"]},
                          {"kinds": ["shell"]}, {}, {"kinds": "metrics"}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.harness.validate_tool("triage", "collect_evidence", arguments)

    def test_single_workflow_keeps_default_full_diagnosis_permissions(self):
        self.assertEqual(self.harness.allowed_tools("diagnosis"), self.harness.allowed_tools("diagnosis", "single"))
        self.assertEqual(8, len(self.harness.allowed_tools("diagnosis", "single")))
        for name in ("assess_remediation", "build_postmortem"):
            self.harness.validate_tool("diagnosis", name, {}, "single")

    def test_swarm_diagnosis_cannot_assess_or_build_even_when_yaml_allows_both(self):
        config = copy.deepcopy(self.config)
        self.assertIn("assess_remediation", config["roles"]["diagnosis"])
        self.assertIn("build_postmortem", config["roles"]["diagnosis"])
        harness = self.load(config)
        self.assertEqual(6, len(harness.allowed_tools("diagnosis", "swarm")))
        for name in ("assess_remediation", "build_postmortem"):
            self.assertNotIn(name, harness.allowed_tools("diagnosis", "swarm"))
            with self.assertRaises(ValueError):
                harness.validate_tool("diagnosis", name, {}, workflow="swarm")
            harness.validate_tool("remediation", name, {}, workflow="swarm")
        harness.validate_tool("diagnosis", "collect_evidence", {"kinds": ["metrics", "logs", "traces", "changes", "runbooks"]}, "swarm")

    def test_workflow_does_not_expand_other_roles_or_undo_yaml_restrictions(self):
        for role in ("triage", "remediation"):
            self.assertEqual(self.harness.allowed_tools(role, "single"), self.harness.allowed_tools(role, "swarm"))
        with self.assertRaises(ValueError):
            self.harness.validate_tool("triage", "collect_evidence", {"kinds": ["traces"]}, "swarm")
        with self.assertRaises(ValueError):
            self.harness.validate_tool("remediation", "collect_evidence", {"kinds": ["metrics"]}, "swarm")
        config = copy.deepcopy(self.config)
        config["roles"]["diagnosis"] = ["query_metrics"]
        harness = self.load(config)
        self.assertEqual(["query_metrics"], harness.allowed_tools("diagnosis", "single"))
        self.assertEqual(["query_metrics"], harness.allowed_tools("diagnosis", "swarm"))

    def test_unknown_workflow_is_rejected_for_permissions_and_prompts(self):
        for workflow in ("auto", "", None, "SWARM"):
            with self.subTest(workflow=workflow):
                with self.assertRaises(ValueError):
                    self.harness.allowed_tools("diagnosis", workflow)
                with self.assertRaises(ValueError):
                    self.harness.validate_tool("diagnosis", "query_metrics", {}, workflow)
                with self.assertRaises(ValueError):
                    system_prompt("diagnosis", {}, workflow)

    def test_prompts_state_current_workflow_native_calls_and_valid_short_json(self):
        default = system_prompt("diagnosis", {})
        single = system_prompt("diagnosis", {}, "single")
        swarm = system_prompt("diagnosis", {}, "swarm")
        self.assertEqual(default, single)
        self.assertIn("[workflow:single]", single)
        self.assertIn("可调用assess_remediation和build_postmortem", single)
        self.assertIn("[workflow:swarm]", swarm)
        self.assertIn("禁止调用assess_remediation和build_postmortem", swarm)
        self.assertIn("API原生tool_calls", swarm)
        self.assertIn("process_checks最多5条", swarm)
        self.assertIn("不得在content", swarm)
        example_line = next(line for line in swarm.splitlines() if line.startswith('{"hypotheses":'))
        example = json.loads(example_line)
        self.assertEqual({"hypotheses", "recommendations", "missing_evidence", "process_checks"}, set(example))

    def test_swarm_triage_focuses_on_alerts_gaps_and_process_questions(self):
        prompt = system_prompt("triage", {}, "swarm")
        self.assertIn("Swarm分诊只负责告警研判、证据缺口及待核查问题", prompt)
        self.assertIn("recommendations必须为[]（空数组）", prompt)
        self.assertIn("处置建议统一由后续remediation角色负责", prompt)
        self.assertIn("流程问题只能放入process_checks", prompt)
        self.assertIn("以“是否”或“待核查”开头提问", prompt)
        self.assertIn("不得将“未经评估即上线的流程缺口”", prompt)
        self.assertIn("未提供审批、告警或评估材料不代表相应流程缺失", prompt)
        self.assertEqual({"query_metrics", "search_logs", "collect_evidence"},
                         set(self.harness.allowed_tools("triage", "swarm")))

    def test_single_triage_prompt_keeps_default_compatibility(self):
        default = system_prompt("triage", {})
        single = system_prompt("triage", {}, "single")
        self.assertEqual(default, single)
        self.assertIn("[workflow:single]", single)
        self.assertNotIn("Swarm分诊只负责", single)
        self.assertNotIn("recommendations必须为[]（空数组）", single)
        self.assertIn("流程问题只能放入process_checks", single)

    def test_config_schema_and_limits(self):
        changes = [
            lambda c: c.update({"extra": True}),
            lambda c: c.pop("scope"),
            lambda c: c["limits"].update({"max_steps": 0}),
            lambda c: c["limits"].update({"max_seconds": True}),
            lambda c: c["limits"].update({"max_tool_calls": 65}),
            lambda c: c["scope"].update({"allowed_services": []}),
            lambda c: c["scope"].update({"allow_all": True}),
            lambda c: c["roles"].update({"admin": ["query_metrics"]}),
            lambda c: c["l0"].update({"unknown": "x"}),
        ]
        for change in changes:
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                change(config)
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_aliases_are_rejected(self):
        with patch.object(Path, "read_text", return_value="a: &a [*a]\n"):
            with self.assertRaises(ValueError):
                Harness(ROOT / "harness.yaml")

    def test_duplicate_yaml_fields_are_rejected(self):
        with patch.object(Path, "read_text", return_value="schema_version: 1\nschema_version: 2\n"):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                Harness(ROOT / "harness.yaml")

    def test_payload_and_nested_scope_limits(self):
        self.harness.validate_tool("triage", "query_metrics", {"service": "order-service", "environment": "production"})
        invalid = [[], {"query": "x" * 16001}, {"query": float("nan")},
                   {"selector": {"service": "foreign-service"}}, {"environment": "foreign"}]
        nested = {}
        for _ in range(14):
            nested = {"nested": nested}
        invalid.append(nested)
        for payload in invalid:
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(ValueError):
                    self.harness.validate_tool("triage", "query_metrics", payload)

    def test_redaction_is_recursive_and_preserves_counters(self):
        value = {
            "api_key": "sample-key", "nested": [{"access_token": "private", "max_tokens": 2000}],
            "token_count": 20, "authorization": "Basic abc", "clientSecret": "private",
            "log": "Bearer abc.123 sk-123456789abcdef password=private hello",
        }
        cleaned = redact(value)
        self.assertEqual(cleaned["api_key"], REDACTED)
        self.assertEqual(cleaned["nested"][0]["access_token"], REDACTED)
        self.assertEqual(cleaned["nested"][0]["max_tokens"], 2000)
        self.assertEqual(cleaned["token_count"], 20)
        self.assertEqual(cleaned["clientSecret"], REDACTED)
        self.assertNotIn("private", cleaned["log"])
        self.assertNotIn("abc.123", cleaned["log"])
        self.assertNotIn("sk-123456789abcdef", cleaned["log"])
        self.assertIn("hello", cleaned["log"])
        self.assertEqual(value["api_key"], "sample-key")

    def test_json_string_redaction_preserves_parseable_tool_arguments(self):
        arguments = '{"api_key":"sk-abcd12345678","service":"order-service","max_tokens":2000}'
        cleaned = json.loads(redact(arguments))
        self.assertEqual(cleaned["api_key"], REDACTED)
        self.assertEqual(cleaned["service"], "order-service")
        self.assertEqual(cleaned["max_tokens"], 2000)

    def test_json_string_redaction_preserves_model_json_and_user_seed(self):
        seed = json.dumps({
            "question": '用户描述：password="private value"，接口失败',
            "evidence": [{"message": "Bearer abc.123", "token_count": 11}],
            "tool_arguments": '{"access_token":"private","query":"timeout"}',
        }, ensure_ascii=False)
        cleaned = json.loads(redact(seed))
        self.assertNotIn("private", cleaned["question"])
        self.assertNotIn("abc.123", cleaned["evidence"][0]["message"])
        self.assertEqual(cleaned["evidence"][0]["token_count"], 11)
        self.assertEqual(json.loads(cleaned["tool_arguments"])["access_token"], REDACTED)
        array = json.loads(redact('[{"password":"private"}]'))
        self.assertEqual(array[0]["password"], REDACTED)


if __name__ == "__main__":
    unittest.main()
