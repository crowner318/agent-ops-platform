"""Loopback-only HTTP tests; no Agent subprocess, user runtime or API key.

The injected service is deliberately inert. Service-level command allowlists,
real report-root containment and human-review rules have separate tests; this
file verifies that HTTP cannot bypass or manufacture those service operations.
"""
from __future__ import annotations

from copy import deepcopy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from workbench.server import MAX_BODY, create_server


class FakeService:
    def __init__(self):
        self.created = []
        self.reviewed = []
        self.downloads = []
        self.closed = 0
        self.trashed = set()
        self.tasks = {"task_1": {"id": "task_1", "agent": "release", "status": "waiting_human",
                                 "decision": "pending_review", "deployment_approved": False}}

    def bootstrap(self):
        return {"agents": ["ops", "release"], "fixture": "isolated-http-test"}

    def list_tasks(self):
        return deepcopy([task for key, task in self.tasks.items() if key not in self.trashed])

    def list_trash(self):
        return deepcopy([dict(self.tasks[key], deleted=True) for key in self.trashed])

    def storage_status(self):
        return {"mode": "legacy", "reports": "files", "redis": {"available": False}}

    def list_cases(self):
        return [{"id": "case_1", "summary": "isolated fixture", "verified": False}]

    def purge_case(self, case_id, payload):
        if payload != {"confirmation": case_id}:
            raise ValueError("确认编号不符。")
        return {"id": case_id, "permanently_deleted": True}

    def purge_task(self, task_id, payload):
        if payload != {"confirmation": task_id}:
            raise ValueError("确认编号不符。")
        return {"id": task_id, "permanently_deleted": True}

    def trash_task(self, task_id):
        value = self.tasks[task_id]
        self.trashed.add(task_id)
        return dict(value, deleted=True)

    def restore_task(self, task_id):
        value = self.tasks[task_id]
        self.trashed.discard(task_id)
        return dict(value, deleted=False)

    def get_material_template(self, agent, profile_id):
        if agent != "ops" or profile_id != "complete-demo":
            raise ValueError("场景不存在。")
        return {"materials": {"format": "json", "data": {"logs": []}, "synthetic": True}, "params": {"service": "order-service", "environment": "demo"}}

    def get_task(self, task_id):
        if task_id == "value_error":
            raise ValueError("参数不符合测试规则。")
        if task_id == "conflict":
            raise RuntimeError("当前任务状态不允许此操作。")
        if task_id == "unexpected":
            raise OSError("INTERNAL_EXCEPTION_MUST_NOT_REACH_BROWSER")
        return deepcopy(self.tasks[task_id])

    def create_task(self, payload):
        if payload.get("invalid"):
            raise ValueError("测试业务输入无效。")
        self.created.append(deepcopy(payload))
        return {"id": "created_task", "status": "queued", "deployment_approved": False}

    def submit_review(self, task_id, payload):
        if task_id not in self.tasks:
            raise KeyError(task_id)
        if payload.get("decision") not in {"acknowledge", "request_changes", "reject"}:
            raise ValueError("意见只能确认阅读、要求修改或拒绝，不能批准生产发布。")
        self.reviewed.append((task_id, deepcopy(payload)))
        # The fixture has a blocker. HTTP must preserve, never reinterpret,
        # the service's blocked result as release approval.
        return {"id": task_id, "status": "blocked", "decision": "blocked",
                "review_record": deepcopy(payload), "deployment_approved": False}

    def get_report(self, task_id, kind):
        if task_id not in self.tasks or kind not in {"md", "json"}:
            raise KeyError(task_id)
        self.downloads.append((task_id, kind))
        if kind == "md":
            return "隔离测试报告.md", b"# Synthetic HTTP fixture\nNo production action.\n"
        return "isolated-report.json", b'{"synthetic":true,"deployment_approved":false}'

    def close(self):
        self.closed += 1


