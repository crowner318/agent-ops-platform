from concurrent.futures import Future
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.service import MAX_REPORT, WorkbenchService


class FakeService(WorkbenchService):
    def __init__(self, config):
        self.calls = []
        self.gate = None
        super().__init__(config)

    def _config_probe(self, agent):
        return {"name": agent, "available": True, "api_key_configured": False, "model": "fake", "config_message": "offline"}

    def _run_cli(self, task_id, command, cwd):
        self.calls.append((command, cwd))
        if self.gate is not None:
            self.gate.wait(3)
        def arg(name):
            return next(value.split("=", 1)[1] for value in command if value.startswith(name + "="))
        if cwd == self.ops_project:
            root = self.ops_runtime / "reports"
            root.mkdir(parents=True, exist_ok=True)
            path = root / (arg("--session") + "-fake.json")
            data = {"session_id": task_id, "answer": "fake report", "complete": True, "demo": "--demo" in command,
                    "api_key": "must-never-return", "reasoning_content": "private reasoning", "execution": {"max_tokens": 123}}
        else:
            review_id = arg("--review-id")
            root = self.release_runtime / "reports"
            root.mkdir(parents=True, exist_ok=True)
            path = root / (review_id + ".json")
            data = {"review_id": review_id, "change_id": "risk-pool", "status": "blocked" if "resume" in command else "waiting_human",
                    "review_digest": "a" * 64, "metadata": {"mode": "demo"}}
            self.release_runtime.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.release_runtime / "reviews.sqlite3")) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS reviews(review_id TEXT PRIMARY KEY,change_id TEXT,status TEXT,result TEXT)")
                connection.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?)", (review_id, "risk-pool", data["status"], json.dumps(data)))
                connection.commit()
        path.write_text(json.dumps(data), encoding="utf-8")
        path.with_suffix(".md").write_text("# fake\nBearer fake-token password=private-value", encoding="utf-8")
        self._append_logs(task_id, ["Bearer fake-token", "api_key=unit-only-key", "done"])
        return 2 if data.get("status") == "blocked" else 0


