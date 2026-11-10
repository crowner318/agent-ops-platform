"""New mutation routes keep the existing same-origin and session-token checks."""
import json
import unittest
import test_http as support


class CustomHTTPTests(unittest.TestCase):
    setUp = support.HTTPTests.setUp
    tearDown = support.HTTPTests.tearDown
    request = support.HTTPTests.request
    post = support.HTTPTests.post
    raw_headers = support.HTTPTests.raw_headers

    def test_trash_and_restore_require_token_and_keep_original_status(self):
        for action in ("trash", "restore"):
            status, _, _ = self.post(f"/api/tasks/task_1/{action}", {}, token=None, headers_only=True)
            self.assertEqual(403, status)
        status, _, raw = self.post("/api/tasks/task_1/trash", {})
        self.assertEqual(202, status)
        self.assertTrue(json.loads(raw)["task"]["deleted"])
        self.assertEqual([], json.loads(self.request("GET", "/api/tasks")[2])["tasks"])
        self.assertEqual(1, len(json.loads(self.request("GET", "/api/trash")[2])["tasks"]))
        self.assertEqual("waiting_human", self.service.tasks["task_1"]["status"])
        self.assertEqual([], self.service.reviewed)
        status, _, raw = self.post("/api/tasks/task_1/restore", {})
        self.assertEqual(202, status)
        self.assertFalse(json.loads(raw)["task"]["deleted"])

    def test_recycle_routes_reject_payload_paths_cross_origin_and_wrong_verb(self):
        for action in ("trash", "restore"):
            path = "/api/tasks/task_1/" + action
            self.assertEqual(400, self.post(path, {"path": "C:/private"})[0])
            self.assertEqual(403, self.post(path, {}, origin="http://evil.invalid", headers_only=True)[0])
            self.assertEqual(405, self.request("GET", path)[0])
        self.assertFalse(self.service.trashed)

    def test_material_template_accepts_only_fixed_agent_and_profile_query(self):
        status, _, raw = self.request("GET", "/api/material-template?agent=ops&profile_id=complete-demo")
        self.assertEqual(200, status)
        self.assertEqual("json", json.loads(raw)["materials"]["format"])
        for query in ("agent=ops", "agent=ops&agent=release&profile_id=complete-demo", "agent=ops&profile_id=complete-demo&path=C:/private", "agent=ops&profile_id=../../private", "agent=ops&profile_id="):
            self.assertEqual(400, self.request("GET", "/api/material-template?" + query)[0])
        self.assertEqual(403, self.request("GET", "/api/trash", headers={"Origin": "http://evil.invalid"})[0])
