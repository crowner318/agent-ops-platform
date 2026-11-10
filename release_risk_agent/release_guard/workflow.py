"""Native LangGraph workflow with durable SQLite checkpoints and human pause.

The graph creates review records only. Neither a model response nor a human
acknowledgement authorizes deployment or overrides deterministic blockers.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .analysis import CURRENT_KINDS, SCOPE_KEYS, evidence_scope_issues, review_analysis
from .mcp_client import MCPTools
from .policy import evaluate


TOOL_REQUESTS = (
    ("get_release_snapshot", {}), ("get_config_diff", {}), ("get_code_diff", {}),
    ("get_ci_checks", {}), ("get_dependency_budget", {}),
    ("search_runbooks", {"query": "connection pool release rollback capacity 连接池 发布 容量"}),
    ("search_incident_cases", {"query": "connection pool release timeout 连接池 发布 超时"}),
)


class ReleaseState(TypedDict, total=False):
    change_id: str
    run_mode: str
    scope: dict
    evidence: list[dict]
    tool_records: list[dict]
    policy_findings: list[dict]
    analysis: dict | None
    analysis_source: str
    quality_issues: list[dict]
    model_calls: list[dict]
    decision: str
    status: str
    review_digest: str
    review_record: dict
    deployment_approved: bool


def make_review_digest(state: dict) -> str:
    package = {"scope": state.get("scope", {}), "policy_findings": state.get("policy_findings", []),
        "analysis": state.get("analysis"), "analysis_source": state.get("analysis_source"),
        "evidence": sorted(state.get("evidence", []), key=lambda row: row["id"]),
        "quality_issues": state.get("quality_issues", [])}
    return hashlib.sha256(json.dumps(package, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _bind_thread(path: Path, thread_id: str, change_id: str, mode: str) -> None:
    if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 200:
        raise ValueError("thread_id must be a non-empty identifier of at most 200 characters")
    with closing(sqlite3.connect(str(path), timeout=20)) as connection, connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS release_thread_bindings (
            thread_id TEXT PRIMARY KEY, change_id TEXT NOT NULL, run_mode TEXT NOT NULL)""")
        connection.execute("INSERT OR IGNORE INTO release_thread_bindings VALUES(?,?,?)",
                           (thread_id, change_id, mode))
        bound = connection.execute("SELECT change_id,run_mode FROM release_thread_bindings WHERE thread_id=?",
                                   (thread_id,)).fetchone()
        if bound != (change_id, mode):
            raise ValueError("thread_id is already bound to a different change or execution mode")


def _review_payload(payload: Any, digest: str) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"reviewer", "decision", "comment", "proposed_digest"}:
        raise ValueError("human review requires reviewer, decision, comment and proposed_digest only")
    if (not isinstance(payload["reviewer"], str) or not 1 <= len(payload["reviewer"].strip()) <= 100
            or not isinstance(payload["comment"], str) or len(payload["comment"]) > 2000):
        raise ValueError("invalid reviewer or comment")
    if payload["decision"] not in {"acknowledge", "request_changes", "reject"}:
        raise ValueError("human decision must be acknowledge, request_changes or reject; never deployment approval")
    if payload["proposed_digest"] != digest:
        raise ValueError("human review digest does not match the paused review package")
    return dict(payload, reviewer=payload["reviewer"].strip())


