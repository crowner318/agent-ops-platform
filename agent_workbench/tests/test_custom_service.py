"""Custom inputs and recoverable report deletion through the service boundary."""
import json
from contextlib import closing
import sqlite3
import time
import unittest

import test_service as support


class CustomServiceTests(unittest.TestCase):
    setUp = support.ServiceTests.setUp
    tearDown = support.ServiceTests.tearDown
    wait = support.ServiceTests.wait

    def test_custom_scope_and_material_are_snapshotted_without_shell_arguments(self):
        payload = {"agent": "ops", "mode": "demo", "params": {"profile_id": "custom", "service": "billing-api", "environment": "prod-eu", "workflow": "single"},
                   "materials": {"format": "fields", "synthetic": False, "observed_at": "2026-10-19T10:00:00+08:00", "metrics": "request_error_rate=0.01 ratio", "logs": "INFO <script>alert(1)</script> $(test)"}}
        task = self.wait(self.service.create_task(payload)["id"])
        self.assertEqual("incomplete", task["status"])
        self.assertEqual("用户提供资料（未核实）", task["source_label"])
        command = self.service.calls[0][0]
        self.assertIn("--service=billing-api", command)
        self.assertIn("--environment=prod-eu", command)
        self.assertFalse(any("$(test)" in part for part in command))
        source = self.service.runtime_dir / "inputs" / task["id"] / "demo_data.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual("billing-api", data["logs"][0]["service"])
        self.assertFalse(data["logs"][0]["synthetic"])
        self.assertIn("$(test)", json.dumps(data))
        self.assertNotIn("materials", json.loads(self.service._row(task["id"])["params"]))

    def test_custom_material_scope_type_and_empty_errors_do_not_enqueue(self):
        cases = [
            {"agent": "ops", "mode": "demo", "params": {"profile_id": "custom"}},
            {"agent": "ops", "mode": "demo", "params": {}, "materials": {}},
            {"agent": "ops", "mode": "demo", "params": {"profile_id": "custom"}, "materials": {"format": "fields", "synthetic": True, "observed_at": "2026-10-19T10:00:00+08:00"}},
            {"agent": "ops", "mode": "demo", "params": {"profile_id": "custom", "service": "../../escape"}, "materials": {}},
            {"agent": "release", "mode": "demo", "params": {"profile_id": "custom", "change_id": "new", "service": "x"}, "materials": {}},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.create_task(payload)
        self.assertEqual([], self.service.calls)
        self.assertEqual([], self.service.list_tasks())

    def test_trash_restore_preserves_files_and_does_not_rescan_deleted_history(self):
        root = self.ops / "runtime/reports"
        root.mkdir(parents=True)
        path = root / "original.json"
        raw = json.dumps({"session_id": "original", "answer": "historic", "complete": True, "demo": True})
        path.write_text(raw, encoding="utf-8")
        task = self.service.list_tasks()[0]
        self.assertTrue(self.service.trash_task(task["id"])["deleted"])
        self.assertEqual([], self.service.list_tasks())
        self.assertEqual([], self.service.list_tasks())
        self.assertEqual(raw, path.read_text(encoding="utf-8"))
        self.assertEqual(task["id"], self.service.list_trash()[0]["id"])
        self.assertTrue(self.service.get_task(task["id"])["deleted"])
        self.assertTrue(self.service.get_report(task["id"], "json")[1])
        self.service.close()
        self.service = support.FakeService(self.config)
        self.assertEqual([], self.service.list_tasks())
        self.assertEqual(1, len(self.service.list_trash()))
        restored = self.service.restore_task(task["id"])
        self.assertFalse(restored["deleted"])
        self.assertEqual("completed", self.service.get_task(task["id"])["status"])
        self.assertEqual([], self.service.calls)

    def test_waiting_review_can_be_restored_but_cannot_be_reviewed_in_trash(self):
        task = self.wait(self.service.create_task({"agent": "release", "mode": "demo", "params": {"change_id": "risk-pool"}})["id"])
        payload = {"reviewer": "Tester", "decision": "acknowledge", "comment": "read", "review_digest": "a" * 64}
        self.service.trash_task(task["id"])
        with self.assertRaises(RuntimeError): self.service.submit_review(task["id"], payload)
        self.assertEqual(1, len(self.service.calls))
        self.service.restore_task(task["id"])
        self.assertEqual("waiting_human", self.service.get_task(task["id"])["status"])
        self.service.submit_review(task["id"], payload)
        self.assertEqual("blocked", self.wait(task["id"])["status"])

    def test_active_tasks_cannot_be_trashed(self):
        self.service._pool.shutdown(wait=True)
        self.service._pool = support.HoldPool()
        task = self.service.create_task({"agent": "ops", "mode": "demo", "params": {}})
        with self.assertRaises(RuntimeError): self.service.trash_task(task["id"])
        self.assertFalse(self.service.get_task(task["id"])["deleted"])
        self.service._update(task["id"], status="running")
        with self.assertRaises(RuntimeError): self.service.trash_task(task["id"])

    def test_old_database_schema_migrates_and_preserves_job(self):
        self.service.close()
        db = self.service.db
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN deleted_at")
            connection.execute("INSERT INTO jobs(id,agent,title,mode,status,created_at,updated_at,params) VALUES('old','ops','old','demo','completed','2026-01-01','2026-01-01','{}')")
        self.service = support.FakeService(self.config)
        self.assertEqual("old", self.service.list_tasks()[0]["id"])
        self.service.trash_task("old")
        self.assertEqual("old", self.service.list_trash()[0]["id"])
