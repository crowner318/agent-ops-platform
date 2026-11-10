"""Deterministic findings. The workflow must merge these, never ask an LLM to veto them."""
from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def evaluate(evidence: list[dict]) -> list[dict]:
    """Evaluate one release scope; reject mixing scope identities silently."""
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    rows = [row for row in evidence if isinstance(row, dict) and isinstance(row.get("content"), dict)]
    scopes = {(row.get("change_id"), row.get("service"), row.get("environment")) for row in rows}
    if len(scopes) > 1:
        raise ValueError("policy evidence mixes release scopes")
    findings = []

    def add(code, severity, title, detail, ids):
        findings.append({"code": code, "severity": severity, "title": title, "detail": detail,
                         "evidence_ids": list(dict.fromkeys(ids)), "source": "policy"})

    for row in rows:
        data = row["content"]
        if row.get("kind") == "config_diff":
            before, after = data.get("pool_before"), data.get("pool_after")
            if _number(before) and _number(after) and before > 0 and 0 <= after <= before * .5:
                add("POOL_SHARP_REDUCTION", "warning", "连接池上限骤降至少50%",
                    f"连接池上限从{before}降到{after}；需要负载、持有时长与容量验证，不能仅据此确认故障。", [row["id"]])
    ci = [row for row in rows if row.get("kind") == "ci_checks"]
    ci_valid = [row for row in ci if row["content"].get("status") in {"passed", "failed"}]
    if not ci_valid:
        add("MISSING_CI_MATERIAL", "warning", "缺少可用CI检查材料", "尚未取得明确通过/失败的检查记录；缺材料不等于检查已失败。", [r["id"] for r in ci])
    for row in ci_valid:
        if row["content"]["status"] == "failed":
            add("CI_CHECK_FAILED", "blocker", "CI检查失败", "已有明确失败检查；在责任人处理与重新验证前，不应提交放行建议。", [row["id"]])
    budgets = [row for row in rows if row.get("kind") == "dependency_budget"]
    valid_budgets = []
    for row in budgets:
        data = row["content"]
        requested, available = data.get("requested_connections"), data.get("available_connections")
        if _number(requested) and _number(available) and requested >= 0 and available >= 0:
            valid_budgets.append(row)
            if requested > available:
                add("DEPENDENCY_BUDGET_EXCEEDED", "blocker", "数据库连接需求超出可用预算",
                    f"目标副本总连接需求为{requested}，扣除其他消费者后的数据库可用连接为{available}。", [row["id"]])
    if not valid_budgets:
        add("MISSING_CAPACITY_MATERIAL", "warning", "缺少可用容量材料", "缺少目标总连接需求或数据库可用预算，不能认定容量足够，也不能认定已超额。", [r["id"] for r in budgets])
    return findings
