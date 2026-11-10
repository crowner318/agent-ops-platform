from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import yaml
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        # Python3.14 Windows下不使用TemporaryDirectory的mode=0700 ACL。
        self.temp = Path(tempfile.gettempdir()) / ("it_ops_integration_" + uuid4().hex)
        self.temp.mkdir()

    def tearDown(self):
        # 只删除本测试创建且已验证在独立目录中的文件，不跟随符号链接。
        root = self.temp.resolve()
        self.assertTrue(root.name.startswith("it_ops_integration_"))
        for path in sorted(self.temp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.temp.rmdir()

    def command(self, *args):
        env = dict(os.environ, DEEPSEEK_API_KEY="", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(ROOT / "run.py"), "--runtime-dir", str(self.temp),
                               "--quiet", *args], cwd=ROOT, env=env, text=True, encoding="utf-8",
                              capture_output=True, timeout=30)

    def latest(self):
        files = sorted((self.temp / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        return json.loads(files[-1].read_text(encoding="utf-8"))

    def test_swarm_followup_and_demo_training_exclusion(self):
        first = self.command("--demo", "--mode", "swarm", "--session", "integration", "-q", "订单服务故障，请生成复盘")
        self.assertEqual(first.returncode, 0, first.stderr)
        report = self.latest()
        self.assertEqual(report["mode"], "swarm")
        self.assertEqual(len(report["agents"]), 3)
        self.assertTrue(report["complete"])
        self.assertEqual(report["execution"]["model_steps"], 5)
        self.assertEqual(report["execution"]["cache"]["backend_executions"], 7)
        self.assertFalse(report["execution"]["usage_complete"])
        self.assertIsNone(report["execution"]["usage"]["total_tokens"])
        self.assertTrue(all(row.get("synthetic") for row in report["evidence"]))
        second = self.command("--demo", "--session", "integration", "--mode", "single", "-q", "继续分析")
        self.assertEqual(second.returncode, 0, second.stderr)
        with closing(sqlite3.connect(self.temp / "memory.sqlite3")) as connection:
            raw = connection.execute("SELECT payload FROM events WHERE kind='trajectory' ORDER BY id DESC LIMIT 1").fetchone()[0]
        seed = json.loads(json.loads(raw)["messages"][1]["content"])
        self.assertIn("订单服务故障", seed["context"]["l1_recent"])
        confirm = self.command("--confirm-case", report["case_id"])
        self.assertEqual(confirm.returncode, 0, confirm.stderr)
        export_path = self.temp / "sft.jsonl"
        exported = self.command("--export-sft", str(export_path))
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(export_path.read_text(encoding="utf-8"), "")

    def test_healthy_service_does_not_invent_root_cause(self):
        result = self.command("--demo", "--service", "payment-service", "--mode", "single")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.latest()
        self.assertEqual(report["agents"][0]["analysis"]["hypotheses"], [])
        self.assertTrue(all(e["service"] == "payment-service" for e in report["evidence"]))

    def test_empty_window_returns_incomplete_without_invented_evidence(self):
        result = self.command("--demo", "--start", "2027-01-01T00:00:00+08:00",
                              "--end", "2027-01-01T01:00:00+08:00")
        self.assertEqual(result.returncode, 2, result.stderr)
        report = self.latest()
        self.assertFalse(report["complete"])
        self.assertFalse(report["evidence"])
        self.assertEqual(report["execution"]["model_steps"], 0)

    def test_blank_key_fails_locally_without_request(self):
        result = self.command()
        self.assertEqual(result.returncode, 1)
        self.assertIn("DEEPSEEK_API_KEY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_simple_auto_route_and_compact_summary(self):
        result = self.command("--demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.latest()
        self.assertEqual(report["mode"], "single")
        self.assertEqual(report["execution"]["model_steps"], 3)
        self.assertFalse(report["quality_issues"])
        self.assertIn("2026-10-19T10:15:00+08:00", report["answer"])

    def test_invalid_scope_fails_before_tools(self):
        result = self.command("--demo", "--service", "out-of-scope-service")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.temp / "reports").exists())

    def test_configuration_check_is_offline_and_does_not_print_key(self):
        fake_key = "offline-configuration-test-key"
        env = dict(os.environ, DEEPSEEK_API_KEY=fake_key, DEEPSEEK_MODEL="qwen3.8-flash",
                   DEEPSEEK_BASE_URL="ws-example.cn-beijing.maas.aliyuncs.com", API_PROVIDER="auto",
                   PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--check-config"],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["provider"], "qwen")
        self.assertIs(data["network_request_sent"], False)
        self.assertNotIn(fake_key, result.stdout + result.stderr)

    def test_prefetch_obeys_policy_and_output_budget_before_model(self):
        original = yaml.safe_load((ROOT / "harness.yaml").read_text(encoding="utf-8"))
        for variant in ("denied", "too_large"):
            with self.subTest(variant=variant):
                policy = json.loads(json.dumps(original))
                if variant == "denied":
                    policy["roles"]["diagnosis"].remove("collect_evidence")
                else:
                    policy["limits"]["max_output_chars"] = 100
                policy_path = self.temp / (variant + ".yaml")
                policy_path.write_text(yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
                result = self.command("--demo", "--harness", str(policy_path))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse((self.temp / "reports").exists())


if __name__ == "__main__":
    unittest.main()
