"""Verify usable demo scopes, immutable inputs and honest sparse-data outcomes."""
import json
import unittest

import test_service as support
from workbench.profiles import scenarios


class ProfileTests(unittest.TestCase):
    def setUp(self):
        support.ServiceTests.setUp(self)
        self.service._pool.shutdown(wait=True)
        self.service._pool = support.HoldPool()
        self.data = self.ops / "data"
        self.data.mkdir()
        row = {"service": "order-service", "environment": "demo", "timestamp": "2026-10-19T10:05:00+08:00", "synthetic": True}
        self.fixture = {kind: [dict(row, id=kind)] for kind in ("metrics", "logs", "traces", "changes")}
        self.fixture["metrics"].append(dict(row, environment="staging", id="smoke"))
        (self.data / "demo_data.json").write_text(json.dumps(self.fixture), encoding="utf-8")
        (self.data / "demo_complete").mkdir()
        (self.data / "demo_complete/demo_data.json").write_text(json.dumps(self.fixture), encoding="utf-8")

    def tearDown(self):
        support.ServiceTests.tearDown(self)

    def test_scopes_count_current_material_only(self):
        profiles = scenarios("ops", self.ops)
        complete, original, sparse = profiles
        self.assertEqual("complete-demo", complete["id"])
        self.assertEqual(4, complete["coverage"]["current_count"])
        self.assertEqual("complete", complete["coverage"]["material_status"])
        self.assertEqual(1, sparse["coverage"]["current_count"])
        self.assertEqual(["logs", "traces", "changes"], sparse["coverage"]["missing_kinds"])
        self.assertTrue(sparse["ready"])
        self.assertEqual("synthetic", original["source"])
        self.assertEqual("single", self.service.bootstrap()["defaults"]["ops"]["workflow"])

    def test_empty_or_mismatched_scope_is_rejected_before_enqueue(self):
        for params in ({"profile_id": "../complete-demo"}, {"profile_id": "unknown"},
                       {"profile_id": "complete-demo", "environment": "production"},
                       {"profile_id": "complete-demo", "service": "payment-service"},
                       {"profile_id": "complete-demo", "start": "2026-11-01T00:00:00+08:00", "end": "2026-11-01T01:00:00+08:00"},
                       {"profile_id": "complete-demo", "data_dir": str(self.root)},
                       {"profile_id": "complete-demo", "_coverage": {"material_status": "complete"}}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.service.create_task({"agent": "ops", "mode": "api", "params": params})
        self.assertEqual([], self.service._pool.jobs)
        self.assertEqual([], self.service.list_tasks())

    def test_queued_task_uses_immutable_selected_input(self):
        task = self.service.create_task({"agent": "ops", "mode": "demo", "params": {"profile_id": "complete-demo"}})
        snapshot = self.service.runtime_dir / "inputs" / task["id"] / "demo_data.json"
        original = snapshot.read_bytes()
        (self.data / "demo_complete/demo_data.json").write_text('{}', encoding="utf-8")
        self.assertEqual(original, snapshot.read_bytes())
        function, task_id, action = self.service._pool.jobs[0]
        function(task_id, action)
        command = self.service.calls[0][0]
        self.assertIn("--data-dir=" + str(snapshot.parent), command)
        self.assertEqual("completed", self.service.get_task(task_id)["status"])
        self.assertEqual("complete", task["coverage"]["material_status"])

    def test_sparse_samples_are_not_presented_as_complete(self):
        task = self.service.create_task({"agent": "ops", "mode": "demo", "params": {"profile_id": "healthy-staging"}})
        function, task_id, action = self.service._pool.jobs[0]
        function(task_id, action)
        detail = self.service.get_task(task_id)
        self.assertEqual("incomplete", detail["status"])
        self.assertEqual("partial", detail["assessment"]["material_status"])
        self.assertEqual("demo", detail["assessment"]["model_status"])
        self.assertEqual("generated", detail["assessment"]["generation_status"])
        self.assertEqual("模拟演示资料", detail["source_label"])
        self.assertTrue(detail["report"]["data"]["complete"])

    def test_trusted_settings_path_is_runtime_environment_only(self):
        self.service.ops_settings_file = self.root / "private-settings.py"
        payload = self.service.bootstrap()
        self.assertNotIn(str(self.service.ops_settings_file), json.dumps(payload))
        self.assertEqual(str(self.service.ops_settings_file), self.service._environment()["OPS_SETTINGS_FILE"])
        self.assertNotIn("OPS_SETTINGS_FILE", json.dumps(self.service.list_tasks()))


if __name__ == "__main__":
    unittest.main()
