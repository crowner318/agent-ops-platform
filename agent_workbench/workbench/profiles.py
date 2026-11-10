"""Fixed local demo scenarios, with material coverage computed before execution."""
from datetime import datetime
import json
from pathlib import Path

OPS_DEFAULTS = {"service": "order-service", "environment": "demo", "start": "2026-10-19T09:55:00+08:00",
                "end": "2026-10-19T10:20:00+08:00", "question": "订单服务发布后错误率升高，请分析候选根因、给出处置建议并生成复盘草稿。",
                "workflow": "single"}


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("演示资料超过2MB限制。")
    def reject(_):
        raise ValueError("演示资料不能包含非有限数字。")
    value = json.loads(raw, parse_constant=reject)
    if not isinstance(value, dict):
        raise ValueError("演示资料必须为JSON对象。")
    return value


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("资料时间必须含时区。")
    return result


def coverage(agent, data_dir, params):
    required = ("metrics", "logs", "traces", "changes") if agent == "ops" else (
        "release_snapshot", "config_diff", "code_diff", "ci_checks", "dependency_budget")
    selected = []
    counts = {}
    if agent == "ops":
        data = read_json(Path(data_dir) / "demo_data.json")
        start, end = stamp(params["start"]), stamp(params["end"])
        for kind in required:
            rows = []
            for row in data.get(kind, []):
                if not isinstance(row, dict):
                    raise ValueError("演示资料的记录格式无效。")
                if (row.get("service") == params["service"] and row.get("environment") == params["environment"]
                        and start <= stamp(row["timestamp"]) <= end):
                    rows.append(row)
            counts[kind] = len(rows)
            selected.extend(rows)
    else:
        data = read_json(Path(data_dir) / "fixtures.json")
        change = data.get("changes", {}).get(params["change_id"])
        if not isinstance(change, dict):
            raise ValueError("该场景没有对应的发布材料。")
        for kind in required:
            rows = change.get("evidence", {}).get(kind, [])
            counts[kind] = len(rows)
            selected.extend(rows)
    times = sorted(stamp(row["timestamp"]) for row in selected)
    missing = [kind for kind in required if not counts[kind]]
    source = "synthetic" if selected and all(row.get("synthetic") is True for row in selected) else "local"
    return {"counts": counts, "current_count": len(selected), "missing_kinds": missing,
            "first_observation": times[0].isoformat() if times else None,
            "last_observation": times[-1].isoformat() if times else None,
            "material_status": "empty" if not selected else "partial" if missing else "complete",
            "source": source}


def scenarios(agent, project):
    project = Path(project)
    items = []
    if agent == "ops":
        definitions = [
            ("complete-demo", "完整订单故障演示", "含流量、连接池、数据库及变更材料，建议先运行本场景。", project / "data/demo_complete", "demo", "completed"),
            ("original-fault", "原始故障样本", "保留原始18条观测，模型可能合理建议继续补查。", project / "data", "demo", "completed"),
            ("healthy-staging", "预发布正常片段", "只有烟测与错误率样本，用于展示材料覆盖不足，不代表完整故障。", project / "data", "staging", "incomplete"),
        ]
        for identity, label, description, directory, environment, expected in definitions:
            if not (directory / "demo_data.json").is_file():
                continue
            params = dict(OPS_DEFAULTS, environment=environment)
            if environment == "staging":
                params["question"] = "检查预发布环境的现有观测，说明能确认什么，以及还缺少哪些类别的材料。"
            observed = coverage(agent, directory, params)
            items.append({"id": identity, "label": label, "description": description,
                "data_dir": directory, "params": params, "coverage": observed, "source": observed["source"],
                "expected_status": expected, "owner": "演示运维组", "ready": observed["current_count"] > 0})
    else:
        labels = {"safe": "完整正常发布演示", "risk-pool": "连接池误配与容量阻断", "missing": "缺材料处理演示", "legacy-demo": "旧案例参考演示"}
        for dirname, prefix in (("data/demo_complete", "complete"), ("data", "original")):
            directory = project / dirname
            if not (directory / "fixtures.json").is_file():
                continue
            data = read_json(directory / "fixtures.json")
            for change_id in ("safe", "risk-pool", "missing", "legacy-demo"):
                if change_id not in data.get("changes", {}):
                    continue
                scope = data["changes"][change_id].get("scope", {})
                params = {"change_id": change_id, "service": scope.get("service", ""), "environment": scope.get("environment", "")}
                observed = coverage(agent, directory, params)
                label = labels[change_id]
                if prefix == "original":
                    label = "原始样本 · " + ("正常发布对照" if change_id == "safe" else label)
                description = ("原始材料保持不变，模型可能指出合理的资料缺口。" if prefix == "original" else
                               "明确保留缺项，用于验证不完整状态。" if change_id == "missing" else
                               "读取已标注为模拟的发布材料；确认阅读不会执行发布。")
                items.append({"id": prefix + "-" + change_id,
                    "label": label,
                    "description": description,
                    "data_dir": directory, "params": params, "coverage": observed, "source": observed["source"],
                    "expected_status": {"safe":"review_acknowledged", "risk-pool":"blocked", "missing":"incomplete"}.get(change_id, "waiting_human"),
                    "owner":"演示发布组", "ready":observed["current_count"] > 0})
    return items


def public_profile(item):
    return {key: value for key, value in item.items() if key != "data_dir"}