class HoldPool:
    def __init__(self): self.jobs = []
    def submit(self, *args): self.jobs.append(args); return Future()
    def shutdown(self, **kwargs): pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[3] / "work" / ("workbench-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.ops, self.release = self.root / "ops", self.root / "release"
        for project in (self.ops, self.release):
            project.mkdir()
            (project / "run.py").write_text("# fake worker only", encoding="utf-8")
        (self.release / "data").mkdir()
        groups = {kind: [{"id": kind, "timestamp": "2026-10-30T09:55:00+08:00", "synthetic": True}]
                  for kind in ("release_snapshot", "config_diff", "code_diff", "ci_checks", "dependency_budget")}
        (self.release / "data/fixtures.json").write_text(json.dumps({"changes": {"risk-pool": {"scope": {"service": "order-service", "environment": "staging"}, "evidence": groups}}}), encoding="utf-8")
        self.config = {"ops_project": self.ops, "release_project": self.release, "python_executable": sys.executable,
                       "runtime_dir": self.root / "runtime", "release_runtime_dir": self.root / "isolated-release-runtime"}
        self.service = FakeService(self.config)

    def tearDown(self):
        self.service.close()
        time.sleep(.05)
        self.assertEqual(self.root.parent, self.root.resolve().parent)
        shutil.rmtree(self.root)

    def wait(self, task_id):
        for _ in range(150):
            result = self.service.get_task(task_id)
            if result["status"] not in {"queued", "running"}:
                return result
            time.sleep(.02)
        self.fail("mock worker did not finish")

    def test_ops_fixed_command_results_redaction_and_download(self):
        summary = self.service.create_task({"agent": "ops", "mode": "demo", "params": {"question": "--not-a-shell; $(echo hi)", "workflow": "single"}})
        task = self.wait(summary["id"])
        self.assertEqual("completed", task["status"])
        command, cwd = self.service.calls[0]
        self.assertEqual(self.ops, cwd)
        self.assertEqual(str(Path(sys.executable).resolve()), command[0])
        self.assertIn("--question=--not-a-shell; $(echo hi)", command)
        self.assertIn("--runtime-dir=" + str(self.service.ops_runtime), command)
        self.assertNotIn("must-never-return", json.dumps(task))
        self.assertNotIn("private reasoning", json.dumps(task))
        self.assertEqual(123, task["report"]["data"]["execution"]["max_tokens"])
        name, raw = self.service.get_report(task["id"], "json")
        self.assertTrue(name.endswith(".json"))
        self.assertEqual("[REDACTED]", json.loads(raw)["api_key"])
        self.assertNotIn("fake-token", self.service.get_report(task["id"], "md")[1].decode())
        self.assertEqual(1, len(self.service.list_tasks()))

    def test_release_explicit_review_digest_and_blocked_mapping(self):
        summary = self.service.create_task({"agent": "release", "mode": "demo", "params": {"change_id": "risk-pool"}})
        task = self.wait(summary["id"])
        self.assertEqual("waiting_human", task["status"])
        self.assertEqual(1, len(self.service.calls))
        payload = {"reviewer": "Tester", "decision": "acknowledge", "comment": "Read only", "review_digest": "a" * 64}
        with self.assertRaises(RuntimeError):
            self.service.submit_review(task["id"], dict(payload, review_digest="b" * 64))
        with self.assertRaises(ValueError):
            self.service.submit_review(task["id"], dict(payload, decision="approve"))
        self.service.submit_review(task["id"], payload)
        reviewed = self.wait(task["id"])
        self.assertEqual("blocked", reviewed["status"])
        command = self.service.calls[-1][0]
        self.assertIn("resume", command)
        self.assertIn("--review-id=" + task["review_id"], command)
        self.assertNotIn("--demo", command)
        self.assertNotIn("approved", reviewed["status"])

    def test_review_digest_rechecked_before_queued_worker(self):
        task = self.wait(self.service.create_task({"agent": "release", "mode": "demo", "params": {"change_id": "risk-pool"}})["id"])
        self.service._pool.shutdown(wait=True)
        hold = HoldPool(); self.service._pool = hold
        self.service.submit_review(task["id"], {"reviewer": "T", "decision": "reject", "comment": "", "review_digest": "a" * 64})
        row = self.service._row(task["id"])
        path = Path(row["report_path"])
        data = json.loads(path.read_text()); data["review_digest"] = "b" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        function, task_id, action = hold.jobs[0]
        function(task_id, action)
        self.assertEqual("failed", self.service._row(task_id)["status"])
        self.assertEqual(1, len(self.service.calls))

    def test_payload_rejects_paths_commands_and_invalid_modes(self):
        invalid = [
            {"agent": "ops", "mode": "demo", "params": {"runtime_dir": "/tmp"}},
            {"agent": "ops", "mode": "api", "params": {"python_executable": "bad"}},
            {"agent": "release", "mode": "demo", "params": {"change_id": "risk-pool", "redis_url": "bad"}},
            {"agent": "release", "mode": "demo", "params": {"change_id": "../safe"}},
            {"agent": "ops", "mode": [], "params": {}},
            {"agent": "ops", "mode": "demo", "params": {"workflow": []}},
            {"agent": "ops", "mode": "demo", "params": {"start": "2026-10-19T10:00:00"}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): self.service.create_task(value)
        self.assertEqual([], self.service.calls)

    def test_history_readonly_stable_ids_and_path_escape_rejected(self):
        root = self.ops / "runtime/reports"; root.mkdir(parents=True)
        path = root / "history.json"
        original = {"session_id": "old", "answer": "historic", "complete": False, "demo": True}
        path.write_text(json.dumps(original), encoding="utf-8")
        first = next(t for t in self.service.list_tasks() if t["historical"])
        second = next(t for t in self.service.list_tasks() if t["historical"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(original, json.loads(path.read_text()))
        outside = self.root / "outside.json"; outside.write_text('{}')
        for escape in (outside, root / "../outside.json", root / "..\\outside.json"):
            self.service._update(first["id"], report_path=str(escape))
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.service.get_report(first["id"], "json")
        link = root / "escaped.json"
        try: link.symlink_to(outside)
        except (OSError, NotImplementedError): pass
        else:
            self.service._update(first["id"], report_path=str(link))
            with self.assertRaises(ValueError): self.service.get_report(first["id"], "json")

    def test_queue_limit_restart_interrupted_no_replay_and_log_bound(self):
        self.service._pool.shutdown(wait=True)
        self.service._pool = HoldPool()
        for _ in range(20): self.service.create_task({"agent": "ops", "mode": "api", "params": {}})
        with self.assertRaises(RuntimeError): self.service.create_task({"agent": "ops", "mode": "api", "params": {}})
        task_id = self.service.list_tasks()[0]["id"]
        self.service._append_logs(task_id, ["敏感但无密钥的文字" * 200 for _ in range(1000)])
        self.assertLessEqual(len(self.service._row(task_id)["logs"].encode()), 65536)
        self.service.close()
        self.service = FakeService(self.config)
        self.assertTrue(all(t["status"] == "interrupted" for t in self.service.list_tasks()))
        self.assertEqual([], self.service.calls)

    def test_bootstrap_probe_cache_and_actual_popen_arguments(self):
        with patch.object(self.service, "_config_probe", wraps=self.service._config_probe) as probe:
            self.assertFalse(self.service.bootstrap()["agents"]["ops"]["api_key_configured"])
            self.service.bootstrap()
            self.assertEqual(2, probe.call_count)
        with self.assertRaises(KeyError): self.service.get_task("not-present")
        for task_id in ("../secret", "a\\b", "C:\\secret"):
            with self.assertRaises(ValueError): self.service.get_task(task_id)
        # The real subprocess layer is checked independently of the fake worker.
        with patch("workbench.service.subprocess.Popen", side_effect=OSError("test stop")) as popen:
            with self.assertRaises(OSError): WorkbenchService._run_cli(self.service, "none", [sys.executable, "-V"], self.ops)
            self.assertIs(popen.call_args.kwargs["shell"], False)


if __name__ == "__main__": unittest.main()
