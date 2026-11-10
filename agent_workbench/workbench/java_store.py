"""Typed private HTTP client. Java owns business SQLite and Redis reports."""
import json
import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler


class JavaStore:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Java存储地址须为可信内部服务根地址。")
        if not token or len(token) < 16:
            raise ValueError("Java模式需要至少16字符的STORAGE_SERVICE_TOKEN。")
        self.url, self.token = url.rstrip("/"), token
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method, path, value=None):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") if value is not None else None
        request = Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json", "X-Storage-Token": self.token})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(17 * 1024 * 1024)
                if len(raw) >= 17 * 1024 * 1024:
                    raise RuntimeError("存储服务响应过大。")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if path == "/health" and exc.code == 503:
                try:
                    return json.loads(exc.read(65536))
                except ValueError:
                    raise RuntimeError("存储健康检查返回无效。") from None
            if exc.code == 404:
                raise KeyError("存储记录不存在。") from None
            if exc.code == 409:
                raise RuntimeError("存储记录冲突，请刷新后重试。") from None
            if exc.code in {401, 403}:
                raise RuntimeError("Java存储认证失败，请检查服务端令牌配置。") from None
            raise RuntimeError("Java或Redis存储暂不可用；未切换到本地文件。") from None
        except (OSError, URLError, ValueError):
            raise RuntimeError("无法连接Java存储服务；请启动Java和Redis。") from None

    @staticmethod
    def key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("存储编号格式无效。")
        return quote(value, safe="")

    def jobs(self): return self.request("GET", "/v1/jobs?include_deleted=true")["jobs"]
    def get(self, key): return self.request("GET", "/v1/jobs/" + self.key(key))["job"]
    def insert(self, row): return self.request("POST", "/v1/jobs", {"job": row})
    def import_tombstone(self, row):
        payload = {key: row[key] for key in ("id", "agent", "source_key", "purged_at", "review_id", "created_at", "updated_at") if row.get(key) is not None}
        payload.setdefault("source_key", hashlib.sha256(("task:" + row["id"]).encode()).hexdigest())
        return self.request("POST", "/v1/jobs/import-tombstone", {"job": payload})
    def patch(self, key, values): return self.request("PATCH", "/v1/jobs/" + self.key(key), {"values": values})
    def recover(self, error, updated_at): return self.request("POST", "/v1/jobs/recover", {"error": error, "updated_at": updated_at})
    def put_report(self, key, data, markdown): return self.request("POST", "/v1/reports/" + self.key(key), {"json": data, "markdown": markdown})
    def report(self, key): return self.request("GET", "/v1/reports/" + self.key(key))
    def purge(self, key): return self.request("POST", "/v1/jobs/" + self.key(key) + "/purge", {"confirmation": key})
    def cases(self): return self.request("GET", "/v1/cases")["cases"]
    def case_tombstones(self): return self.request("GET", "/v1/case-tombstones")["ids"]
    def import_case_tombstone(self, key): return self.request("POST", "/v1/cases/import-tombstone", {"id": self.key(key)})
    def add_case(self, value): return self.request("POST", "/v1/cases", {"case": value})
    def purge_case(self, key): return self.request("DELETE", "/v1/cases/" + self.key(key), {"confirmation": key})
    def health(self): return self.request("GET", "/health")

    def dispatch_enqueue(self, event_id, task_id, action, updated_at, *, job=None):
        payload = {"id": event_id, "task_id": task_id, "action": action, "updated_at": updated_at}
        if job is not None:
            payload["job"] = job
        return self.request("POST", "/v1/dispatch", payload)

    def dispatch_pending(self):
        return self.request("GET", "/v1/dispatch/pending")["events"]

    def dispatch_published(self, event_id):
        return self.request("POST", "/v1/dispatch/" + self.key(event_id) + "/published", {})

    def dispatch_claim(self, event_id, worker_id, updated_at):
        return self.request("POST", "/v1/dispatch/" + self.key(event_id) + "/claim",
                            {"worker_id": worker_id, "updated_at": updated_at})

    def dispatch_finish(self, event_id, worker_id, updated_at):
        return self.request("POST", "/v1/dispatch/" + self.key(event_id) + "/finish",
                            {"worker_id": worker_id, "updated_at": updated_at})

    def dispatch_recover(self, before, updated_at):
        return self.request("POST", "/v1/dispatch/recover", {"before": before, "updated_at": updated_at})

    def dispatch_recover_untracked(self, error, updated_at):
        return self.request("POST", "/v1/dispatch/recover-untracked", {"error": error, "updated_at": updated_at})
