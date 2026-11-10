"""Bounded subprocess jobs and a read-only report index. No Agent modules imported."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from uuid import uuid4

from .profiles import coverage, scenarios, public_profile
from .materials import MAX_MATERIAL_BYTES, material_template, prepare_materials
from .version import service_info
from .java_store import JavaStore

MAX_REPORT = 8 * 1024 * 1024
MAX_LOGS = 300
TERMINAL = {"completed", "review_acknowledged", "blocked", "incomplete", "failed", "interrupted", "rejected", "changes_requested"}
RELEASE_STATES = {"waiting_human", "review_acknowledged", "blocked", "incomplete", "rejected", "changes_requested"}
OPS_DEFAULTS = {"service": "order-service", "environment": "demo", "start": "2026-10-19T09:55:00+08:00",
                "end": "2026-10-19T10:20:00+08:00", "question": "订单服务发布后错误率升高，请分析候选根因、给出处置建议并生成复盘草稿。"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"reasoningcontent", "reasoning", "additionalreasoning"}:
                continue
            sensitive = normalized in {"apikey", "password", "passwd", "token", "secret", "authorization", "redisurl"}
            sensitive = sensitive or normalized.endswith(("apikey", "password", "secret", "accesstoken", "refreshtoken"))
            result[str(key)] = "[REDACTED]" if sensitive else redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(redact(json.loads(value)), ensure_ascii=False, allow_nan=False)
            except (ValueError, RecursionError):
                pass
        value = re.sub(r"(?i)\bBearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
        value = re.sub(r"(?i)(rediss?://)[^/@\s]+@", r"\1[REDACTED]@", value)
        return re.sub(r"(?i)(\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|token)\s*[:=]\s*)"
                      r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)", r"\1[REDACTED]", value)
    if isinstance(value, float) and not (float("-inf") < value < float("inf")):
        return None
    return value


def _text(value, name, maximum, nonempty=True):
    if not isinstance(value, str) or len(value) > maximum or "\0" in value or (nonempty and not value.strip()):
        raise ValueError(f"{name}为空、类型无效或过长。")
    return value.strip()


def _identifier(value, name="编号"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError(f"{name}格式无效。")
    return value


class WorkbenchService:
    def __init__(self, config):
        self.ops_project = Path(config["ops_project"]).resolve()
        self.release_project = Path(config["release_project"]).resolve()
        self.runtime_dir = Path(config["runtime_dir"]).resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.ops_runtime = self.runtime_dir / "ops"
        self.ops_runtime.mkdir(parents=True, exist_ok=True)
        self.release_runtime = Path(config.get("release_runtime_dir", self.release_project / "runtime")).resolve()
        self.python = {name: str(Path(config.get(name + "_python", config["python_executable"])).resolve()) for name in ("ops", "release")}
        self.ops_settings_file = config.get("ops_settings_file")
        self.projects = {"ops": self.ops_project, "release": self.release_project}
        self.roots = {"ops_original": Path(config.get("ops_history_project", self.ops_project)).resolve() / "runtime/reports", "ops_workbench": self.ops_runtime / "reports",
                      "release": self.release_runtime / "reports"}
        self.db = self.runtime_dir / "workbench.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._processes = {}
        self.queue_mode = config.get("queue_mode", os.environ.get("WORKBENCH_QUEUE_MODE", "local"))
        self.role = config.get("role", "web")
        if self.queue_mode not in {"local", "kafka"} or self.role not in {"web", "worker"}:
            raise ValueError("任务队列模式或执行角色无效。")
        if self.role == "worker" and self.queue_mode != "kafka":
            raise ValueError("独立执行进程需要 Kafka 队列。")
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workbench") if self.queue_mode == "local" else None
        self._publisher = None
        self._config_cache = None
        self._config_checked = 0.0
        self._report_stamps = {}
        self.store = None
        self.storage_mode = config.get("storage_mode", os.environ.get("WORKBENCH_STORAGE_MODE", "legacy"))
        if self.storage_mode not in {"legacy", "java"}:
            raise ValueError("storage_mode只能为legacy或java。")
        if self.queue_mode == "kafka" and self.storage_mode != "java":
            raise ValueError("Kafka 模式需要 Java 存储来保存待发送任务。")
        if self.storage_mode == "java":
            self.store = JavaStore(config.get("storage_service_url", os.environ.get("STORAGE_SERVICE_URL", "http://127.0.0.1:8780")), os.environ.get("STORAGE_SERVICE_TOKEN", ""))
            if self.role == "web":
                message = "工作台已重启；任务未自动重放，请检查已有报告。"
                if self.queue_mode == "kafka":
                    self.store.dispatch_recover_untracked(message, _now())
                else:
                    self.store.recover(message, _now())
                self._import_legacy_jobs()
        else:
            self._init_legacy_jobs()
        if self.role == "web":
            self._scan_history()
            if self.queue_mode == "kafka":
                from .kafka_queue import OutboxPublisher
                self._publisher = OutboxPublisher(self.store)
                self._publisher.start()

    def _init_legacy_jobs(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, agent TEXT NOT NULL, title TEXT NOT NULL, mode TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                params TEXT NOT NULL, logs TEXT NOT NULL DEFAULT '[]', review_id TEXT,
                report_root TEXT, report_path TEXT, is_history INTEGER NOT NULL DEFAULT 0, error TEXT)""")
            if "deleted_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}:
                connection.execute("ALTER TABLE jobs ADD COLUMN deleted_at TEXT")
            for column in ("purged_at", "source_key"):
                if column not in {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}:
                    connection.execute("ALTER TABLE jobs ADD COLUMN " + column + " TEXT")
            connection.execute("CREATE TABLE IF NOT EXISTS case_tombstones(id TEXT PRIMARY KEY)")
            connection.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')",
                               ("工作台已重启；任务未自动重放，请检查已有报告。", _now()))

    def _all_rows(self):
        if self.store:
            return self.store.jobs()
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY created_at DESC")]

    def _import_legacy_jobs(self):
        """Copy legacy metadata once per id; Java tombstones always take precedence."""
        if not self.db.is_file():
            return
        known = {row["id"] for row in self.store.jobs()}
        with closing(sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute("SELECT * FROM jobs")] if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone() else []
            case_tombstones = [row[0] for row in connection.execute("SELECT id FROM case_tombstones")] if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_tombstones'").fetchone() else []
        for case_id in case_tombstones:
            self.store.import_case_tombstone(case_id)
        for row in rows:
            if row["id"] in known:
                continue
            if row.get("purged_at"):
                self.store.import_tombstone(row)
                continue
            if row["status"] in {"queued", "running"}:
                row.update(status="interrupted", error="迁移前任务未自动重放。")
            self._insert_job(row)
            if row.get("report_path") and row.get("report_root") in self.roots and not row.get("purged_at"):
                path = Path(row["report_path"])
                if path.is_file():
                    data = json.loads(self._read_report(row["report_root"], path))
                    self._store_report(row["id"], row["report_root"], path, data)

    def _insert_job(self, values):
        row = self._new_job_row(values)
        if row.get("report_path"):
            row["source_key"] = hashlib.sha256(row["report_path"].encode()).hexdigest()
        if self.store:
            self.store.insert(row)
        else:
            with self._connect() as connection:
                connection.execute("INSERT INTO jobs(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))

    @staticmethod
    def _new_job_row(values):
        return {**dict(logs="[]", review_id=None, report_root=None, report_path=None, is_history=0, error=None,
                       deleted_at=None, purged_at=None, source_key=None), **values}

    def _enqueue(self, task_id, action, *, job=None):
        if self.queue_mode == "kafka":
            event_id = "event-" + uuid4().hex
            self.store.dispatch_enqueue(event_id, task_id, action, _now(), job=self._new_job_row(job) if job else None)
        else:
            if job is not None:
                self._insert_job(job)
            else:
                self._update(task_id, status="queued", error=None)
            self._pool.submit(self._worker, task_id, action)

    def _patch_job(self, task_id, values):
        if values.get("report_path"):
            values = dict(values, source_key=hashlib.sha256(values["report_path"].encode()).hexdigest())
        if self.store:
            self.store.patch(task_id, values)
        else:
            with self._connect() as connection:
                connection.execute("UPDATE jobs SET " + ",".join(key + "=?" for key in values) + " WHERE id=?", (*values.values(), task_id))

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _row(self, task_id):
        _identifier(task_id)
        if self.store:
            row = self.store.get(task_id)
            if row.get("purged_at") or row.get("purged"):
                raise KeyError(task_id)
            return row
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
        if row is None or row["purged_at"]:
            raise KeyError(task_id)
        return dict(row)

    def _update(self, task_id, **values):
        allowed = {"status", "logs", "review_id", "report_root", "report_path", "error"}
        if set(values) - allowed:
            raise ValueError("内部任务字段无效。")
        values["updated_at"] = _now()
        self._patch_job(task_id, values)

    def _read_report(self, root_name, path):
        root = self.roots[root_name].resolve()
        candidate = Path(path).resolve()
        if candidate.parent != root or candidate.suffix.lower() not in {".json", ".md"}:
            raise ValueError("报告必须位于固定报告目录内。")
        if candidate.stat().st_size > MAX_REPORT:
            raise ValueError("报告超过8MiB限制。")
        raw = candidate.read_bytes()
        if len(raw) > MAX_REPORT:
            raise ValueError("报告超过8MiB限制。")
        return raw.decode("utf-8-sig")

    def _report_data(self, row):
        if self.store:
            try:
                return self.store.report(row["id"])["json"]
            except KeyError:
                return None
        if not row.get("report_path") or row.get("report_root") not in self.roots:
            return None
        value = json.loads(self._read_report(row["report_root"], row["report_path"]))
        if not isinstance(value, dict):
            raise ValueError("报告必须是JSON对象。")
        return value

    def _store_report(self, task_id, root_name, path, data):
        if not self.store:
            return
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        if self._report_stamps.get(str(path)) == stamp:
            return
        try:
            markdown = self._read_report(root_name, path.with_suffix(".md"))
        except FileNotFoundError:
            markdown = ""
        self.store.put_report(task_id, redact(data), redact(markdown))
        self._report_stamps[str(path)] = stamp

    @staticmethod
    def _business_status(agent, data):
        if agent == "ops":
            return "completed" if data.get("complete") is True else "incomplete"
        status = data.get("status")
        return status if status in RELEASE_STATES else "incomplete"

    def _scan_history(self):
        rows = self._all_rows()
        by_path = {row.get("report_path"): row for row in rows if row.get("report_path")}
        by_source = {row.get("source_key"): row for row in rows if row.get("source_key")}
        by_review = {row.get("review_id"): row for row in rows if row.get("review_id")}
        by_id = {row["id"]: row for row in rows}
        for root_name, root in self.roots.items():
            if not root.is_dir():
                continue
            for path in root.glob("*.json"):
                try:
                    data = json.loads(self._read_report(root_name, path))
                    if not isinstance(data, dict) or (root_name != "release" and "answer" not in data):
                        continue
                    if root_name == "release" and not isinstance(data.get("review_id"), str):
                        continue
                    report_path = str(path.resolve())
                    source_key = hashlib.sha256(report_path.encode()).hexdigest()
                    existing = by_path.get(report_path) or by_source.get(source_key) or by_review.get(data.get("review_id")) or by_id.get(data.get("session_id"))
                    if existing:
                        if existing.get("purged_at") or existing.get("purged"):
                            continue
                        if existing["status"] in {"queued", "running"}:
                            continue
                        if not existing.get("report_path"):
                            self._patch_job(existing["id"], {"report_root": root_name, "report_path": report_path})
                        if existing["status"] != "interrupted" and (existing["status"] == "waiting_human" or existing["is_history"]):
                            self._update(existing["id"], status=self._business_status(existing["agent"], data))
                        if self.store:
                            self._store_report(existing["id"], root_name, path, data)
                        continue
                    task_id = "hist-" + source_key[:24]
                    agent = "release" if root_name == "release" else "ops"
                    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                    mode = "demo" if data.get("demo") is True or metadata.get("mode") == "demo" or data.get("run_mode") == "scripted_demo" else "api"
                    title = ("历史发布评审 · " + str(data.get("change_id", ""))) if agent == "release" else "历史运维调查 · " + str(data.get("session_id", path.stem))
                    created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    self._insert_job(dict(id=task_id, agent=agent, title=redact(title[:180]), mode=mode, status=self._business_status(agent, data), created_at=created, updated_at=created, params="{}", review_id=data.get("review_id") if agent == "release" else None, report_root=root_name, report_path=report_path, is_history=1))
                    by_id[task_id] = {"id": task_id, "status": "interrupted", "report_path": report_path}
                    if self.store:
                        self._store_report(task_id, root_name, path, data)
                except (OSError, ValueError, TypeError, RecursionError):
                    continue

    def _summary(self, row):
        result = {key: row[key] for key in ("id", "agent", "title", "mode", "status", "created_at", "updated_at")}
        result["report_available"] = bool(row.get("report_path"))
        result["historical"] = bool(row.get("is_history"))
        result["deleted"] = bool(row.get("deleted_at"))
        result["deleted_at"] = row.get("deleted_at")
        if row.get("review_id"):
            result["review_id"] = row["review_id"]
        if row.get("error"):
            result["error"] = row["error"]
        context = json.loads(row.get("params") or "{}")
        result.update(profile_label=context.get("_profile_label", "历史材料"),
                      source_label=context.get("_source_label", "按报告中的来源标记判断"),
                      coverage=context.get("_coverage"), owner=context.get("_owner"), service=context.get("service"), environment=context.get("environment"))
        return redact(result)

    def list_tasks(self):
        self._scan_history()
        return [self._summary(row) for row in sorted(self._all_rows(), key=lambda row: row["created_at"], reverse=True) if not row.get("deleted_at") and not row.get("purged_at") and not row.get("purged")][:500]

    def list_trash(self):
        self._scan_history()
        return [self._summary(row) for row in sorted(self._all_rows(), key=lambda row: row.get("deleted_at") or "", reverse=True) if row.get("deleted_at") and not row.get("purged_at") and not row.get("purged")][:500]

    def trash_task(self, task_id):
        with self._lock:
            row = self._row(task_id)
            if row["status"] in {"queued", "running"}:
                raise RuntimeError("任务正在运行，请结束后再移到回收站。")
            if not row.get("deleted_at"):
                self._patch_job(task_id, {"deleted_at": _now(), "updated_at": _now()})
            return self._summary(self._row(task_id))

    def restore_task(self, task_id):
        with self._lock:
            self._row(task_id)
            self._patch_job(task_id, {"deleted_at": None, "updated_at": _now()})
            return self._summary(self._row(task_id))

    @staticmethod
    def _ensure_visible(row):
        if row.get("deleted_at"):
            raise RuntimeError("该任务已移到回收站，请先恢复后查看报告或提交复核。")

    def get_task(self, task_id):
        self._scan_history()
        row = self._row(task_id)
        result = dict(self._summary(row), logs=redact(json.loads(row["logs"])), report=None)
        try:
            data = self._report_data(row)
            if data is not None:
                md = ""
                try:
                    md = self.store.report(task_id)["markdown"] if self.store else self._read_report(row["report_root"], Path(row["report_path"]).with_suffix(".md"))
                except FileNotFoundError:
                    pass
                result["report"] = {"data": redact(data), "markdown": redact(md), "review_digest": data.get("review_digest"),
                    "download_md": f"/api/tasks/{task_id}/report.md", "download_json": f"/api/tasks/{task_id}/report.json"}
                result["assessment"] = {
                    "generation_status": "generated", "material_status": (result.get("coverage") or {}).get("material_status", "unknown"),
                    "quality_status": "issues" if data.get("quality_issues") else "passed",
                    "model_status": "demo" if row["mode"] == "demo" else "api" if (
                        data.get("execution", {}).get("api_attempts", 0) or any(call.get("attempts", 0) for call in data.get("model_calls", []))) else "not_called"}
        except (OSError, ValueError, RecursionError):
            result["report_available"] = False
            result["error"] = "报告不可用、内容无效或超过大小限制。"
        return result

    def get_report(self, task_id, kind):
        if not isinstance(kind, str) or kind not in {"md", "json"}:
            raise ValueError("报告类型只能是md或json。")
        row = self._row(task_id)
        if not row.get("report_path"):
            raise KeyError("任务尚未生成报告。")
        path = Path(row["report_path"]).with_suffix("." + kind)
        if self.store:
            saved = self.store.report(task_id)
            raw = json.dumps(saved["json"], ensure_ascii=False) if kind == "json" else saved["markdown"]
        else:
            raw = self._read_report(row["report_root"], path)
        if kind == "json":
            raw = json.dumps(redact(json.loads(raw)), ensure_ascii=False, indent=2, allow_nan=False)
        else:
            raw = redact(raw)
        content = raw.encode("utf-8")
        if len(content) > MAX_REPORT:
            raise ValueError("报告超过8MiB限制。")
        return path.name, content

    def storage_status(self):
        queue_status = {"mode": self.queue_mode, "available": self.queue_mode == "local" or bool(self._publisher and self._publisher.available)}
        if not self.store:
            return {"mode": "legacy", "queue": queue_status, "java": {"available": False}, "sqlite": {"available": True, "owner": "python"}, "redis": {"available": False}, "reports": "files", "message": "当前为兼容模式。使用Java配置或Docker Compose启用Java SQLite与Redis报告存储。"}
        try:
            status = self.store.health()
            return {"mode": "java", "queue": queue_status, "java": {"available": True}, "sqlite": {"available": status.get("sqlite", {}).get("available", False), "owner": "java"}, "redis": status.get("redis", {"available": False}), "reports": "redis", "message": "业务存储由Java管理；Python保留Agent执行检查点。"}
        except RuntimeError:
            return {"mode": "java", "queue": queue_status, "java": {"available": False}, "sqlite": {"available": False, "owner": "java"}, "redis": {"available": False}, "reports": "redis", "message": "Java或Redis服务不可用，未降级为文件报告。"}

    def _memory_cases(self):
        db = self.ops_runtime / "memory.sqlite3"
        if not db.is_file():
            return []
        with closing(sqlite3.connect(db, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cases'").fetchone():
                return []
            return [dict(row, verified=bool(row["verified"]), source="ops", source_job_id=row["session_id"]) for row in connection.execute("SELECT * FROM cases ORDER BY created_at DESC LIMIT 500")]

    def list_cases(self):
        memories = self._memory_cases()
        if self.store:
            deleted = set(self.store.case_tombstones())
            existing = self.store.cases()
            known = {case["id"] for case in existing}
            for case in memories:
                if case["id"] not in deleted and case["id"] not in known:
                    self.store.add_case(case)
            return redact(self.store.cases())
        with self._connect() as connection:
            deleted = {row[0] for row in connection.execute("SELECT id FROM case_tombstones")}
        return redact([case for case in memories if case["id"] not in deleted])

    def _erase_memory_case(self, case_id):
        db = self.ops_runtime / "memory.sqlite3"
        if db.is_file():
            with closing(sqlite3.connect(db, timeout=10)) as connection, connection:
                connection.execute("PRAGMA secure_delete=ON")
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='practices'").fetchone():
                    connection.execute("DELETE FROM practices WHERE case_id=?", (case_id,))
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cases'").fetchone():
                    connection.execute("DELETE FROM cases WHERE id=?", (case_id,))

    def purge_case(self, case_id, payload):
        _identifier(case_id, "案例编号")
        if payload != {"confirmation": case_id}:
            raise ValueError("请输入完整案例编号确认永久删除；此操作不可恢复。")
        with self._lock:
            cases = self.list_cases()
            case = next((case for case in cases + self._memory_cases() if case["id"] == case_id), None)
            if not case:
                raise KeyError(case_id)
            source = case.get("source_job_id")
            if source and any(row["id"] == source and row["status"] in {"queued", "running"} for row in self._all_rows()):
                raise RuntimeError("关联任务仍在运行，请结束后再删除案例。")
            if self.store:
                self.store.purge_case(case_id)
            else:
                with self._connect() as connection:
                    connection.execute("INSERT OR IGNORE INTO case_tombstones VALUES(?)", (case_id,))
            self._erase_memory_case(case_id)
            return {"id": case_id, "permanently_deleted": True}

    def purge_task(self, task_id, payload):
        if payload != {"confirmation": task_id}:
            raise ValueError("请输入完整任务编号确认永久删除；此操作不可恢复。")
        with self._lock:
            _identifier(task_id)
            row = next((row for row in self._all_rows() if row["id"] == task_id), None)
            if row is None:
                raise KeyError(task_id)
            already_purged = bool(row.get("purged_at") or row.get("purged"))
            if not row.get("deleted_at") or row["status"] in {"queued", "running"}:
                raise RuntimeError("仅可永久删除回收站中已结束的任务。")
            path = Path(row["report_path"]) if row.get("report_path") else None
            root_name = row.get("report_root")
            if not path and row.get("source_key"):
                for name, report_root in self.roots.items():
                    for candidate in report_root.glob("*.json"):
                        if hashlib.sha256(str(candidate.resolve()).encode()).hexdigest() == row["source_key"]:
                            path, root_name = candidate, name
                            break
            # Validate every local target before making any destructive change.
            if path:
                root = self.roots[root_name].resolve()
                if path.resolve().parent != root:
                    raise ValueError("报告位置不属于固定目录。")
            inputs_root = (self.runtime_dir / "inputs").resolve()
            input_dir = (inputs_root / task_id).resolve()
            if input_dir.parent != inputs_root:
                raise ValueError("材料位置无效。")
            cases = {case["id"]: case for case in self.list_cases() + self._memory_cases() if case.get("source_job_id") == task_id}.values()
            if self.store and not already_purged:
                self.store.purge(task_id)
            elif not self.store and not already_purged:
                self._patch_job(task_id, {"purged_at": _now(), "title": "已永久删除", "params": "{}", "logs": "[]", "error": None, "report_path": None})
            for case in cases:
                if self.store:
                    self.store.purge_case(case["id"])
                else:
                    with self._connect() as connection:
                        connection.execute("INSERT OR IGNORE INTO case_tombstones VALUES(?)", (case["id"],))
                self._erase_memory_case(case["id"])
            if path and root_name != "ops_original":
                path.with_suffix(".md").unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
            if input_dir.exists():
                shutil.rmtree(input_dir)
            return {"id": task_id, "permanently_deleted": True, "external_source_retained": root_name == "ops_original"}

    def _changes(self):
        path = self.release_project / "data/fixtures.json"
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("变更文件过大。")
            raw = path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("变更文件过大。")
            data = json.loads(raw)
            labels = {"risk-pool": "连接池缩小与容量风险", "safe": "材料齐全的对照变更", "missing": "缺少测试与容量材料", "legacy-demo": "结合旧案例的演示变更"}
            result = []
            for change_id, change in data.get("changes", {}).items():
                _identifier(change_id, "变更编号")
                scope = change.get("scope", {})
                if scope.get("environment") not in {"demo", "staging"}:
                    continue
                result.append({"id": change_id, "label": labels.get(change_id, change_id),
                    "description": f"{scope.get('service', '')} / {scope.get('environment', '')}；固定模拟材料，未接入生产。"})
            return redact(result)
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    def _config_probe(self, agent):
        project = self.projects[agent]
        available = (project / "run.py").is_file() and Path(self.python[agent]).is_file()
        result = {"name": "IT故障诊断助手" if agent == "ops" else "发布风险评审助手", "available": available,
                  "api_key_configured": False, "model": "由原项目配置加载", "config_message": "项目不可用。"}
        if not available:
            return result
        command = [self.python[agent], "-B", str(project / "run.py"), "--check-config"] if agent == "ops" else [self.python[agent], "-B", str(project / "run.py"), "check-config", "--demo"]
        try:
            probe = subprocess.run(command, cwd=project, env=self._environment(), shell=False, capture_output=True,
                encoding="utf-8", errors="replace", timeout=15, **self._windows_options())
            parsed = json.loads(probe.stdout) if len(probe.stdout) < 65536 and probe.returncode == 0 else {}
            result.update(api_key_configured=parsed.get("api_key_configured") is True,
                          model=redact(str(parsed.get("model", "由原项目配置加载")))[:160],
                          config_message="本地配置检查通过；未发送模型请求。" if parsed else "API配置未通过检查；可先使用离线演示。")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result["config_message"] = "本地配置检查暂不可用；可先使用离线演示。"
        return result

    def bootstrap(self):
        with self._lock:
            if self._config_cache is None or time.monotonic() - self._config_checked > 60:
                self._config_cache = {agent: self._config_probe(agent) for agent in ("ops", "release")}
                self._config_checked = time.monotonic()
        profiles = {name: [public_profile(item) for item in scenarios(name, project)] for name, project in self.projects.items()}
        default_ops = profiles["ops"][0]["params"] if profiles["ops"] else dict(OPS_DEFAULTS)
        info = service_info(self.runtime_dir, self.storage_mode, self.queue_mode)
        info["capabilities"].update(permanent_delete=True, cases=True, java_storage=self.store is not None)
        return {**info, "agents": self._config_cache, "changes": self._changes(), "profiles": profiles,
                "defaults": {"ops": default_ops}, "limits": {"max_workers": 2, "max_pending_and_running": 20,
                    "max_material_bytes": MAX_MATERIAL_BYTES, "max_material_records": 500}}

    def get_material_template(self, agent, profile_id):
        if agent not in self.projects:
            raise ValueError("请选择有效的Agent。")
        _identifier(profile_id, "场景编号")
        return material_template(agent, self.projects[agent], profile_id)

    def create_task(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"agent", "mode", "params", "materials"}:
            raise ValueError("任务字段无效。")
        agent, mode, params = payload.get("agent"), payload.get("mode"), payload.get("params")
        if not isinstance(agent, str) or not isinstance(mode, str) or agent not in {"ops", "release"} or mode not in {"demo", "api"} or not isinstance(params, dict):
            raise ValueError("请选择有效的Agent和demo/api模式。")
        if not (self.projects[agent] / "run.py").is_file() or not Path(self.python[agent]).is_file():
            raise ValueError("Agent项目或解释器不可用。")
        choices = scenarios(agent, self.projects[agent])
        profile_id = params.get("profile_id")
        custom = profile_id == "custom"
        if custom != ("materials" in payload):
            raise ValueError("自填材料请使用自定义场景；预设场景不能悄悄替换材料。")
        if profile_id is not None:
            _identifier(profile_id, "场景编号")
        if profile_id is None and agent == "release":
            selected = next((item for item in choices if item["params"].get("change_id") == params.get("change_id")), None)
        else:
            selected = next((item for item in choices if item["id"] == profile_id), None) if profile_id else (choices[0] if choices else None)
        if profile_id and selected is None and not custom:
            raise ValueError("演示场景不存在，请刷新页面。")
        params = {key: value for key, value in params.items() if key != "profile_id"}
        if agent == "ops":
            if set(params) - (set(OPS_DEFAULTS) | {"workflow"}):
                raise ValueError("运维任务包含未知参数。")
            normalized = {**OPS_DEFAULTS, **(selected["params"] if selected else {}), **params}
            normalized["question"] = _text(normalized["question"], "问题", 16000)
            for key in ("service", "environment"):
                _identifier(normalized[key], key)
            for key in ("start", "end"):
                text = _text(normalized[key], key, 80)
                try:
                    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if value.tzinfo is None or value.utcoffset() is None:
                        raise ValueError()
                except ValueError:
                    raise ValueError("起止时间必须是带时区的ISO时间。") from None
                normalized[key] = text
            if datetime.fromisoformat(normalized["start"].replace("Z", "+00:00")) > datetime.fromisoformat(normalized["end"].replace("Z", "+00:00")):
                raise ValueError("开始时间不能晚于结束时间。")
            normalized["workflow"] = params.get("workflow", normalized.get("workflow", "single"))
            if not isinstance(normalized["workflow"], str) or normalized["workflow"] not in {"auto", "single", "swarm"}:
                raise ValueError("工作流模式无效。")
            title = normalized["question"][:100]
        else:
            allowed_release = {"change_id", "service", "environment"} if custom else {"change_id"}
            if set(params) != allowed_release:
                raise ValueError("自定义发布需填写变更、服务和环境；预设发布只接受变更编号。")
            change_id = _identifier(params["change_id"], "变更编号")
            if not custom and change_id not in {item["id"] for item in self._changes()}:
                raise ValueError("请选择预设变更材料。")
            if selected and change_id != selected["params"]["change_id"]:
                raise ValueError("变更与所选演示场景不匹配。")
            normalized, title = {"change_id": change_id}, "发布风险评审 · " + change_id
            if custom:
                normalized.update(service=_identifier(params["service"], "服务"), environment=_identifier(params["environment"], "环境"))
        custom_files = None
        if custom:
            custom_files, observed, source_label = prepare_materials(agent, payload["materials"], normalized)
        else:
            observed = coverage(agent, selected["data_dir"], normalized) if selected else None
        if observed is not None and not observed["current_count"]:
            raise ValueError("当前服务、环境和时间内没有演示资料，未发送模型请求。请使用所选场景的默认范围。")
        if selected and agent == "ops" and any(normalized[key] != selected["params"][key] for key in ("service", "environment")):
            raise ValueError("服务或环境不属于所选演示场景，请重新选择场景。")
        saved_params = dict(normalized)
        if selected:
            saved_params.update(_profile_id=selected["id"], _profile_label=selected["label"], _coverage=observed,
                                _source_label="模拟演示资料" if selected["source"] == "synthetic" else "本地资料",
                                _owner=selected["owner"])
        elif custom:
            saved_params.update(_profile_id="custom", _profile_label="自填材料", _coverage=observed,
                                _source_label=source_label)
        task_id = "task-" + uuid4().hex[:24]
        review_id = "web-" + uuid4().hex[:24] if agent == "release" else None
        with self._lock:
            if self._closed:
                raise RuntimeError("工作台正在关闭。")
            if sum(row["status"] in {"queued", "running"} for row in self._all_rows()) >= 20:
                raise RuntimeError("待运行任务较多，请稍后提交。")
            if self.store:
                health = self.store.health()
                if not health.get("redis", {}).get("available") or not health.get("sqlite", {}).get("available"):
                    raise RuntimeError("Java SQLite或Redis未就绪，未发送模型请求。")
            if selected or custom:
                destination = self.runtime_dir / "inputs" / task_id
                destination.mkdir(parents=True, exist_ok=False)
            if custom:
                for name, value in custom_files.items():
                    if name not in {"demo_data.json", "fixtures.json"}:
                        raise ValueError("自填材料的内部文件名无效。")
                    (destination / name).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            elif selected:
                names = ("demo_data.json",) if agent == "ops" else ("fixtures.json", "legacy_cases.json")
                for name in names:
                    path = selected["data_dir"] / name
                    if path.is_file():
                        if path.stat().st_size > 2_000_000:
                            raise ValueError("演示资料超过大小限制。")
                        shutil.copyfile(path, destination / name)
            job = dict(id=task_id, agent=agent, title=redact(title), mode=mode, status="queued", created_at=_now(),
                       updated_at=_now(), params=json.dumps(redact(saved_params), ensure_ascii=False), review_id=review_id)
            self._enqueue(task_id, {"action": "start", "params": normalized}, job=job)
        return self._summary(self._row(task_id))

    def _review_material(self, row, expected):
        data = self._report_data(row)
        if not data or data.get("status") != "waiting_human" or data.get("review_digest") != expected:
            raise RuntimeError("评审材料或状态已变化，请刷新报告后再复核。")
        review_id = _identifier(row.get("review_id"), "评审编号")
        db = self.release_runtime / "reviews.sqlite3"
        if not db.is_file():
            raise RuntimeError("缺少原发布评审数据库，不能只凭报告复核。")
        uri = db.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            record = connection.execute("SELECT change_id,status,result FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if record is None:
            raise RuntimeError("原发布评审记录不存在。")
        saved = json.loads(record[2]) if record[2] else {}
        if record[0] != data.get("change_id") or record[1] != "waiting_human" or saved.get("review_digest") != expected:
            raise RuntimeError("数据库评审材料或状态已变化，请刷新。")
        return data

    def submit_review(self, task_id, payload):
        if not isinstance(payload, dict) or set(payload) != {"reviewer", "decision", "comment", "review_digest"}:
            raise ValueError("请提供复核人、意见、备注和材料指纹。")
        reviewer = _text(payload["reviewer"], "复核人", 80)
        comment = _text(payload["comment"], "备注", 2000, nonempty=False)
        if not isinstance(payload["decision"], str) or payload["decision"] not in {"acknowledge", "request_changes", "reject"}:
            raise ValueError("复核只能确认已读、要求修改或拒绝；不能批准发布。")
        digest = payload["review_digest"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("评审材料指纹无效。")
        with self._lock:
            row = self._row(task_id)
            self._ensure_visible(row)
            if self._closed or row["status"] in {"queued", "running"}:
                raise RuntimeError("任务正在运行或工作台已关闭。")
            if row["agent"] != "release" or row["status"] != "waiting_human":
                raise ValueError("该任务没有待处理的发布人工复核。")
            self._review_material(row, digest)
            self._enqueue(task_id, {"action": "review", "reviewer": reviewer, "decision": payload["decision"],
                                    "comment": comment, "review_digest": digest})
        return self._summary(self._row(task_id))

    def _environment(self):
        result = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", LANGSMITH_TRACING="false")
        if self.ops_settings_file:
            result["OPS_SETTINGS_FILE"] = str(self.ops_settings_file)
        return result

    @staticmethod
    def _windows_options():
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def _command(self, row, action):
        agent = row["agent"]
        command = [self.python[agent], "-u", "-B", str(self.projects[agent] / "run.py")]
        data_dir = self.runtime_dir / "inputs" / row["id"]
        if not data_dir.is_dir():
            data_dir = self.projects[agent] / "data"
        if action["action"] == "review":
            command += ["resume", "--review-id=" + row["review_id"], "--decision=" + action["decision"],
                "--reviewer=" + action["reviewer"], "--comment=" + action["comment"], "--runtime-dir=" + str(self.release_runtime),
                "--data-dir=" + str(data_dir)]
        elif agent == "release":
            command += ["start", "--review-id=" + row["review_id"], "--change-id=" + action["params"]["change_id"],
                "--runtime-dir=" + str(self.release_runtime), "--data-dir=" + str(data_dir)]
            if row["mode"] == "demo": command += ["--demo"]
        else:
            params = action["params"]
            command += ["--runtime-dir=" + str(self.ops_runtime), "--session=" + row["id"], "--mode=" + params["workflow"]]
            command += ["--data-dir=" + str(data_dir)]
            if json.loads(row["params"]).get("_profile_id") == "custom":
                command += ["--local-input"]
            command += ["--" + key + "=" + params[key] for key in OPS_DEFAULTS]
            if row["mode"] == "demo": command += ["--demo"]
        return command

    def _append_logs(self, task_id, logs):
        bounded = [redact(str(line))[:2000] for line in logs[-MAX_LOGS:]]
        encoded = json.dumps(bounded, ensure_ascii=False)
        while len(encoded.encode("utf-8")) > 65536 and bounded:
            bounded.pop(0)
            encoded = json.dumps(bounded, ensure_ascii=False)
        self._update(task_id, logs=encoded)

    def _run_cli(self, task_id, command, cwd):
        chunks = queue.Queue(maxsize=64)
        process = subprocess.Popen(command, cwd=cwd, env=self._environment(), shell=False, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **self._windows_options())
        with self._lock:
            self._processes[task_id] = process
        def reader():
            try:
                while True:
                    value = process.stdout.readline(4096)
                    if not value: break
                    try: chunks.put(value, timeout=.5)
                    except queue.Full: pass
            finally:
                try: process.stdout.close()
                except OSError: pass
        reading = threading.Thread(target=reader, daemon=True)
        reading.start()
        logs, pending = [], ""
        begun, flushed = time.monotonic(), 0.0
        try:
            while reading.is_alive() or not chunks.empty():
                if self._closed or time.monotonic() - begun > 600:
                    process.kill()
                    raise RuntimeError("任务超过运行时间或工作台关闭，子进程已停止。")
                try:
                    pending += chunks.get(timeout=.15)
                except queue.Empty:
                    pass
                while "\n" in pending or len(pending) > 4000:
                    if "\n" in pending:
                        line, pending = pending.split("\n", 1)
                    else:
                        line, pending = pending[:4000], pending[4000:]
                    logs.append(redact(line.rstrip("\r")))
                    logs = logs[-MAX_LOGS:]
                if time.monotonic() - flushed > .4:
                    self._append_logs(task_id, logs)
                    flushed = time.monotonic()
            if pending: logs.append(redact(pending))
            self._append_logs(task_id, logs)
            return process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            with self._lock:
                self._processes.pop(task_id, None)

    def _find_output(self, row):
        root_name = "release" if row["agent"] == "release" else "ops_workbench"
        root = self.roots[root_name]
        candidates = [root / (row["review_id"] + ".json")] if row["agent"] == "release" else list(root.glob(row["id"] + "-*.json"))
        for path in sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            try:
                data = json.loads(self._read_report(root_name, path))
                if not isinstance(data, dict): continue
                if row["agent"] == "release" and data.get("review_id") != row["review_id"]: continue
                if row["agent"] == "ops" and data.get("session_id") != row["id"]: continue
                return root_name, str(path.resolve()), data
            except (OSError, ValueError):
                continue
        return None

    def _worker(self, task_id, action):
        try:
            if self._closed: return
            row = self._row(task_id)
            if action["action"] == "review":
                self._review_material(row, action["review_digest"])
            self._update(task_id, status="running", error=None)
            code = self._run_cli(task_id, self._command(row, action), self.projects[row["agent"]])
            output = self._find_output(row)
            if self._closed:
                self._update(task_id, status="interrupted", error="工作台已关闭；任务未自动重放。")
            elif output and code in (0, 2):
                root_name, path, data = output
                self._store_report(task_id, root_name, Path(path), data)
                status = self._business_status(row["agent"], data)
                saved = json.loads(row["params"])
                if row["agent"] == "ops" and (saved.get("_coverage") or {}).get("material_status") in {"partial", "empty"}:
                    status = "incomplete"
                self._update(task_id, status=status, report_root=root_name,
                             report_path=path, error=None)
                if self.store:
                    # Worker files are staging only; Redis is the report source.
                    Path(path).with_suffix(".md").unlink(missing_ok=True)
                    Path(path).unlink(missing_ok=True)
            else:
                self._update(task_id, status="failed", error="Agent未成功生成新报告，请查看已脱敏的运行日志。")
        except Exception as exc:
            safe = str(redact(str(exc)))[:250] if isinstance(exc, (ValueError, RuntimeError)) else "任务执行失败，请检查本地项目和运行环境。"
            self._update(task_id, status="interrupted" if self._closed else "failed", error=safe)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for process in list(self._processes.values()):
                if process.poll() is None:
                    process.terminate()
            # The worker owns running Kafka claims. A web restart preserves queued events.
            if self.queue_mode == "local" and self.store:
                self.store.recover("工作台关闭，任务未自动重放；请检查已有报告。", _now())
            elif self.queue_mode == "local":
                with self._connect() as connection:
                    connection.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')", ("工作台关闭，任务未自动重放；请检查已有报告。", _now()))
        if self._publisher:
            self._publisher.close()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
