"""Describe materials actually observed; never fabricate missing or healthy facts."""
from __future__ import annotations


def summarize_materials(evidence: list[dict]) -> dict:
    current = [r for r in evidence if r.get("current_telemetry") is True]
    metrics = {r["metric"] for r in current if isinstance(r.get("metric"), str)}
    groups = set()
    for row in current:
        if "metric" in row: groups.add("metrics")
        elif "spans" in row or "duration_ms" in row: groups.add("traces")
        elif "change_type" in row: groups.add("changes")
        elif "message" in row: groups.add("logs")
    changes = [r for r in current if "change_type" in r]
    errors = [r for r in current if r.get("level") == "ERROR" or r.get("status") == "error"]
    pool_markers = ("db.acquire", "connection acquisition", "connection pool", "hikaripool", "连接池", "获取连接", "获取数据库连接")
    pool_fault = any(any(marker in str(row).lower() for marker in pool_markers) for row in errors)
    context = next((r["incident_context"] for r in changes if isinstance(r.get("incident_context"), dict)), {})
    known = {
        "traffic": bool(metrics & {"request_throughput", "request_rate", "qps"}),
        "connection_holding": any("connection_hold_time" in m for m in metrics),
        "connection_returns": any(r.get("event_type") == "connection_returns" for r in current),
        "database_capacity": {"db_sessions", "db_max_connections"} <= metrics,
        "database_waits": {"db_lock_waiters", "database_io_wait"} <= metrics,
        "complete_change_diff": any(r.get("diff_complete") is True for r in changes),
        "release_purpose": bool(context.get("release_purpose")),
        "rollback_plan": bool(context.get("rollback_plan")),
        "incident_owner": bool(context.get("incident_owner")),
        "baseline": any(r.get("phase") == "baseline" for r in current),
    }
    return {"current_count": len(current), "source_types": sorted(groups), "metric_names": sorted(metrics),
            "has_fault_signals": bool(errors), "has_pool_fault_signals": pool_fault, "has_change_record": bool(changes),
            "sparse": len(current) < 3, "provided": known,
            "incident_context": context, "synthetic": bool(current) and all(r.get("synthetic") is True for r in current)}


def missing_materials(evidence: list[dict]) -> list[str]:
    summary = summarize_materials(evidence)
    if summary["sparse"]:
        return ["当前范围仅取得少量样本，缺少足够的跨来源运行记录，不能据此判断发布故障或整体健康。"]
    if not summary["has_pool_fault_signals"]:
        return []
    requests = {
        "traffic": "当前资料未提供请求吞吐量及其统计周期，需要补查流量变化。",
        "connection_holding": "当前资料未提供连接持有时长分布，需要补查连接占用情况。",
        "connection_returns": "当前资料未提供连接归还明细，需要核对是否存在未及时归还。",
        "database_capacity": "当前资料未提供数据库会话数及总连接上限，需要核对容量余量。",
        "database_waits": "当前资料未提供数据库锁等待与I/O等待，需要检查竞争原因。",
        "complete_change_diff": "当前资料只有部分变更记录，需要完整代码与配置差异。",
    }
    return [text for key, text in requests.items() if not summary["provided"][key]]
