"""SQLite business records; human observations never grant release permission."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


def sanitize(value, _depth=0):
    """Best-effort recursive credential filtering, not a source DLP system."""
    if _depth > 40:
        raise ValueError("记录嵌套层数过多。")
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("记录字段名必须为字符串。")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in {"reasoningcontent", "reasoning"}:
                continue
            secret = normalized in {"apikey", "token", "password", "passwd", "authorization", "secret"}
            secret = secret or normalized.endswith(("apikey", "password", "secret", "accesstoken", "refreshtoken"))
            result[key] = "[REDACTED]" if secret else sanitize(child, _depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(child, _depth + 1) for child in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(sanitize(json.loads(value), _depth + 1), ensure_ascii=False, allow_nan=False)
            except (ValueError, RecursionError):
                pass
        value = re.sub(r"(?i)\bBearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
        value = re.sub(r"(?i)(rediss?://)[^/@\s]+@", r"\1[REDACTED]@", value)
        return re.sub(r"(?i)(\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|token)\s*[:=]\s*)"
                      r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)", r"\1[REDACTED]", value)
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("记录必须只含有限的 JSON 数据。")


def _json(value):
    if not isinstance(value, dict):
        raise ValueError("记录必须是 JSON 对象。")
    encoded = json.dumps(sanitize(value), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise ValueError("记录超过 1 MiB 上限。")
    return encoded


def _name(value, label, limit=128):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.strip():
        raise ValueError(f"{label}必须是长度不超过{limit}的非空字符串。")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{label}包含非法控制字符。")
    return str(sanitize(value))


def _now():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reviews(
                    review_id TEXT PRIMARY KEY, change_id TEXT NOT NULL,
                    mode TEXT NOT NULL, coordination TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created', result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id TEXT NOT NULL REFERENCES reviews(review_id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_review ON events(review_id,id);
                CREATE TABLE IF NOT EXISTS human_reviews(
                    id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(review_id),
                    reviewer TEXT NOT NULL, decision TEXT NOT NULL, proposed_digest TEXT NOT NULL,
                    comment TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS human_review_scope ON human_reviews(review_id,created_at);
                CREATE TRIGGER IF NOT EXISTS human_reviews_no_update BEFORE UPDATE ON human_reviews
                    BEGIN SELECT RAISE(ABORT,'human reviews are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS human_reviews_no_delete BEFORE DELETE ON human_reviews
                    BEGIN SELECT RAISE(ABORT,'human reviews are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS review_binding_immutable
                    BEFORE UPDATE OF change_id,mode,coordination ON reviews
                    BEGIN SELECT RAISE(ABORT,'review binding is immutable'); END;
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_review(self, review_id, change_id, mode, coordination) -> bool:
        values = [_name(value, label) for value, label in (
            (review_id, "评审ID"), (change_id, "变更ID"), (mode, "模式"), (coordination, "协调方式"))]
        now = _now()
        with self._connection() as db:
            cursor = db.execute("INSERT OR IGNORE INTO reviews(review_id,change_id,mode,coordination,created_at,updated_at) "
                                "VALUES(?,?,?,?,?,?)", (*values, now, now))
        return cursor.rowcount == 1

    def append_event(self, review_id, kind, payload):
        kind = _name(kind, "事件类型", 64)
        if kind.casefold().startswith("human") or kind in {"review_record", "approval"}:
            raise ValueError("人工审核记录必须通过显式人工入口追加，不能由模型事件代替。")
        with self._connection() as db:
            db.execute("INSERT INTO events(review_id,kind,payload,created_at) VALUES(?,?,?,?)",
                       (review_id, kind, _json(payload), _now()))

    def save_result(self, review_id, payload):
        encoded = _json(payload)
        status = _name(payload.get("status", "completed"), "状态", 64)
        if status.casefold() in {"approved", "approve", "deployed", "released", "release_approved"}:
            raise ValueError("评审记录不能表示生产发布已获批准或已执行。")
        with self._connection() as db:
            cursor = db.execute("UPDATE reviews SET result=?,status=?,updated_at=? WHERE review_id=?",
                                (encoded, status, _now(), review_id))
            if not cursor.rowcount:
                raise ValueError("评审记录不存在。")

    def append_human_review(self, review_id, reviewer, decision, proposed_digest, comment="", *, record_id=None) -> dict:
        """Explicit human CLI only; reviewer is self-declared, not enterprise SSO."""
        reviewer = _name(reviewer, "审核人", 120)
        if decision not in {"acknowledge", "reject", "request_changes"}:
            raise ValueError("人工意见只能为 acknowledge/reject/request_changes；不构成发布授权。")
        if not isinstance(proposed_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", proposed_digest):
            raise ValueError("人工意见必须绑定64位SHA256证据与建议摘要。")
        if not isinstance(comment, str) or len(comment) > 4000:
            raise ValueError("审核备注不能超过4000字符。")
        if record_id is not None:
            record_id = _name(record_id, "人工记录ID", 128)
        record = {"id": record_id or "hr-" + uuid4().hex, "review_id": review_id,
                  "reviewer": reviewer, "decision": decision, "proposed_digest": proposed_digest.lower(),
                  "comment": sanitize(comment), "created_at": _now()}
        with self._connection() as db:
            review = db.execute("SELECT result FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if review is None:
                raise ValueError("评审记录不存在。")
            previous = json.loads(review["result"]) if review["result"] else {}
            expected = previous.get("review_digest", previous.get("proposed_digest"))
            if expected and expected != record["proposed_digest"]:
                raise ValueError("人工意见对应的证据摘要已发生变化，请重新查看评审。")
            db.execute("INSERT OR IGNORE INTO human_reviews(id,review_id,reviewer,decision,proposed_digest,comment,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", tuple(record[key] for key in (
                           "id", "review_id", "reviewer", "decision", "proposed_digest", "comment", "created_at")))
            stored = dict(db.execute("SELECT * FROM human_reviews WHERE id=?", (record["id"],)).fetchone())
            fields = ("review_id", "reviewer", "decision", "proposed_digest", "comment")
            if any(stored[field] != record[field] for field in fields):
                raise ValueError("人工记录ID已绑定不同的评审或意见，拒绝覆盖。")
        return stored

    def get_review(self, review_id) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if row is None:
                return None
            review = dict(row)
            review["result"] = json.loads(review["result"]) if review["result"] else None
            review["events"] = [dict(item, payload=json.loads(item["payload"])) for item in db.execute(
                "SELECT * FROM events WHERE review_id=? ORDER BY id", (review_id,))]
            review["human_reviews"] = [dict(item) for item in db.execute(
                "SELECT * FROM human_reviews WHERE review_id=? ORDER BY rowid", (review_id,))]
        return review

    def list_reviews(self) -> list:
        with self._connection() as db:
            rows = db.execute("SELECT review_id,change_id,mode,coordination,status,created_at,updated_at "
                              "FROM reviews ORDER BY rowid DESC").fetchall()
        return [dict(row) for row in rows]
