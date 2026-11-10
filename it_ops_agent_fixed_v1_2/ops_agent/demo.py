"""离线脚本仅验证优化流程，不代表模型质量或API耗时。"""
from __future__ import annotations

import json
import re
from .materials import missing_materials, summarize_materials


def _call(name, args, turn, index=0):
    return {"id": f"demo-{turn}-{index}-{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _collect(messages):
    observed = {}
    seed = json.loads(messages[1]["content"])
    for row in seed.get("context", {}).get("evidence", []):
        observed[row["id"]] = row
    for msg in messages:
        if msg["role"] == "tool":
            output = json.loads(msg["content"])
            if output.get("ok"):
                observed.update({row["id"]: row for row in output.get("evidence", [])})
        elif msg["role"] == "user":
            payload = json.loads(msg["content"]) if msg["content"].lstrip().startswith("{") else {}
            for item in payload.get("runtime_tool_observations", []):
                output = item.get("result", {})
                observed.update({row["id"]: row for row in output.get("evidence", [])})
    return list(observed.values())


class DemoClient:
    is_demo = True

    def complete(self, messages, tools=None, tool_choice="auto", timeout=None, response_format=None):
        role = re.search(r"\[role:(\w+)\]", messages[0]["content"]).group(1)
        seed = json.loads(messages[1]["content"])
        workflow = seed.get("context", {}).get("workflow", "single")
        evidence = _collect(messages)
        materials = summarize_materials(evidence)
        turn = sum(m["role"] == "assistant" for m in messages)
        prior = {call["function"]["name"] for m in messages for call in m.get("tool_calls", [])}
        names = {t["function"]["name"] for t in tools or []}
        calls = []
        if tool_choice != "none":
            if not any(e.get("current_telemetry") for e in evidence) and "collect_evidence" in names:
                kinds = ["metrics", "logs"] if role == "triage" else ["metrics", "logs", "traces", "changes", "runbooks"]
                calls.append(_call("collect_evidence", {"kinds": kinds}, turn))
            elif role == "diagnosis" and "search_runbooks" in names and "search_runbooks" not in prior and not any(e.get("current_telemetry") is False for e in evidence):
                calls.append(_call("search_runbooks", {"query": "pool timeout latency"}, turn))
            elif (materials["has_pool_fault_signals"] and (role == "remediation" or workflow == "single" and role == "diagnosis")
                  and "assess_remediation" not in prior):
                ids = [e["id"] for e in evidence][:40]
                if ids:
                    calls = [_call("assess_remediation", {"action": "核对运行时配置和数据库容量后再评审连接池变更。",
                        "evidence_ids": ids}, turn), _call("build_postmortem", {
                        "summary": "模拟故障证据已收集，根因和恢复状态仍需验证。", "evidence_ids": ids}, turn, 1)]
                    calls = [call for call in calls if call["function"]["name"] in names]
        if calls:
            return {"role": "assistant", "content": None, "tool_calls": calls}
        changes = [e for e in evidence if e.get("change_type")]
        errors = [e for e in evidence if e.get("status") == "error" or e.get("level") == "ERROR"]
        pool_changes = [e for e in changes if any(marker in json.dumps(e, ensure_ascii=False).lower()
                       for marker in ("db_pool", "connection pool", "连接池"))]
        relevant_fault = bool(pool_changes) and materials["has_pool_fault_signals"]
        hypotheses = []
        if relevant_fault:
            hypotheses.append({"text": "连接池配置变化与获取连接超时在时间和机制上相符，是待验证候选原因。",
                "evidence_ids": [e["id"] for e in (pool_changes + errors)[:5]],
                "validation": ["核对实例生效配置与数据库连接余量。", "在隔离环境回放相同负载，并排查连接泄漏、慢事务等竞争原因。"]})
        report = {"hypotheses": hypotheses,
            "recommendations": ([{"text": ("依据当前连接池差异与超时样本，评审演示材料中的配置回退方案，并对照负载、连接归还和数据库容量验证；尚未执行。"
                                                        if materials["provided"]["rollback_plan"] else
                                                        "先核对运行时配置、连接等待和数据库容量；变更需独立审批、灰度、回退与恢复观测。"),
                                  "evidence_ids": [e["id"] for e in pool_changes]}]
                                if relevant_fault and (role == "remediation" or workflow == "single") else []),
            "missing_evidence": missing_materials(evidence),
            "process_checks": (["核对演示计划中的独立授权和验证步骤；材料中的责任人不是自动执行授权。"]
                               if materials["provided"]["incident_owner"] else
                               ["核查关键配置是否经过容量评估，以及审批和告警覆盖情况。"] if relevant_fault else [])}
        return {"role": "assistant", "content": json.dumps(report, ensure_ascii=False)}
