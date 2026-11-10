"""Bound local release snapshots: no model-controlled file, URL, command or scope."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

tool_names = (
    "get_release_snapshot", "get_config_diff", "get_code_diff", "get_ci_checks",
    "get_dependency_budget", "search_runbooks", "search_incident_cases",
)
TOOL_KINDS = dict(zip(tool_names, (
    "release_snapshot", "config_diff", "code_diff", "ci_checks",
    "dependency_budget", "runbook", "incident_case",
)))
QUERY_TOOLS = frozenset(tool_names[-2:])
MAX_RESULT_CHARS = 120_000


def _scope_identifier(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None


def _nonfinite(_):
    raise ValueError("local material must contain finite JSON numbers")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        _nonfinite(value)
    return number


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must include a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class EvidenceStore:
    """One immutable-in-practice data snapshot per server/client lifetime."""

    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir).resolve()
        fixture_bytes = (data_dir / "fixtures.json").read_bytes()
        if len(fixture_bytes) > 2_000_000:
            raise ValueError("fixtures file exceeds size limit")
        legacy_path = data_dir / "legacy_cases.json"
        legacy_bytes = legacy_path.read_bytes() if legacy_path.exists() else b"[]"
        if len(legacy_bytes) > 2_000_000:
            raise ValueError("legacy cases exceed size limit")
        self.content_hash = hashlib.sha256(fixture_bytes + b"\0legacy\0" + legacy_bytes).hexdigest()
        payload = json.loads(fixture_bytes, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite, parse_float=_finite_float)
        if not isinstance(payload, dict) or set(payload) != {"snapshot_id", "changes"}:
            raise ValueError("fixtures require snapshot_id and changes")
        if not isinstance(payload["snapshot_id"], str) or not payload["snapshot_id"]:
            raise ValueError("snapshot_id must be a nonempty string")
        self.snapshot_id = payload["snapshot_id"]
        if not isinstance(payload["changes"], dict) or not payload["changes"]:
            raise ValueError("changes must be a nonempty mapping")
        self._changes = deepcopy(payload["changes"])
        seen = set()
        for change_id, change in self._changes.items():
            if not isinstance(change, dict) or set(change) != {"scope", "as_of", "evidence"}:
                raise ValueError("invalid change snapshot")
            scope = change["scope"]
            if (not isinstance(scope, dict) or set(scope) != {"change_id", "service", "environment", "snapshot_id"}
                    or scope["change_id"] != change_id or scope["snapshot_id"] != self.snapshot_id
                    or any(not _scope_identifier(scope[key]) for key in ("change_id", "service", "environment"))):
                raise ValueError("invalid scope: change, service and environment require 1-80 letters, numbers, underscores or hyphens")
            timestamp(change["as_of"])
            groups = change["evidence"]
            if not isinstance(groups, dict) or set(groups) - set(TOOL_KINDS.values()):
                raise ValueError("unknown evidence collection")
            for kind, rows in groups.items():
                if not isinstance(rows, list):
                    raise ValueError("evidence collections must be lists")
                for row in rows:
                    self._validate_record(row, kind)
                    if row["id"] in seen:
                        raise ValueError("duplicate evidence ID")
                    seen.add(row["id"])
                    if kind not in {"runbook", "incident_case"} and any(row[key] != scope[key] for key in ("change_id", "service", "environment")):
                        raise ValueError("telemetry evidence does not match its fixed scope")
        legacy = json.loads(legacy_bytes, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite, parse_float=_finite_float)
        if not isinstance(legacy, list) or any(not isinstance(row, dict) for row in legacy):
            raise ValueError("legacy_cases.json must be an array of case objects")
        self._legacy = deepcopy(legacy)
        self._all_ids = frozenset(seen)

    @staticmethod
    def _validate_record(row: Any, kind: str) -> None:
        required = {"id", "kind", "change_id", "service", "environment", "timestamp", "synthetic", "content"}
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("invalid evidence record fields")
        if any(not isinstance(row[key], str) or not row[key] for key in ("id", "change_id", "service", "environment")):
            raise ValueError("evidence identity fields must be nonempty strings")
        if row["kind"] != kind or any(not _scope_identifier(row[key]) for key in ("change_id", "service", "environment")):
            raise ValueError("invalid evidence kind or scope identifier")
        if type(row["synthetic"]) is not bool or not isinstance(row["content"], dict):
            raise ValueError("local evidence requires an explicit synthetic boolean and object content")
        timestamp(row["timestamp"])

    def scope(self, change_id: str) -> dict:
        if not isinstance(change_id, str) or change_id not in self._changes:
            raise ValueError("unknown change_id; available local snapshots only")
        return deepcopy(self._changes[change_id]["scope"])

    def change_ids(self) -> tuple[str, ...]:
        return tuple(self._changes)

    @staticmethod
    def validate_arguments(name: str, arguments: Any) -> None:
        if name not in tool_names:
            raise ValueError("unknown tool")
        expected = {"change_id", "query"} if name in QUERY_TOOLS else {"change_id"}
        if not isinstance(arguments, dict) or set(arguments) != expected:
            raise ValueError("tool arguments must match the exact declared fields")
        for key, value in arguments.items():
            if not isinstance(value, str) or not value.strip() or len(value) > (2000 if key == "query" else 128):
                raise ValueError("invalid or oversized tool argument")
        if len(json.dumps(arguments, ensure_ascii=False)) > 8000:
            raise ValueError("tool arguments exceed size limit")

    def _knowledge(self, rows: list[dict], change: dict, kind: str, query: str) -> list[dict]:
        scope, as_of = change["scope"], timestamp(change["as_of"])
        terms = query.casefold().split()
        result = []
        for row in rows:
            if any(row[key] != scope[key] for key in ("service", "environment")):
                continue
            content = row["content"]
            if kind == "runbook":
                try:
                    valid_from, valid_until = timestamp(content.get("valid_from")), timestamp(content.get("valid_until"))
                except ValueError:
                    continue
                if not valid_from <= as_of <= valid_until:
                    continue
            elif content.get("verified") is not True:
                continue
            if not any(term in json.dumps(content, ensure_ascii=False).casefold() for term in terms):
                continue
            bound = deepcopy(row)
            # Knowledge comes from a past record but is cited for this review.
            bound["change_id"] = scope["change_id"]
            result.append(bound)
        return result

    def _legacy_rows(self, change: dict) -> list[dict]:
        scope = change["scope"]
        rows = []
        for case in self._legacy:
            if (case.get("verified") is not True or case.get("service") != scope["service"]
                    or case.get("environment") != scope["environment"]):
                continue
            source_id = case.get("id")
            summary = case.get("summary")
            if not isinstance(source_id, str) or not isinstance(summary, str) or not summary.strip():
                continue
            row_id = "legacy-" + hashlib.sha256(source_id.encode()).hexdigest()[:20]
            if row_id in self._all_ids or any(row["id"] == row_id for row in rows):
                continue
            # Import time is metadata, not the current incident observation time.
            imported = case.get("imported_at", change["as_of"])
            try:
                timestamp(imported)
            except ValueError:
                continue
            content = {key: deepcopy(case[key]) for key in (
                "summary", "verified", "original_session_id", "source_type", "report_complete",
                "imported_at", "source_digest", "synthetic",
            ) if key in case}
            content["source_id"] = source_id
            rows.append({"id": row_id, "kind": "incident_case", "change_id": scope["change_id"],
                         "service": scope["service"], "environment": scope["environment"],
                         "timestamp": imported, "synthetic": case.get("synthetic") is True, "content": content})
        return rows

    def execute(self, name: str, arguments: dict) -> dict:
        scope = {}
        try:
            self.validate_arguments(name, arguments)
            scope = self.scope(arguments["change_id"])
            change = self._changes[scope["change_id"]]
            kind = TOOL_KINDS[name]
            rows = deepcopy(change["evidence"].get(kind, []))
            if name in QUERY_TOOLS:
                if kind == "incident_case":
                    rows.extend(self._legacy_rows(change))
                rows = self._knowledge(rows, change, kind, arguments["query"])
            result = {"ok": True, "scope": scope, "evidence": rows,
                      "data": {"kind": kind, "count": len(rows), "status": "available" if rows else "missing",
                               "snapshot_hash": self.content_hash}}
            if len(json.dumps(result, ensure_ascii=False)) > MAX_RESULT_CHARS:
                raise ValueError("tool result exceeds size limit")
            return deepcopy(result)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return {"ok": False, "scope": scope, "evidence": [], "data": None, "error": str(exc)}
