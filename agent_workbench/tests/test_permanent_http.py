import json
import unittest
import test_http as support


class PermanentHTTPTests(unittest.TestCase):
    setUp = support.HTTPTests.setUp
    tearDown = support.HTTPTests.tearDown
    request = support.HTTPTests.request
    post = support.HTTPTests.post
    raw_headers = support.HTTPTests.raw_headers

    def test_permanent_endpoints_require_csrf_and_exact_confirmation(self):
        for path, key in (("/api/tasks/task_1/purge", "task_1"), ("/api/cases/case_1/purge", "case_1")):
            self.assertEqual(403, self.post(path, {"confirmation": key}, token=None, headers_only=True)[0])
            self.assertEqual(400, self.post(path, {"confirmation": "wrong"})[0])
            status, _, raw = self.post(path, {"confirmation": key})
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["permanently_deleted"])

    def test_status_and_case_reads_do_not_submit_deletion_or_model_requests(self):
        self.assertEqual("legacy", json.loads(self.request("GET", "/api/storage-status")[2])["mode"])
        self.assertEqual("case_1", json.loads(self.request("GET", "/api/cases")[2])["cases"][0]["id"])
        self.assertEqual([], self.service.created)
        self.assertEqual([], self.service.reviewed)

    def test_purge_does_not_accept_path_or_cross_origin(self):
        self.assertEqual(403, self.post("/api/cases/case_1/purge", {"confirmation": "case_1"}, origin="http://evil.invalid", headers_only=True)[0])
        self.assertEqual(404, self.post("/api/cases/..%2Fprivate/purge", {})[0])