class MemoryStaticRoot:
    """Exercise the allowlist without reading any real file or settings."""
    files = {"index.html": b"<!doctype html><title>HTTP test</title>",
             "styles.css": b"body{color:black}", "app.js": b"'use strict';",
             "favicon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>'}

    def __init__(self):
        self.accessed = []

    def __truediv__(self, name):
        self.accessed.append(name)
        if name not in self.files:
            raise AssertionError("HTTP attempted to open a non-allowlisted static file")
        content = self.files[name]
        class Resource:
            def read_bytes(self):
                return content
        return Resource()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.web = MemoryStaticRoot()
        self.web_patch = patch("workbench.server.WEB_ROOT", self.web)
        self.web_patch.start()
        self.server = create_server({}, host="127.0.0.1", port=0, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.host = f"127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.web_patch.stop()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = (response.status, {key.lower(): value for key, value in response.getheaders()}, response.read())
        finally:
            connection.close()
        return result

    def bootstrap(self):
        status, headers, raw = self.request("GET", "/api/bootstrap")
        self.assertEqual(200, status)
        return json.loads(raw), headers

    def post(self, path, payload, *, token=True, origin=None, extra_headers=None, headers_only=False):
        headers = {"Content-Type": "application/json"}
        if token is True:
            headers["X-Workbench-Token"] = self.server.csrf_token
        elif token is not None:
            headers["X-Workbench-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        headers.update(extra_headers or {})
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if headers_only:
            # Invalid Host/Origin/token/path/content-type must be rejected
            # before the body is read. Do not send a body racing that close.
            headers.setdefault("Host", self.host)
            headers["Content-Length"] = str(len(raw))
            return self.raw_headers("POST", path, list(headers.items()))
        return self.request("POST", path, raw, headers)

    def raw_headers(self, method, path, headers, body=b""):
        # All callers exercise a rejection before body reading. Sending an
        # unread body after a rejection can cause a Windows TCP reset, hiding
        # an otherwise correct error response; headers alone test the guard.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in headers:
                connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_bootstrap_exposes_only_local_session_token_and_security_headers(self):
        body, headers = self.bootstrap()
        self.assertEqual("isolated-http-test", body["fixture"])
        self.assertEqual(self.server.csrf_token, body["csrf_token"])
        self.assertGreaterEqual(len(body["csrf_token"]), 32)
        self.assertIn("no-store", headers["cache-control"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        self.assertEqual("DENY", headers["x-frame-options"])
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertIn("style-src 'self'", headers["content-security-policy"])
        self.assertNotIn("'unsafe-inline'", headers["content-security-policy"])
        self.assertNotEqual("*", headers.get("access-control-allow-origin"))
        self.assertEqual([], self.service.created)
        self.assertEqual([], self.service.reviewed)

    def test_listing_and_reading_do_not_submit_human_review(self):
        for path, key in (("/api/tasks", "tasks"), ("/api/tasks/task_1", "task")):
            status, headers, raw = self.request("GET", path)
            self.assertEqual(200, status)
            self.assertIn(key, json.loads(raw))
            self.assertIn("no-store", headers["cache-control"])
        self.assertEqual([], self.service.reviewed)
        self.assertEqual([], self.service.created)
        self.assertFalse(self.service.tasks["task_1"]["deployment_approved"])

    def test_task_creation_accepts_same_origin_and_token_only_tool_clients(self):
        for origin in (self.origin, f"http://localhost:{self.port}", None):
            payload = {"agent": "ops", "question": "调查模拟连接池", "mode": "demo"}
            status, _, raw = self.post("/api/tasks", payload, origin=origin)
            self.assertEqual(202, status)
            self.assertEqual("queued", json.loads(raw)["task"]["status"])
        self.assertEqual(3, len(self.service.created))
        self.assertEqual([], self.service.reviewed)

    def test_missing_wrong_or_non_ascii_token_cannot_create_task(self):
        for token in (None, "wrong-token", "", "é"):
            with self.subTest(token=token):
                status, _, raw = self.post("/api/tasks", {"agent": "ops"}, token=token, origin=self.origin, headers_only=True)
                self.assertEqual(403, status)
                self.assertIn("error", json.loads(raw))
        self.assertEqual([], self.service.created)

    def test_cross_origin_null_wrong_scheme_or_port_are_rejected_for_get_and_post(self):
        for origin in ("http://evil.invalid", "null", f"https://localhost:{self.port}",
                       f"http://localhost:{self.port + 1}", self.origin + ".evil.invalid"):
            with self.subTest(origin=origin):
                status, _, _ = self.request("GET", "/api/bootstrap", headers={"Origin": origin})
                self.assertEqual(403, status)
                status, _, _ = self.post("/api/tasks", {"agent": "ops"}, origin=origin, headers_only=True)
                self.assertEqual(403, status)
        self.assertEqual([], self.service.created)

    def test_host_validation_prevents_dns_rebinding_and_userinfo_hosts(self):
        for host in (f"evil.invalid:{self.port}", f"127.0.0.1.evil.invalid:{self.port}",
                     f"localhost:{self.port + 1}", "127.0.0.1", f"user@localhost:{self.port}"):
            with self.subTest(host=host):
                status, _, _ = self.request("GET", "/api/bootstrap", headers={"Host": host})
                self.assertEqual(403, status)
        status, _, _ = self.request("GET", "/api/bootstrap", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(200, status)

    def test_duplicate_sensitive_headers_are_rejected(self):
        base = [("Host", self.host), ("Content-Type", "application/json"),
                ("Content-Length", "2"), ("X-Workbench-Token", self.server.csrf_token)]
        additions = [("Host", self.host), ("X-Workbench-Token", self.server.csrf_token),
                     ("Origin", self.origin)]
        for addition in additions:
            headers = base + ([('Origin', self.origin)] if addition[0] == "Origin" else []) + [addition]
            with self.subTest(header=addition[0]):
                status, _, _ = self.raw_headers("POST", "/api/tasks", headers)
                self.assertEqual(403, status)
        self.assertEqual([], self.service.created)

    def test_json_content_type_body_shape_nonfinite_and_encoding_are_checked(self):
        status, _, _ = self.post("/api/tasks", {}, extra_headers={"Content-Type": "text/plain"}, headers_only=True)
        self.assertEqual(415, status)
        headers = {"X-Workbench-Token": self.server.csrf_token, "Content-Type": "application/json"}
        for raw in (b"not-json", b"[]", b"null", b'"string"', b"true", b'{"x":NaN}',
                    b'{"x":Infinity}', b"\xff", b""):
            with self.subTest(raw=raw):
                status, _, body = self.request("POST", "/api/tasks", raw, headers)
                self.assertEqual(400, status)
                self.assertIn("error", json.loads(body))
        self.assertEqual([], self.service.created)

    def test_oversized_and_invalid_lengths_or_chunked_transfer_are_rejected(self):
        base = [("Host", self.host), ("Content-Type", "application/json"),
                ("X-Workbench-Token", self.server.csrf_token)]
        for length in ("-1", "not-a-number"):
            status, _, _ = self.raw_headers("POST", "/api/tasks", base + [("Content-Length", length)])
            self.assertEqual(400, status)
        status, _, _ = self.raw_headers("POST", "/api/tasks", base + [("Content-Length", str(MAX_BODY + 1))])
        self.assertEqual(413, status)
        status, _, _ = self.raw_headers("POST", "/api/tasks", base + [("Content-Length", "2"), ("Transfer-Encoding", "chunked")])
        self.assertEqual(400, status)
        self.assertEqual([], self.service.created)

    def test_reports_are_explicit_attachments_and_query_cannot_choose_a_file(self):
        for kind in ("md", "json"):
            path = f"/api/tasks/task_1/report.{kind}"
            status, headers, content = self.request("GET", path)
            self.assertEqual(200, status)
            self.assertIn("attachment;", headers["content-disposition"])
            self.assertIn("filename*=UTF-8''", headers["content-disposition"])
            self.assertIn("no-store", headers["cache-control"])
            alternate, _, same_content = self.request("GET", path + "?path=../../settings.py&file=C:/private/settings.py")
            self.assertEqual(200, alternate)
            self.assertEqual(content, same_content)
        self.assertEqual([("task_1", "md"), ("task_1", "md"), ("task_1", "json"), ("task_1", "json")], self.service.downloads)
        self.assertEqual([], self.service.reviewed)

    def test_static_allowlist_and_task_paths_do_not_open_arbitrary_files(self):
        for path in ("/", "/index.html", "/styles.css", "/app.js", "/favicon.svg"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(200, status)
        before = list(self.web.accessed)
        bad = ("/settings.py", "/workbench.config.json", "/../settings.py", "/%2e%2e/settings.py",
               "/web/../../settings.py", "/runtime/reviews.sqlite3", "/files/C:/private/settings.py",
               "/api/tasks/../report.json", "/api/tasks/%2e%2e/report.json",
               "/api/tasks/task_1%2f..%2fsettings.py/report.json",
               "/api/tasks/task_1%5c..%5csettings.py/report.json", "/api/tasks/task_1/report.json/extra")
        for path in bad:
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertIn(status, {400, 404})
        self.assertEqual(before, self.web.accessed)
        self.assertEqual([], self.service.downloads)

    def test_only_explicit_authenticated_post_can_submit_review_and_blocked_is_preserved(self):
        payload = {"reviewer": "fixture-user", "decision": "acknowledge", "comment": "只确认阅读，不批准生产发布。"}
        status, _, _ = self.request("GET", "/api/tasks/task_1/review")
        self.assertEqual(405, status)
        self.assertEqual([], self.service.reviewed)
        status, _, _ = self.post("/api/tasks/task_1/review", payload, token=None, origin=self.origin, headers_only=True)
        self.assertEqual(403, status)
        self.assertEqual([], self.service.reviewed)
        status, _, raw = self.post("/api/tasks/task_1/review", payload, origin=self.origin)
        self.assertEqual(202, status)
        task = json.loads(raw)["task"]
        self.assertEqual("blocked", task["status"])
        self.assertEqual("acknowledge", task["review_record"]["decision"])
        self.assertFalse(task["deployment_approved"])
        self.assertEqual([("task_1", payload)], self.service.reviewed)
        status, _, _ = self.post("/api/tasks/task_1/review", dict(payload, decision="approve"), origin=self.origin)
        self.assertEqual(400, status)
        self.assertEqual(1, len(self.service.reviewed))

    def test_business_errors_keep_status_and_unexpected_errors_do_not_leak_details(self):
        for task_id, expected in (("missing", 404), ("value_error", 400), ("conflict", 409), ("unexpected", 500)):
            status, _, raw = self.request("GET", "/api/tasks/" + task_id)
            self.assertEqual(expected, status)
            self.assertIsInstance(json.loads(raw)["error"], str)
            self.assertNotIn(b"INTERNAL_EXCEPTION_MUST_NOT_REACH_BROWSER", raw)
            self.assertNotIn(b"Traceback", raw)

    def test_origin_form_only_no_redirect_or_cross_site_preflight(self):
        status, headers, _ = self.request("GET", "http://evil.invalid/api/bootstrap", headers={"Host": self.host})
        self.assertEqual(400, status)
        self.assertNotIn("location", headers)
        status, _, _ = self.post("http://evil.invalid/api/tasks", {"agent": "ops"},
                                 extra_headers={"Host": self.host}, headers_only=True)
        self.assertEqual(400, status)
        status, headers, _ = self.request("OPTIONS", "/api/tasks",
            headers={"Origin": "http://evil.invalid", "Access-Control-Request-Method": "POST"})
        self.assertEqual(405, status)
        self.assertNotEqual("*", headers.get("access-control-allow-origin"))
        self.assertEqual([], self.service.created)

    def test_only_loopback_bindings_and_valid_ports_are_allowed(self):
        for host in ("0.0.0.0", "192.0.2.1", "example.invalid"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_server({}, host=host, port=0, service=FakeService())
        for port in (-1, 65536, True, "8765"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                create_server({}, host="127.0.0.1", port=port, service=FakeService())


if __name__ == "__main__":
    unittest.main()