@asynccontextmanager
async def build_workflow(settings, runtime_dir: Path, data_dir: Path, coordinator=None,
                         demo: bool = False, event_callback=None):
    """Yield a compiled graph; keep this context open during ainvoke/aget_state.

    ``start`` uses a new configurable.thread_id and input {change_id: ...}.
    ``resume`` uses Command(resume={reviewer,decision,comment,proposed_digest}).
    The official AsyncSqliteSaver is required because the MCP nodes are async;
    native synchronous SqliteSaver intentionally does not implement ainvoke's
    checkpoint operations. Checkpoints survive closing and reopening processes.
    """
    runtime_dir, data_dir = Path(runtime_dir), Path(data_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / "checkpoints.sqlite"
    mode = "scripted_demo" if demo else "model_api"

    async def emit(event: dict):
        if event_callback is not None:
            value = event_callback(event)
            if inspect.isawaitable(value):
                await value

    async def collect(state: ReleaseState, config) -> dict:
        change_id = state.get("change_id")
        if not isinstance(change_id, str) or not change_id.strip() or len(change_id) > 128:
            raise ValueError("change_id must be a non-empty identifier of at most 128 characters")
        thread_id = config.get("configurable", {}).get("thread_id")
        await asyncio.to_thread(_bind_thread, db_path, thread_id, change_id, mode)
        scope, rows, records, issues = {}, {}, [], []
        transport = "unknown"
        try:
            async with MCPTools(data_dir, coordinator=coordinator,
                                timeout=min(float(settings.timeout), 20.0)) as client:
                discovered = await client.list_tools()
                transport = getattr(client, "mcp_transport", "unknown")
                names = {tool.name for tool in discovered}
                missing = {name for name, _ in TOOL_REQUESTS} - names
                if missing:
                    issues.append({"code": "mcp_discovery_incomplete", "path": "tools",
                                   "message": "MCP未提供全部必需的只读工具。"})

                async def query(name, extra):
                    started = perf_counter()
                    if name not in names:
                        return name, None, round((perf_counter() - started) * 1000, 3)
                    try:
                        result = await asyncio.wait_for(client.call(name, dict(change_id=change_id, **extra)),
                                                        timeout=min(float(settings.timeout), 20.0))
                        return name, result, round((perf_counter() - started) * 1000, 3)
                    except Exception:
                        return name, None, round((perf_counter() - started) * 1000, 3)

                responses = await asyncio.gather(*(query(name, extra) for name, extra in TOOL_REQUESTS))
            for name, response, duration in responses:
                good = isinstance(response, dict) and response.get("ok") is True
                records.append({"name": name, "ok": good, "duration_ms": duration,
                                "evidence_count": len(response.get("evidence", [])) if good else 0,
                                "mcp_transport": response.get("mcp_transport", transport) if good else transport,
                                "cache_hit": bool(response.get("cache_hit", False)) if good else False})
                if not good:
                    issues.append({"code": "evidence_tool_failed", "path": name,
                                   "message": "必需取证工具未成功，不能把缺少结果视为通过。"})
                    continue
                received_scope = response.get("scope")
                if (not isinstance(received_scope, dict) or received_scope.get("change_id") != change_id
                        or any(not isinstance(received_scope.get(k), str) or not received_scope[k] for k in SCOPE_KEYS)):
                    issues.append({"code": "tool_scope_invalid", "path": name, "message": "MCP工具范围缺失或变更不匹配。"})
                    continue
                normalized_scope = {key: received_scope[key] for key in SCOPE_KEYS}
                if scope and scope != normalized_scope:
                    issues.append({"code": "tool_scope_mismatch", "path": name, "message": "MCP工具返回了不同范围或快照。"})
                    continue
                scope = normalized_scope
                evidence = response.get("evidence")
                if not isinstance(evidence, list):
                    issues.append({"code": "invalid_tool_evidence", "path": name, "message": "MCP证据不是记录列表。"})
                    continue
                for row in evidence:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        issues.append({"code": "invalid_tool_evidence", "path": name, "message": "MCP证据记录缺少ID。"})
                        continue
                    if row["id"] in rows and rows[row["id"]] != row:
                        issues.append({"code": "conflicting_evidence_id", "path": name, "message": "相同证据ID对应不同正文。"})
                        continue
                    rows[row["id"]] = row
        except Exception:
            issues.append({"code": "mcp_collection_failed", "path": "collect",
                           "message": "MCP取证未完成，未采用未经校验的模型判断。"})
        evidence = list(rows.values())
        issues.extend(evidence_scope_issues(evidence, scope))
        kinds = {row.get("kind") for row in evidence}
        if not CURRENT_KINDS <= kinds:
            issues.append({"code": "required_current_material_missing", "path": "evidence",
                           "message": "缺少必需的当前发布、配置、代码、测试或依赖容量资料；不能声称评审完整。"})
        await emit({"event": "collection_finished", "change_id": change_id,
                    "evidence_count": len(evidence), "tool_count": len(records), "issue_count": len(issues)})
        return {"change_id": change_id, "run_mode": mode, "scope": scope, "evidence": evidence,
                "tool_records": records, "quality_issues": issues, "analysis": None,
                "analysis_source": mode, "policy_findings": [], "model_calls": [],
                "review_record": {}, "decision": "pending_review", "status": "incomplete",
                "deployment_approved": False}

    async def policy(state: ReleaseState) -> dict:
        issues = list(state.get("quality_issues", []))
        try:
            findings = evaluate(state.get("evidence", []))
            if (not isinstance(findings, list) or any(not isinstance(f, dict) or f.get("source") != "policy"
                    or f.get("severity") not in {"blocker", "warning", "info"} for f in findings)):
                raise ValueError("invalid_policy_result")
        except Exception:
            findings = []
            issues.append({"code": "policy_evaluation_failed", "path": "policy",
                           "message": "确定性规则检查未完成，不能声称风险检查通过。"})
        await emit({"event": "policy_finished", "change_id": state["change_id"],
                    "finding_count": len(findings), "blocker_count": sum(f["severity"] == "blocker" for f in findings)})
        return {"policy_findings": findings, "quality_issues": issues}

    async def model_review(state: ReleaseState) -> dict:
        if state.get("run_mode") != mode:
            raise ValueError("cannot switch execution mode for a persisted review")
        issues = list(state.get("quality_issues", []))
        if issues:
            response = {"analysis": None, "source": mode, "issues": [], "model_call": None}
        else:
            response = await review_analysis(settings, state["evidence"], state["scope"],
                                             state["policy_findings"], demo=demo)
            issues.extend(response["issues"])
        updated = {"analysis": response["analysis"], "analysis_source": response["source"],
                   "quality_issues": issues, "status": "incomplete" if issues else "waiting_human",
                   "model_calls": [response["model_call"]] if response.get("model_call") else []}
        updated["review_digest"] = make_review_digest(dict(state, **updated))
        await emit({"event": "model_review_finished", "change_id": state["change_id"],
                    "source": response["source"], "issue_count": len(issues)})
        return updated

    async def human_review(state: ReleaseState, config) -> dict:
        if state.get("run_mode") != mode:
            raise ValueError("cannot resume a persisted review using a different execution mode")
        await asyncio.to_thread(_bind_thread, db_path, config.get("configurable", {}).get("thread_id"),
                                state["change_id"], mode)
        digest = make_review_digest(state)
        if digest != state.get("review_digest"):
            raise ValueError("paused review package changed; create a new review")
        package = {"type": "release_risk_review", "scope": state.get("scope", {}),
            "status": state["status"], "policy_findings": state.get("policy_findings", []),
            "analysis": state.get("analysis"), "quality_issues": state.get("quality_issues", []),
            "evidence_ids": [row["id"] for row in state.get("evidence", [])],
            "review_digest": digest, "run_mode": mode,
            "allowed_decisions": ["acknowledge", "request_changes", "reject"],
            "notice": "确认仅表示已审阅风险报告，不是生产发布批准；blocker不可覆盖。"}
        payload = interrupt(package)
        reviewed = _review_payload(payload, digest)
        if reviewed["decision"] == "reject":
            outcome = "rejected"
        elif reviewed["decision"] == "request_changes":
            outcome = "changes_requested"
        elif state.get("quality_issues"):
            outcome = "incomplete"
        elif any(f.get("severity") == "blocker" for f in state.get("policy_findings", [])):
            outcome = "blocked"
        else:
            outcome = "review_acknowledged"
        record = dict(reviewed, reviewed_at=datetime.now(timezone.utc).isoformat(),
            scope=state.get("scope", {}), outcome=outcome, production_approval=False,
            record_type="release_risk_report_review")
        return {"review_record": record, "decision": outcome, "status": outcome,
                "deployment_approved": False}

    async def finish(state: ReleaseState) -> dict:
        await emit({"event": "review_finished", "change_id": state["change_id"],
                    "status": state["status"], "production_approval": False})
        return {"deployment_approved": False}

    graph = StateGraph(ReleaseState)
    graph.add_node("collect", collect)
    graph.add_node("policy", policy)
    graph.add_node("model_review", model_review)
    graph.add_node("human_review", human_review)
    graph.add_node("finish", finish)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "policy")
    graph.add_edge("policy", "model_review")
    graph.add_edge("model_review", "human_review")
    graph.add_edge("human_review", "finish")
    graph.add_edge("finish", END)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        yield graph.compile(checkpointer=saver)
