"""Read-only incident investigation skills backed by explicit fixture records.

No skill in this module executes a shell command, changes infrastructure, or
claims that a proposed remediation has been performed.
"""

from __future__ import annotations

from copy import deepcopy
from concurrent.futures import Future
from datetime import datetime
import json
from pathlib import Path
import threading
from time import perf_counter
from typing import Any
from .materials import missing_materials, summarize_materials


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("time must be an ISO 8601 string with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("time must be an ISO 8601 string with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must include a timezone")
    return parsed


_SPECS = {
    "collect_evidence": ("Collect selected evidence categories in one call; returns deduplicated evidence and compact summaries. Query applies only to runbooks; logs are collected without a keyword filter.",
                         {"kinds": {"type": "array", "items": {"type": "string", "enum": ["metrics", "logs", "traces", "changes", "runbooks"]}, "minItems": 1, "maxItems": 5, "uniqueItems": True},
                          "query": {"type": "string", "default": "timeout connection pool latency"}}, ["kinds"]),
    "query_metrics": ("Read metrics for the bound service, environment and incident window.",
                      {"metric": {"type": "string", "description": "Metric name or all (default)."}}, []),
    "search_logs": ("Read logs for the bound incident scope; optional case-insensitive keyword.",
                    {"keyword": {"type": "string"}}, []),
    "analyze_traces": ("Read traces and summarize latency/errors within the bound incident window.", {}, []),
    "get_changes": ("Read deployment and configuration changes within the bound incident window.", {}, []),
    "search_runbooks": ("Find service runbooks valid at the incident end time. Historical knowledge is not current telemetry.",
                        {"query": {"type": "string"}}, ["query"]),
    "assess_remediation": ("Assess a proposed action using evidence already returned in this run. Produces a plan only; requires human approval for production writes.",
                           {"action": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                           ["action", "evidence_ids"]),
    "build_postmortem": ("Create a postmortem draft from evidence already returned. A draft does not verify a root cause or recovery.",
                         {"summary": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                         ["summary", "evidence_ids"]),
}

_COLLECT_TOOLS = {
    "metrics": "query_metrics", "logs": "search_logs", "traces": "analyze_traces",
    "changes": "get_changes", "runbooks": "search_runbooks",
}
_DEFAULT_QUERY = "timeout connection pool latency"


class SkillRegistry:
    """A per-run, thread-safe registry with a fixed incident scope.

    The fixture must be named ``demo_data.json``. All evidence IDs are unique
    across its collections. ``observed`` returns a snapshot, never a mutable set.
    """

    def __init__(self, data_dir: Path, service: str, environment: str,
                 start_time: str, end_time: str):
        if not isinstance(service, str) or not service.strip():
            raise ValueError("service must be a non-empty string")
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError("environment must be a non-empty string")
        self.service, self.environment = service, environment
        self.start_time, self.end_time = start_time, end_time
        self._start, self._end = _time(start_time), _time(end_time)
        if self._start > self._end:
            raise ValueError("start_time must not be after end_time")
        self._lock = threading.RLock()
        self._observed: set[str] = set()
        self._cache: dict[tuple, dict] = {}
        self._inflight: dict[tuple, Future] = {}
        self._cache_counts = {"backend_executions": 0, "cache_hits": 0, "cache_misses": 0}
        payload = json.loads((Path(data_dir) / "demo_data.json").read_text(encoding="utf-8"))
        self._records: dict[str, tuple[dict, ...]] = {}
        self._by_id: dict[str, dict] = {}
        for collection in ("metrics", "logs", "traces", "changes", "runbooks"):
            rows = payload.get(collection, [])
            if not isinstance(rows, list):
                raise ValueError(f"{collection} must be a list")
            validated = []
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError(f"{collection} records must be objects")
                row = deepcopy(item)
                for key in ("id", "service", "environment", "timestamp"):
                    if not isinstance(row.get(key), str) or not row[key]:
                        raise ValueError(f"record requires a non-empty {key}")
                if row["id"] in self._by_id:
                    raise ValueError(f"duplicate evidence ID: {row['id']}")
                _time(row["timestamp"])
                if collection == "runbooks":
                    if _time(row["valid_from"]) > _time(row["valid_until"]):
                        raise ValueError("invalid runbook validity interval")
                    row["evidence_type"] = "historical_knowledge"
                    row["current_telemetry"] = False
                else:
                    row["evidence_type"] = "current_telemetry"
                    row["current_telemetry"] = True
                validated.append(row)
                self._by_id[row["id"]] = row
            self._records[collection] = tuple(validated)

    @property
    def observed(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._observed)

    def observed_evidence(self) -> list[dict]:
        """Return trusted records actually observed in this run.

        Context messages may supply IDs but must never supply their canonical
        contents. This snapshot can be used to replace untrusted context rows.
        """
        with self._lock:
            return deepcopy([self._by_id[eid] for eid in sorted(self._observed)])

    def schemas(self, allowed_names: list[str] | None = None) -> list[dict]:
        names = list(_SPECS) if allowed_names is None else allowed_names
        if not isinstance(names, list) or any(not isinstance(n, str) or n not in _SPECS for n in names):
            raise ValueError("allowed_names contains an unknown skill")
        return [{"type": "function", "function": {
            "name": name, "description": _SPECS[name][0],
            "parameters": {"type": "object", "properties": deepcopy(_SPECS[name][1]),
                           "required": list(_SPECS[name][2]), "additionalProperties": False},
        }} for name in dict.fromkeys(names)]

    def execute(self, name: str, arguments: dict) -> dict:
        started = perf_counter()
        source = name if isinstance(name, str) else "unknown"
        cache_hit = False
        try:
            if not isinstance(name, str) or name not in _SPECS:
                raise ValueError("unknown skill")
            self._validate(name, arguments)
            result, cache_hit = self._cached_execute(name, self._normalized_arguments(name, arguments))
        except Exception as exc:
            result = {"ok": False, "source": source, "evidence": [], "data": None,
                      "error": str(exc)}
        result = deepcopy(result)
        result["cache_hit"] = cache_hit
        result["duration_ms"] = round((perf_counter() - started) * 1000, 3)
        return result

    def cache_stats(self) -> dict:
        """Per-registry counters for leaf tools; collection wrappers are excluded."""
        with self._lock:
            return dict(self._cache_counts)

    @staticmethod
    def _normalized_arguments(name: str, arguments: dict) -> dict:
        normalized = deepcopy(arguments)
        if name == "query_metrics":
            normalized.setdefault("metric", "all")
        elif name == "search_logs":
            normalized.setdefault("keyword", "")
        elif name == "collect_evidence":
            normalized["kinds"] = [kind for kind in _COLLECT_TOOLS if kind in normalized["kinds"]]
            normalized.setdefault("query", _DEFAULT_QUERY)
        return normalized

    def _cached_execute(self, name: str, arguments: dict) -> tuple[dict, bool]:
        key = (self.service, self.environment, self.start_time, self.end_time,
               name, json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        leaf = name != "collect_evidence"
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                if leaf:
                    self._cache_counts["cache_hits"] += 1
                return cached, True
            pending = self._inflight.get(key)
            if pending is not None:
                if leaf:
                    self._cache_counts["cache_hits"] += 1
                owner = False
            else:
                pending = Future()
                self._inflight[key] = pending
                owner = True
                if leaf:
                    self._cache_counts["cache_misses"] += 1
                    self._cache_counts["backend_executions"] += 1
        if not owner:
            return pending.result(), True
        try:
            result = getattr(self, "_" + name)(**arguments)
            with self._lock:
                self._observed.update(item["id"] for item in result["evidence"])
        except Exception as exc:
            result = {"ok": False, "source": name, "evidence": [], "data": None, "error": str(exc)}
        # Publish a detached result. Failures wake current waiters but are never
        # retained, so a retry after evidence collection or transient IO can work.
        result = deepcopy(result)
        with self._lock:
            if result.get("ok"):
                self._cache[key] = result
            pending.set_result(result)
            self._inflight.pop(key, None)
        return result, False

    def _validate(self, name: str, arguments: dict) -> None:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        properties, required = _SPECS[name][1:]
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ValueError("unknown arguments: " + ", ".join(sorted(map(str, unknown))))
        missing = set(required) - set(arguments)
        if missing:
            raise ValueError("missing arguments: " + ", ".join(sorted(missing)))
        for key, value in arguments.items():
            if properties[key]["type"] == "string":
                if not isinstance(value, str) or len(value) > 8000:
                    raise ValueError(f"{key} must be a string of at most 8000 characters")
                if key in ("action", "summary", "query") and not value.strip():
                    raise ValueError(f"{key} must not be empty")
            elif key == "kinds":
                if (not isinstance(value, list) or not 1 <= len(value) <= 5
                        or any(not isinstance(kind, str) or kind not in _COLLECT_TOOLS for kind in value)
                        or len(set(value)) != len(value)):
                    raise ValueError("kinds must contain 1-5 unique entries from metrics/logs/traces/changes/runbooks")
            elif (not isinstance(value, list) or not value or len(value) > 100
                  or any(not isinstance(i, str) or not i for i in value)):
                raise ValueError(f"{key} must be a non-empty list of evidence ID strings (maximum 100)")

    def _scope(self) -> dict:
        return {"service": self.service, "environment": self.environment,
                "start_time": self.start_time, "end_time": self.end_time,
                "read_only": True}

    def _result(self, source: str, rows: list[dict], data: dict) -> dict:
        return {"ok": True, "source": source, "evidence": rows,
                "scope": self._scope(), "data": data}

    def _telemetry(self, collection: str) -> list[dict]:
        return [row for row in self._records[collection]
                if row["service"] == self.service and row["environment"] == self.environment
                and self._start <= _time(row["timestamp"]) <= self._end]

    def _collect_evidence(self, kinds: list[str], query: str = _DEFAULT_QUERY) -> dict:
        evidence = {}
        summaries = {}
        errors = []
        for kind in kinds:
            result = self.execute(_COLLECT_TOOLS[kind], {"query": query} if kind == "runbooks" else {})
            for row in result["evidence"]:
                evidence[row["id"]] = row
            data = result.get("data") or {}
            summary = {"source": _COLLECT_TOOLS[kind], "count": len(result["evidence"]),
                       "status": data.get("status", "error" if not result["ok"] else "observed")}
            if kind == "metrics":
                summary["metrics"] = [{key: metric[key] for key in ("metric", "unit", "min", "max")}
                                      for metric in data.get("metrics", [])]
            for key in ("error_count", "max_duration_ms", "interpretation", "causality", "knowledge_as_of", "notice"):
                if key in data:
                    summary[key] = data[key]
            summaries[kind] = summary
            if not result["ok"]:
                errors.append({"kind": kind, "error": result.get("error", "collection failed")})
        result = self._result("collect_evidence", list(evidence.values()), {
            "kinds": kinds, "evidence_count": len(evidence), "by_kind": summaries,
            "errors": errors, "status": "partial_failure" if errors else ("observed" if evidence else "no_data"),
        })
        result["ok"] = not errors
        return result

    def _query_metrics(self, metric: str = "all") -> dict:
        rows = [r for r in self._telemetry("metrics") if metric == "all" or r["metric"] == metric]
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(row["metric"], []).append(row)
        return self._result("query_metrics", rows, {
            "count": len(rows), "metrics": [{"metric": key, "unit": values[0]["unit"],
                "min": min(v["value"] for v in values), "max": max(v["value"] for v in values),
                "samples": [{"id": v["id"], "timestamp": v["timestamp"], "value": v["value"]} for v in values]}
                for key, values in groups.items()],
            "status": "observed" if rows else "no_data",
        })

    def _search_logs(self, keyword: str = "") -> dict:
        rows = [r for r in self._telemetry("logs")
                if keyword.casefold() in (r["message"] + " " + r.get("level", "")).casefold()]
        return self._result("search_logs", rows, {"count": len(rows), "logs": rows,
            "status": "observed" if rows else "no_data"})

    def _analyze_traces(self) -> dict:
        rows = self._telemetry("traces")
        durations = [r["duration_ms"] for r in rows]
        return self._result("analyze_traces", rows, {"count": len(rows), "traces": rows,
            "error_count": sum(r.get("status") == "error" for r in rows),
            "max_duration_ms": max(durations) if durations else None,
            "interpretation": "Observations only; sampled traces cannot establish causal certainty.",
            "status": "observed" if rows else "no_data"})

    def _get_changes(self) -> dict:
        rows = self._telemetry("changes")
        return self._result("get_changes", rows, {"count": len(rows), "changes": rows,
            "causality": "Temporal correlation is not proof of root cause.",
            "status": "observed" if rows else "no_data"})

    def _search_runbooks(self, query: str) -> dict:
        terms = query.casefold().split()
        rows = [r for r in self._records["runbooks"]
                if r["service"] == self.service and r["environment"] in (self.environment, "*")
                and _time(r["valid_from"]) <= self._end <= _time(r["valid_until"])
                and any(term in (r["title"] + " " + " ".join(r["keywords"]) + " " + r["content"]).casefold()
                        for term in terms)]
        return self._result("search_runbooks", rows, {"count": len(rows), "runbooks": rows,
            "knowledge_as_of": self.end_time,
            "notice": "Historical reference only; runbooks do not prove the current incident cause.",
            "status": "observed" if rows else "no_data"})

    def _cited(self, evidence_ids: list[str]) -> list[dict]:
        with self._lock:
            missing = set(evidence_ids) - self._observed
            if missing:
                raise ValueError("unobserved evidence IDs: " + ", ".join(sorted(missing)))
            return [self._by_id[eid] for eid in dict.fromkeys(evidence_ids)]

    def _assess_remediation(self, action: str, evidence_ids: list[str]) -> dict:
        rows = self._cited(evidence_ids)
        lowered = action.casefold()
        if any(word in lowered for word in ("rollback", "revert", "回滚")):
            category, risk = "rollback", "high"
            checks = ["Confirm the deployment/configuration diff and a known-good revision.",
                      "Check backward compatibility of database/schema and dependent services.",
                      "Save the current revision and define rollback/abort thresholds."]
        elif any(word in lowered for word in ("pool", "connection", "连接池", "连接数")):
            category, risk = "connection_pool_change", "high"
            checks = ["Verify the effective pool configuration on running instances.",
                      "Measure active connections, waiters and database connection headroom.",
                      "Calculate fleet-wide connection limits before proposing a pool size."]
        elif any(word in lowered for word in ("restart", "重启", "scale", "扩容", "缩容")):
            category, risk = "instance_change", "high"
            checks = ["Check redundancy, request draining and maintenance/change windows.",
                      "Check downstream capacity and resource quotas.",
                      "Define a canary and rollback thresholds."]
        else:
            category, risk = "unclassified_proposal", "unknown"
            checks = ["A qualified operator must classify the action and its blast radius.",
                      "Resolve missing evidence before approving any production change."]
        return self._result("assess_remediation", rows, {
            "action": action, "category": category, "risk": risk,
            "execution_status": "proposal_only", "executed": False,
            "human_approval_required": True, "approval_granted": False,
            "prerequisites": checks,
            "change_controls": ["Create an approved change ticket and identify an accountable operator.",
                                "Record a recovery plan, canary scope and measurable abort thresholds.",
                                "Verify service error rate, latency and database load after any separately authorized change."],
            "recovery_verified": False,
            "evidence_ids": [r["id"] for r in rows],
            "limitations": ["The registry cannot execute production writes.",
                            "Referenced evidence supports review; it does not automatically authorize a change."],
        })

    def _build_postmortem(self, summary: str, evidence_ids: list[str]) -> dict:
        rows = self._cited(evidence_ids)
        telemetry = [r for r in rows if r["current_telemetry"]]
        materials = summarize_materials(rows)
        context = materials["incident_context"]
        owner = context.get("incident_owner")
        return self._result("build_postmortem", rows, {
            "status": "draft_requires_review", "summary": summary,
            "summary_provenance": "caller_supplied_unverified_narrative",
            "incident_scope": self._scope(),
            "timeline": [{"timestamp": r["timestamp"], "evidence_id": r["id"],
                          "observation": r.get("message", r.get("description", r.get("metric", r.get("operation", ""))))}
                         for r in sorted(telemetry, key=lambda r: _time(r["timestamp"]))],
            "evidence_ids": [r["id"] for r in rows],
            "root_cause": {"status": "hypothesis_requires_validation", "confirmed": False,
                           "missing_checks": missing_materials(rows),
                           "required_validations": ["Test the proposed mechanism in an isolated replay and check competing explanations; not executed."]},
            "incident_owner": owner,
            "release_purpose": context.get("release_purpose"),
            "rollback_plan": context.get("rollback_plan"),
            "impact": "Point samples are displayed as observations; this draft does not infer population impact or require a fabricated total.",
            "actions_executed": [], "recovery_verified": False,
            "follow_up": [(f"由资料中指定的{owner}复核时间线与候选机制；本记录不构成执行授权。" if owner else
                           "实际事件责任人未包含在当前资料中；本草稿不代填人员或授权。"),
                          "Review configuration safeguards, monitoring and change checks."],
        })
