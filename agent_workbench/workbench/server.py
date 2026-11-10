"""Loopback-only HTTP interface for the two existing command-line Agents."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
from urllib.parse import parse_qs, quote, unquote, urlsplit

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
MAX_BODY = 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
TASK_PATH = re.compile(r"/api/tasks/([A-Za-z0-9_-]{1,100})(?:/(review|trash|restore|purge|report\.md|report\.json))?\Z")
CASE_PATH = re.compile(r"/api/cases/([A-Za-z0-9_-]{1,80})/purge\Z")


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR permits two listeners on the same endpoint. An old
    # Python process can then serve stale routes beside the updated process.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        super().server_close()
        if not getattr(self, "_service_closed", False):
            self._service_closed = True
            service = getattr(self, "service", None)
            if service is not None:
                service.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentWorkbench/1.0"
    sys_version = ""

    def log_message(self, *_):
        # Request bodies and headers contain human comments / session tokens.
        pass

    def _respond(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, value):
        self._respond(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _host_ok(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        try:
            parsed = urlsplit("//" + hosts[0])
            return (parsed.hostname in {"127.0.0.1", "localhost"} and
                    parsed.port == self.server.server_port and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def _origin_ok(self):
        origins = self.headers.get_all("Origin", [])
        return not origins or (len(origins) == 1 and origins[0] in {
            f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"})

    def _guard(self, mutation=False):
        if not self._host_ok():
            self._error(403, "请通过本机工作台地址访问。")
            return False
        if not self._origin_ok():
            self._error(403, "请求来源不属于本工作台。")
            return False
        if mutation:
            tokens = self.headers.get_all("X-Workbench-Token", [])
            if len(tokens) != 1 or not tokens[0].isascii() or not hmac.compare_digest(tokens[0], self.server.csrf_token):
                self._error(403, "页面会话已失效，请刷新页面后重试。")
                return False
        return True

    def _request_path(self):
        try:
            if not self.path.startswith("/") or self.path.startswith("//"):
                raise ValueError("invalid_origin_form")
            return unquote(urlsplit(self.path).path)
        except ValueError:
            self._error(400, "请求地址格式无效。")
            return None

    def _dispatch_error(self, exc):
        if isinstance(exc, KeyError):
            self._error(404, "没有找到该任务或报告。")
        elif isinstance(exc, (ValueError, RuntimeError)):
            message = str(exc)[:400]
            message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b|(?i:Bearer)\s+\S+", "[REDACTED]", message)
            self._error(400 if isinstance(exc, ValueError) else 409, message or "请求无法处理。")
        else:
            self._error(500, "工作台暂时无法处理请求，请查看任务状态或重试。")

    def do_GET(self):
        if not self._guard():
            return
        path = self._request_path()
        if path is None:
            return
        service = self.server.service
        try:
            if path == "/api/bootstrap":
                self._json(200, dict(service.bootstrap(), csrf_token=self.server.csrf_token))
            elif path == "/api/tasks":
                self._json(200, {"tasks": service.list_tasks()})
            elif path == "/api/trash":
                self._json(200, {"tasks": service.list_trash()})
            elif path == "/api/storage-status":
                self._json(200, service.storage_status())
            elif path == "/api/cases":
                self._json(200, {"cases": service.list_cases()})
            elif path == "/api/material-template":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
                if set(query) != {"agent", "profile_id"} or any(len(values) != 1 for values in query.values()):
                    raise ValueError("请提供唯一的Agent和场景编号。")
                self._json(200, service.get_material_template(query["agent"][0], query["profile_id"][0]))
            elif match := TASK_PATH.fullmatch(path):
                task_id, action = match.groups()
                if action in {"report.md", "report.json"}:
                    kind = action.rsplit(".", 1)[1]
                    filename, content = service.get_report(task_id, kind)
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("报告超过下载大小限制。")
                    safe_name = Path(filename).name.replace("\\", "_").replace("\r", "").replace("\n", "")
                    mime = "text/markdown; charset=utf-8" if kind == "md" else "application/json; charset=utf-8"
                    self._respond(200, content, mime, {"Content-Disposition":
                        f"attachment; filename=report.{kind}; filename*=UTF-8''{quote(safe_name, safe='')}"})
                elif action is None:
                    self._json(200, {"task": service.get_task(task_id)})
                else:
                    self._error(405, "此入口需要通过POST提交操作。")
            elif path in STATIC:
                name, mime = STATIC[path]
                self._respond(200, (WEB_ROOT / name).read_bytes(), mime)
            else:
                self._error(404, "页面或接口不存在。")
        except Exception as exc:
            self._dispatch_error(exc)

    def do_POST(self):
        if not self._guard(mutation=True):
            return
        path = self._request_path()
        if path is None:
            return
        match = TASK_PATH.fullmatch(path)
        case_match = CASE_PATH.fullmatch(path)
        if path not in {"/api/tasks", "/api/material-preview"} and not (match and match.group(2) in {"review", "trash", "restore", "purge"}) and not case_match:
            self._error(404, "提交入口不存在。")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(415, "请使用JSON提交任务。")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "不支持分块提交。")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"\d{1,10}", lengths[0]):
            self._error(400, "请求长度无效。")
            return
        length = int(lengths[0])
        if length > MAX_BODY:
            self._error(413, "提交内容超过1MiB，请缩小材料。")
            return
        try:
            self.connection.settimeout(10)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("提交内容不完整。")
            def invalid_constant(_):
                raise ValueError("JSON中不能包含非有限数字。")
            try:
                payload = json.loads(raw, parse_constant=invalid_constant)
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("提交内容不是有效JSON。") from None
            if not isinstance(payload, dict):
                raise ValueError("提交内容必须是JSON对象。")
            if path == "/api/material-preview":
                from .materials import preview_materials
                if set(payload) != {"agent", "document"}:
                    raise ValueError("请提交agent和document两个字段。")
                self._json(200, preview_materials(payload["agent"], payload["document"]))
                return
            if path == "/api/tasks":
                result = self.server.service.create_task(payload)
            elif case_match:
                self._json(200, self.server.service.purge_case(case_match.group(1), payload))
                return
            elif match.group(2) == "purge":
                self._json(200, self.server.service.purge_task(match.group(1), payload))
                return
            elif match.group(2) == "review":
                result = self.server.service.submit_review(match.group(1), payload)
            else:
                if payload:
                    raise ValueError("回收站操作不接受额外参数。")
                action = self.server.service.trash_task if match.group(2) == "trash" else self.server.service.restore_task
                result = action(match.group(1))
            self._json(202, {"task": result})
        except Exception as exc:
            self._dispatch_error(exc)

    def do_OPTIONS(self):
        self._error(405, "不支持跨站调用。")

    do_PUT = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_PATCH = do_OPTIONS


def create_server(config: dict, host="127.0.0.1", port=8765, service=None, container_bind=False):
    if host not in {"127.0.0.1", "localhost"} and not (host == "0.0.0.0" and container_bind and os.environ.get("WORKBENCH_CONTAINER") == "1"):
        raise ValueError("工作台只允许绑定本机127.0.0.1或localhost。")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须是0至65535的整数。")
    # Acquire the endpoint before touching the job database. Starting a second
    # instance must not mark the active instance's jobs as interrupted.
    server = WorkbenchHTTPServer((host, port), Handler)
    try:
        if service is None:
            from .service import WorkbenchService
            service = WorkbenchService(config)
        server.service = service
        server.csrf_token = secrets.token_urlsafe(32)
    except Exception:
        server.server_close()
        raise
    return server
