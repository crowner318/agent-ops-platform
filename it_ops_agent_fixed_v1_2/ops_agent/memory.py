"""SQLite memory scoped by service/environment; confirmed cases are human curated.

L1 is a recent event window, L2 uses local word/character matching (not vectors),
and L3 practices must refer to an explicitly confirmed case. Only the CLI should
call ``confirm_case``; models are never given it as a tool.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .harness import redact


def _without_reasoning(value: Any, depth: int = 0) -> Any:
    """Produce JSON-safe data and discard private reasoning, including JSON strings."""
    if depth > 64:
        raise ValueError("记忆数据嵌套过深。")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("记忆对象的键必须是字符串。")
        return {
            key: _without_reasoning(item, depth + 1)
            for key, item in value.items()
            if key.casefold() != "reasoning_content"
        }
    if isinstance(value, (list, tuple)):
        return [_without_reasoning(item, depth + 1) for item in value]
    if isinstance(value, str):
        # Tool arguments and content often contain serialized JSON. Sanitize the
        # inner document as well, without changing non-JSON prose.
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return value
            return json.dumps(_without_reasoning(parsed, depth + 1), ensure_ascii=False,
                              allow_nan=False)
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    raise TypeError(f"记忆数据含非 JSON 类型：{type(value).__name__}")


def _clean(value: Any) -> Any:
    # Redact structured JSON before serializing it. Applying a prose credential
    # regex directly to JSON tool arguments could remove quotes around a value.
    normalized = _without_reasoning(value)

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            # Query the shared redactor's key policy with harmless None values;
            # preserve its recursive rules without reprocessing JSON strings.
            policy = redact(dict.fromkeys(item))
            return {key: policy[key] if policy[key] is not None else walk(child)
                    for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, str) and item.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(item)
            except (ValueError, RecursionError):
                return redact(item)
            return json.dumps(walk(parsed), ensure_ascii=False, allow_nan=False)
        return redact(item)

    return walk(normalized)


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空。")
    return str(_clean(value))


def _tokens(text: str) -> set[str]:
    lowered = text.casefold()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    for chunk in re.findall(r"[\u3400-\u9fff]+", lowered):
        words.update(chunk)
        words.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return words


def _rank(rows: list[dict], query: str, field: str, limit: int) -> list[dict]:
    terms = _tokens(query)
    if not terms or limit <= 0:
        return []
    scored = []
    for row in rows:
        body = row[field]
        matched = terms.intersection(_tokens(body))
        if not matched:
            continue
        score = len(matched) / len(terms)
        if query.casefold() in body.casefold():
            score += 1
        scored.append((score, row))
    # Rows already have newest-first order, so equal scores keep that order.
    scored.sort(key=lambda item: item[0], reverse=True)
    return [dict(row, relevance=round(score, 4)) for score, row in scored[:limit]]


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS events_scope
                    ON events(session_id, service, environment, id);
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    event_cutoff INTEGER NOT NULL DEFAULT 0,
                    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS cases_scope
                    ON cases(service, environment, verified, created_at);
                CREATE TABLE IF NOT EXISTS practices (
                    id TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    text TEXT NOT NULL,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS practices_scope
                    ON practices(service, environment, created_at);
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(cases)")}
            if "event_cutoff" not in columns:
                # Older drafts without a review boundary must not implicitly
                # approve any existing/future execution trajectories.
                connection.execute("ALTER TABLE cases ADD COLUMN event_cutoff INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # Each call gets its own connection, so parallel Swarm workers do not
        # share sqlite cursor/transaction state. SQLite serializes writes.
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _scope(service: str, environment: str) -> tuple[str, str]:
        return _text(service, "服务"), _text(environment, "环境")

    def add_event(self, session_id: str, service: str, environment: str,
                  kind: str, payload: dict) -> None:
        service, environment = self._scope(service, environment)
        if not isinstance(payload, dict):
            raise TypeError("事件 payload 必须是对象。")
        serialized = json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO events(session_id,service,environment,kind,payload) VALUES(?,?,?,?,?)",
                (_text(session_id, "会话"), service, environment, _text(kind, "事件类型"), serialized),
            )

    def recent(self, session_id: str, service: str, environment: str,
               limit: int = 8) -> list[dict]:
        service, environment = self._scope(service, environment)
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE session_id=? AND service=? AND environment=? "
                "ORDER BY id DESC LIMIT ?",
                (_text(session_id, "会话"), service, environment, int(limit)),
            ).fetchall()
        events = []
        for row in reversed(rows):
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            events.append(event)
        return events

    def save_case(self, session_id: str, service: str, environment: str,
                  summary: str, verified: bool = False) -> str:
        """Create a draft; confirmation is a separate human-only CLI action."""
        if verified:
            raise ValueError("案例只能通过人工 confirm_case 操作确认。")
        service, environment = self._scope(service, environment)
        case_id = "case-" + uuid4().hex
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO cases(id,session_id,service,environment,summary,event_cutoff) "
                "VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(id),0) FROM events "
                "WHERE session_id=? AND service=? AND environment=?))",
                (case_id, _text(session_id, "会话"), service, environment, _text(summary, "案例摘要"),
                 _text(session_id, "会话"), service, environment),
            )
        return case_id

    def recent_dialogue(self, session_id: str, service: str, environment: str,
                        limit: int = 8) -> list[dict]:
        """Return scoped Q/A history without tool-volume displacing conversation."""
        service, environment = self._scope(service, environment)
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE session_id=? AND service=? AND environment=? "
                "AND kind IN ('question','answer') ORDER BY id DESC LIMIT ?",
                (_text(session_id, "会话"), service, environment, int(limit)),
            ).fetchall()
        events = []
        for row in reversed(rows):
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            events.append(event)
        return events

    def confirm_case(self, case_id: str) -> bool:
        """Human CLI only: this method is deliberately absent from model tools."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE cases SET verified=1, "
                "confirmed_at=COALESCE(confirmed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE id=?",
                (case_id,),
            )
        return cursor.rowcount == 1

    def list_cases(self, service: str, environment: str) -> list[dict]:
        service, environment = self._scope(service, environment)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM cases WHERE service=? AND environment=? ORDER BY rowid DESC",
                (service, environment),
            ).fetchall()
        return [dict(row, verified=bool(row["verified"])) for row in rows]

    def search_cases(self, service: str, environment: str, query: str,
                     limit: int = 3) -> list[dict]:
        rows = [row for row in self.list_cases(service, environment) if row["verified"]]
        return _rank(rows, query, "summary", limit)

    def add_practice(self, service: str, environment: str, text: str, case_id: str) -> str:
        service, environment = self._scope(service, environment)
        practice_id = "practice-" + uuid4().hex
        clean_text = _text(text, "实践内容")
        with self._connection() as connection:
            # The checked INSERT is atomic with respect to the case scope and
            # confirmation state; unverified/cross-scope cases create no row.
            cursor = connection.execute(
                "INSERT INTO practices(id,service,environment,text,case_id) "
                "SELECT ?,?,?,?,id FROM cases "
                "WHERE id=? AND service=? AND environment=? AND verified=1",
                (practice_id, service, environment, clean_text, case_id, service, environment),
            )
            if cursor.rowcount != 1:
                raise ValueError("L3 实践必须引用同一服务、同一环境的人工已确认案例。")
        return practice_id

    def search_practices(self, service: str, environment: str, query: str,
                         limit: int = 3) -> list[dict]:
        service, environment = self._scope(service, environment)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT p.* FROM practices p JOIN cases c ON p.case_id=c.id "
                "WHERE p.service=? AND p.environment=? AND c.verified=1 "
                "AND c.service=p.service AND c.environment=p.environment ORDER BY p.rowid DESC",
                (service, environment),
            ).fetchall()
        return _rank([dict(row) for row in rows], query, "text", limit)

    def export_sft(self, path: Path) -> int:
        """Export complete API trajectories from human-confirmed sessions.

        The model-supplied ``payload.verified`` flag has no authority. The only
        approval signal is a confirmed database case with the same session AND
        service/environment, covering events already present when the case was
        saved. Future runs in that session require another review. Demo data is
        excluded. This exports candidate SFT
        examples; it does not train a model or establish training quality.
        A validated ``normalized_final`` replaces only the exported final
        assistant content; persisted provider messages remain the source record.
        Trajectories containing format/quality repair feedback are retained for
        manual curation, because their earlier assistant targets were rejected.
        """
        path = Path(path)
        # Refuse accidental overwrite of the source database or its sidecars.
        resolved = path.resolve()
        if resolved in {self.db_path.resolve(), Path(str(self.db_path) + "-wal").resolve(),
                        Path(str(self.db_path) + "-shm").resolve()}:
            raise ValueError("导出路径不能覆盖记忆数据库。")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT e.payload FROM events e WHERE e.kind='trajectory' AND EXISTS ("
                "SELECT 1 FROM cases c WHERE c.session_id=e.session_id AND c.service=e.service "
                "AND c.environment=e.environment AND c.verified=1 "
                "AND e.id<=c.event_cutoff) ORDER BY e.id",
            ).fetchall()
        samples = []
        for row in rows:
            payload = _clean(json.loads(row["payload"]))
            if payload.get("complete") is not True or payload.get("source") == "scripted_demo":
                continue
            protocol_events = payload.get("protocol_events", [])
            if isinstance(protocol_events, list) and any(
                isinstance(event, dict) and event.get("code") in {
                    "format_repair_requested", "quality_repair_requested"
                } for event in protocol_events
            ):
                # A corrected final answer does not turn the rejected earlier
                # assistant content into an appropriate automatic SFT target.
                continue
            messages = payload.get("messages")
            if not self._valid_messages(messages):
                continue
            if "normalized_final" in payload:
                normalized = payload["normalized_final"]
                if not isinstance(normalized, str):
                    continue
                try:
                    parsed = json.loads(normalized)
                except (ValueError, RecursionError):
                    continue
                # Reuse the report's authoritative four-key schema rather than
                # letting a model-authored normalization bypass output limits.
                from .reporting import FINAL_SCHEMA, _matches_schema
                if not _matches_schema(parsed, FINAL_SCHEMA):
                    continue
                messages = [dict(message) for message in messages]
                messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False, allow_nan=False)
            sample = {"messages": messages}
            tools = payload.get("tools")
            if isinstance(tools, list) and tools:
                sample["tools"] = tools
            samples.append(sample)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for sample in samples:
                stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
        return len(samples)

    @staticmethod
    def _valid_messages(messages: Any) -> bool:
        if not isinstance(messages, list) or not messages:
            return False
        pending: set[str] = set()
        used: set[str] = set()
        for message in messages:
            if not isinstance(message, dict):
                return False
            role = message.get("role")
            if not isinstance(role, str) or role not in {"system", "user", "assistant", "tool"}:
                return False
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                return False
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    return False
                pending.remove(call_id)
            elif pending:
                return False
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or (calls and role != "assistant"):
                return False
            for call in calls:
                if not isinstance(call, dict):
                    return False
                call_id, function = call.get("id"), call.get("function")
                if (not isinstance(call_id, str) or not call_id or call_id in used
                        or call.get("type") != "function" or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)):
                    return False
                try:
                    if not isinstance(json.loads(function["arguments"]), dict):
                        return False
                except (ValueError, RecursionError):
                    return False
                used.add(call_id)
                pending.add(call_id)
        final = messages[-1]
        return (not pending and final.get("role") == "assistant"
                and isinstance(final.get("content"), str) and bool(final["content"].strip())
                and not final.get("tool_calls"))
