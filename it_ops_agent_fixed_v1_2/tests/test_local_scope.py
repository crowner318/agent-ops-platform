"""Explicit local input binds exactly one scope without widening Agent tools."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from ops_agent.harness import Harness
from run import _local_input_harness, main, parser

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-11-06T10:00:00+08:00"


class LocalScopeTests(unittest.TestCase):
    def setUp(self):
        self.parent = (ROOT / "tests/.local_scope_tmp").resolve()
        self.parent.mkdir(exist_ok=True)
        self.root = self.parent / uuid4().hex
        self.root.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        base = {"service": "billing-api", "environment": "prod-eu", "timestamp": AT, "synthetic": False}
        rows = {"metrics": [dict(base, id="metric-1", metric="request_error_rate", value=0.001, unit="ratio")],
                "logs": [dict(base, id="log-1", level="INFO", message="One user-provided local request sample")],
                "traces": [dict(base, id="trace-1", operation="GET /billing", status="ok", duration_ms=40, spans=[])],
                "changes": [dict(base, id="change-1", change_type="configuration", description="Provided local change snapshot")], "runbooks": []}
        (self.data / "demo_data.json").write_text(json.dumps(rows), encoding="utf-8")

    def tearDown(self):
        resolved = self.root.resolve()
        if resolved != self.parent and resolved.is_relative_to(self.parent):
            shutil.rmtree(resolved)

    def arguments(self):
        return ["--demo", "--quiet", "--mode", "single", "--data-dir", str(self.data), "--runtime-dir", str(self.root / "runtime"),
                "--service", "billing-api", "--environment", "prod-eu", "--session", "local-test",
                "--start", "2026-11-06T09:55:00+08:00", "--end", "2026-11-06T10:20:00+08:00", "--question", "检查当前提供的本地资料，列出能确认的观测和缺口。"]

    def test_default_cli_keeps_original_scope_rejection(self):
        self.assertFalse(parser().parse_args([]).local_input)
        with redirect_stderr(StringIO()) as error, redirect_stdout(StringIO()):
            result = main(self.arguments())
        self.assertEqual(1, result)
        self.assertIn("outside the configured scope", error.getvalue())
        self.assertFalse((self.root / "runtime/reports").exists())

    def test_local_copy_binds_exact_scope_and_keeps_tools_budget_and_risk(self):
        original = Harness(ROOT / "harness.yaml")
        local = _local_input_harness(original, "billing-api", "prod-eu")
        local.validate_scope("billing-api", "prod-eu")
        original.validate_scope("order-service", "production")
        with self.assertRaises(ValueError):
            original.validate_scope("billing-api", "prod-eu")
        for service, environment in (("other", "prod-eu"), ("billing-api", "production"), ("order-service", "demo")):
            with self.assertRaises(ValueError):
                local.validate_scope(service, environment)
        for role in ("triage", "diagnosis", "remediation"):
            self.assertEqual(original.allowed_tools(role), local.allowed_tools(role))
        for name in ("max_steps", "max_tool_calls", "max_seconds", "max_output_chars", "l0"):
            self.assertEqual(getattr(original, name), getattr(local, name))
        for name in ("restart_service", "rollback_release", "deploy"):
            with self.assertRaises(ValueError):
                local.validate_tool("diagnosis", name, {})
        with self.assertRaises(ValueError):
            local.validate_tool("triage", "query_metrics", {"service": "order-service"})

    def test_invalid_local_scope_cannot_be_paths_urls_or_commands(self):
        harness = Harness(ROOT / "harness.yaml")
        for value in ("../escape", "prod;whoami", "prod\n", "https://example.com", "a" * 81, "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _local_input_harness(harness, value, "prod-eu")
            with self.subTest(environment=value), self.assertRaises(ValueError):
                _local_input_harness(harness, "billing-api", value)

    def test_local_cli_produces_scoped_report_without_loading_model_configuration(self):
        with patch("run.load_client_config", side_effect=AssertionError("offline must not load secrets")), redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            result = main(self.arguments() + ["--local-input"])
        self.assertIn(result, (0, 2))
        reports = list((self.root / "runtime/reports").glob("*.json"))
        self.assertEqual(1, len(reports))
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual("billing-api", report["scope"]["service"])
        self.assertEqual("prod-eu", report["scope"]["environment"])
        self.assertTrue(all(row["service"] == "billing-api" and row["environment"] == "prod-eu" and row["synthetic"] is False
                            for row in report["evidence"]))
        self.assertEqual(0, report["execution"]["api_attempts"])
        self.assertIn("用户提供的本地材料", reports[0].with_suffix(".md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
