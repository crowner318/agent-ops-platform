"""Validate local user material and compile it to the Agents' existing JSON inputs.

Text is data only. This module never imports an Agent, executes a command, follows
a URL or reads a user-selected path. Missing input remains missing.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from .profiles import read_json, scenarios, stamp

MAX_BYTES = 512 * 1024
MAX_MATERIAL_BYTES = MAX_BYTES
MAX_RECORDS = 500
MAX_TEXT = 60000
OPS_KINDS = ("metrics", "logs", "traces", "changes", "runbooks")
RELEASE_KINDS = ("release_snapshot", "config_diff", "code_diff", "ci_checks", "dependency_budget", "runbook", "incident_case")
CURRENT_RELEASE = RELEASE_KINDS[:5]
SCOPE_KEYS = ("change_id", "service", "environment")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _text(value, label, *, required=False, limit=MAX_TEXT):
    if not isinstance(value, str) or len(value) > limit or "\0" in value or (required and not value.strip()):
        raise ValueError(f"{label}必须是{'非空' if required else ''}文本，且不超过{limit}字符。")
    return value.strip()


def _identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label}仅允许1至80个英文字母、数字、下划线或连字符。")
    return value


def _timestamp(value, label):
    try:
        if not isinstance(value, str):
            raise ValueError()
        return stamp(value)
    except (ValueError, AttributeError, OverflowError):
        raise ValueError(f"{label}必须是带时区的ISO日期时间。") from None


def _bounded(value):
    def walk(item, depth=0):
        if depth > 30:
            raise ValueError("材料嵌套过深。")
        if isinstance(item, dict):
            if not all(isinstance(key, str) and len(key) <= 200 for key in item):
                raise ValueError("材料字段名无效。")
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            _text(item, "材料文本")
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise ValueError("材料只允许JSON值，数值必须有限。")
    walk(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, OverflowError, RecursionError):
        raise ValueError("材料不是有效JSON。") from None
    if len(encoded) > MAX_BYTES:
        raise ValueError("材料超过512KiB，请减少记录或文本。")


def _number(value, label, *, integer=False, optional=False, nonnegative=True):
    if optional and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    if type(value) is bool:
        raise ValueError(f"{label}必须是数值。")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise ValueError(f"{label}必须是数值。") from None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and (not nonnegative or value >= 0) and (not integer or int(value) == value)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{label}必须是{'非负整数' if integer else '有限数值'}。")
    return int(value) if integer else value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("材料JSON存在重复字段。")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("数值必须有限。")


def _lines_or_rows(value, label):
    value = _text(value, label)
    if not value:
        return [], False
    if value.startswith("["):
        try:
            rows = json.loads(value, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError(f"{label}中的JSON数组无效。") from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{label}需要标准记录对象数组。")
        return rows, True
    return [line.strip() for line in value.splitlines() if line.strip()], False


def _json_number(value, label, **kwargs):
    if type(value) not in (int, float):
        raise ValueError(f"{label}必须是JSON数字，不能是字符串或布尔值。")
    return _number(value, label, **kwargs)


def _row(collection, params, observed_at, synthetic, **content):
    return {"id": "input-" + collection + "-" + uuid4().hex[:16], "service": params["service"],
            "environment": params["environment"], "timestamp": observed_at, "synthetic": synthetic, **content}


def _ops_fields(raw, params):
    at, synthetic = raw["observed_at"], raw["synthetic"]
    data = {kind: [] for kind in OPS_KINDS}
    data["metadata"] = {"synthetic": synthetic, "source_type": "user_supplied",
                        "verification_status": "unverified", "observation_time_input": at}
    for kind in OPS_KINDS:
        rows, native = _lines_or_rows(raw.get(kind, ""), kind)
        if native:
            data[kind] = rows
            continue
        for line in rows:
            if kind == "metrics":
                match = re.fullmatch(r"([^\s=]+)\s*=\s*([^\s]+)(?:\s+(.+))?", line)
                if not match:
                    raise ValueError("指标每行格式为 metric=value unit，例如 request_error_rate=0.183 ratio；或填写标准JSON记录数组。")
                metric, value, unit = match.groups()
                data[kind].append(_row(kind, params, at, synthetic, metric=metric,
                    value=_number(value, "指标数值", nonnegative=False), unit=unit or "unspecified"))
            elif kind == "logs":
                match = re.match(r"^(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL)\b", line, flags=re.I)
                level = match.group(1).upper() if match else "UNKNOWN"
                data[kind].append(_row(kind, params, at, synthetic, level=level, message=line))
            elif kind == "traces":
                parts = [piece.strip() for piece in line.split("|")]
                if len(parts) not in (3, 4) or not parts[0] or not parts[1]:
                    raise ValueError("链路每行格式为 operation|status|duration_ms|span:duration_ms；最后一项可省略。")
                spans = []
                for item in parts[3].split(",") if len(parts) == 4 and parts[3] else []:
                    if ":" not in item:
                        raise ValueError("链路span需要 name:duration_ms 格式。")
                    name, duration = item.rsplit(":", 1)
                    spans.append({"name": _text(name, "span名称", required=True), "duration_ms": _number(duration, "span耗时")})
                data[kind].append(_row(kind, params, at, synthetic, operation=parts[0], status=parts[1],
                    duration_ms=_number(parts[2], "链路耗时"), spans=spans))
            elif kind == "changes":
                data[kind].append(_row(kind, params, at, synthetic, change_type="user_provided", description=line))
            else:
                data[kind].append(_row(kind, params, at, synthetic, title="用户提供参考快照", keywords=[],
                    content="用户提供资料，仅绑定本次查询区间；未核实原手册效期。\n" + line,
                    valid_from=params["start"], valid_until=params["end"]))
    return data


def _release_fields(raw, params):
    at, synthetic = raw["observed_at"], raw["synthetic"]
    snapshot = "input-" + uuid4().hex
    scope = {key: params[key] for key in SCOPE_KEYS} | {"snapshot_id": snapshot}
    evidence = {kind: [] for kind in RELEASE_KINDS}
    content = {}
    for field in ("release_purpose", "change_summary", "owner", "rollback_plan", "validation_plan"):
        value = _text(raw.get(field, ""), field)
        if value:
            content[field] = value
    def add(kind, body):
        evidence[kind].append(_row(kind, params, at, synthetic, kind=kind, change_id=params["change_id"], content=body))
    before = _number(raw.get("config_before"), "变更前连接池上限", integer=True, optional=True)
    after = _number(raw.get("config_after"), "变更后连接池上限", integer=True, optional=True)
    if before is not None and after is not None:
        add("config_diff", {"pool_before": before, "pool_after": after,
                            "changes": [{"key": "DB_POOL_MAX", "before": before, "after": after}]})
    elif before is not None or after is not None:
        content["partial_configuration_material"] = {"pool_before": before, "pool_after": after}
    diff = _text(raw.get("code_diff", ""), "代码变更说明")
    if diff:
        add("code_diff", {"summary": diff})
    ci = raw.get("ci_status", "unknown")
    if not isinstance(ci, str) or ci not in {"passed", "failed", "unknown"}:
        raise ValueError("测试状态只能为passed、failed或unknown。")
    details = _text(raw.get("ci_details", ""), "测试材料说明")
    if ci in {"passed", "failed"}:
        add("ci_checks", {"status": ci, "details": details, "source_type": "user_supplied_unverified"})
    elif details:
        content["unconfirmed_ci_material"] = details
    capacity = {key: _number(raw.get(key), key, integer=True, optional=True)
                for key in ("replicas", "pool_max", "db_max_connections", "other_connections")}
    if after is not None and capacity["pool_max"] is not None and after != capacity["pool_max"]:
        raise ValueError("配置连接池上限与容量计算口径不一致，请核对config_after与pool_max。")
    if all(value is not None for value in capacity.values()):
        if capacity["other_connections"] > capacity["db_max_connections"]:
            raise ValueError("其他消费者连接数不能大于数据库总连接上限，请核对容量口径。")
        add("dependency_budget", {"replicas": capacity["replicas"], "pool_max": capacity["pool_max"],
            "db_max_connections": capacity["db_max_connections"], "current_connections": capacity["other_connections"],
            "current_connections_scope": "other consumers only; excludes the target release replicas",
            "requested_connections": capacity["replicas"] * capacity["pool_max"],
            "available_connections": capacity["db_max_connections"] - capacity["other_connections"],
            "calculation_note": "根据用户填写值计算：目标需求=副本数×单副本池上限；可用预算=数据库总上限−其他消费者连接。未连接数据库核实。"})
    elif any(value is not None for value in capacity.values()):
        content["partial_capacity_material"] = {key: value for key, value in capacity.items() if value is not None}
    if capacity["replicas"] is not None:
        content["replicas"] = capacity["replicas"]
    if content or any(evidence.values()):
        content["material_notice"] = "用户自填模拟资料。" if synthetic else "用户提供资料，来源和内容尚未独立核实；不表示已接入监控、代码仓库或CI系统。"
        add("release_snapshot", content)
    return {"snapshot_id": snapshot, "changes": {params["change_id"]: {"scope": scope, "as_of": at, "evidence": evidence}}}


def _validate_row(row, params, synthetic, seen, *, release=False, kind=None):
    if not isinstance(row, dict):
        raise ValueError("每条材料必须为JSON对象。")
    clean_id = _text(row.get("id"), "记录ID", required=True, limit=128)
    if clean_id != row["id"]:
        raise ValueError("记录ID不能以空白字符开头或结尾。")
    if row["id"] in seen:
        raise ValueError("材料记录ID重复。")
    seen.add(row["id"])
    if len(seen) > MAX_RECORDS:
        raise ValueError("材料记录不能超过500条。")
    if any(row.get(key) != params[key] for key in (("change_id", "service", "environment") if release else ("service", "environment"))):
        raise ValueError("材料记录的服务、环境或变更与表单范围不一致；请同步修改材料或表单。")
    _timestamp(row.get("timestamp"), "记录时间")
    if type(row.get("synthetic")) is not bool or row["synthetic"] != synthetic:
        raise ValueError("所有记录的synthetic必须为布尔值，且与表单的资料来源标记一致。")
    if release:
        required = {"id", "kind", "change_id", "service", "environment", "timestamp", "synthetic", "content"}
        if set(row) != required or row.get("kind") != kind or not isinstance(row.get("content"), dict):
            raise ValueError("发布记录字段或kind无效，需要标准证据格式。")


def _validate_ops(data, params, synthetic):
    if not isinstance(data, dict) or set(data) - {*OPS_KINDS, "metadata"}:
        raise ValueError("故障材料只接受metrics、logs、traces、changes、runbooks及metadata。")
    if "metadata" in data and not isinstance(data["metadata"], dict):
        raise ValueError("metadata必须为JSON对象。")
    start, end = _timestamp(params["start"], "开始时间"), _timestamp(params["end"], "结束时间")
    seen, selected, counts = set(), [], {}
    for kind in OPS_KINDS:
        rows = data.get(kind, [])
        if not isinstance(rows, list):
            raise ValueError(f"{kind}必须为数组。")
        for row in rows:
            _validate_row(row, params, synthetic, seen)
            if kind == "metrics":
                _text(row.get("metric"), "指标名称", required=True, limit=160)
                _text(row.get("unit"), "指标单位", limit=160)
                _json_number(row.get("value"), "指标数值", nonnegative=False)
            elif kind == "logs":
                _text(row.get("message"), "日志原文", required=True)
                if "level" in row:
                    _text(row["level"], "日志级别", limit=40)
            elif kind == "traces":
                _json_number(row.get("duration_ms"), "链路耗时")
                _text(row.get("operation"), "链路操作", required=True)
                _text(row.get("status"), "链路状态", required=True, limit=40)
                if not isinstance(row.get("spans", []), list):
                    raise ValueError("spans必须为数组。")
                for span in row.get("spans", []):
                    if not isinstance(span, dict):
                        raise ValueError("span必须为对象。")
                    _text(span.get("name"), "span名称", required=True)
                    _json_number(span.get("duration_ms"), "span耗时")
            elif kind == "changes":
                _text(row.get("change_type"), "变更类型", required=True)
                _text(row.get("description"), "变更说明", required=True)
            else:
                _text(row.get("title"), "手册标题", required=True)
                _text(row.get("content"), "手册内容", required=True)
                if not isinstance(row.get("keywords"), list) or any(not isinstance(word, str) for word in row["keywords"]):
                    raise ValueError("手册keywords需要文本数组。")
                if _timestamp(row.get("valid_from"), "参考生效时间") > _timestamp(row.get("valid_until"), "参考失效时间"):
                    raise ValueError("参考资料生效时间晚于失效时间。")
        if kind != "runbooks":
            in_window = [row for row in rows if start <= _timestamp(row["timestamp"], "记录时间") <= end]
            counts[kind] = len(in_window)
            selected.extend(in_window)
    return _coverage(counts, selected, synthetic)


def _validate_release(data, params, synthetic):
    if not isinstance(data, dict) or set(data) != {"snapshot_id", "changes"}:
        raise ValueError("发布材料需要snapshot_id和changes两个顶层字段。")
    _text(data["snapshot_id"], "快照编号", required=True, limit=160)
    if not isinstance(data["changes"], dict) or set(data["changes"]) != {params["change_id"]}:
        raise ValueError("发布材料只接受当前变更编号对应的一份快照。")
    change = data["changes"][params["change_id"]]
    if not isinstance(change, dict) or set(change) != {"scope", "as_of", "evidence"}:
        raise ValueError("变更快照字段无效。")
    expected_scope = {key: params[key] for key in SCOPE_KEYS} | {"snapshot_id": data["snapshot_id"]}
    if change.get("scope") != expected_scope:
        raise ValueError("快照范围与表单的服务、环境、变更编号不一致。")
    _timestamp(change["as_of"], "评审时刻")
    groups = change["evidence"]
    if not isinstance(groups, dict) or set(groups) - set(RELEASE_KINDS):
        raise ValueError("发布证据集合类型无效。")
    seen, selected, counts = set(), [], {}
    for kind in RELEASE_KINDS:
        rows = groups.get(kind, [])
        if not isinstance(rows, list):
            raise ValueError(f"{kind}必须为数组。")
        for row in rows:
            _validate_row(row, params, synthetic, seen, release=True, kind=kind)
            body = row["content"]
            if kind == "config_diff":
                for key in ("pool_before", "pool_after"):
                    if key in body:
                        _json_number(body[key], key, integer=True)
            elif kind == "dependency_budget":
                for key in ("replicas", "pool_max", "db_max_connections", "current_connections", "requested_connections", "available_connections"):
                    if key in body:
                        _json_number(body[key], key, integer=True)
                if {"replicas", "pool_max", "requested_connections"} <= body.keys() and body["requested_connections"] != body["replicas"] * body["pool_max"]:
                    raise ValueError("requested_connections与副本数乘连接池上限不一致。")
                if ({"db_max_connections", "current_connections", "available_connections"} <= body.keys()
                        and body.get("current_connections_scope") == "other consumers only; excludes the target release replicas"
                        and body["available_connections"] != body["db_max_connections"] - body["current_connections"]):
                    raise ValueError("available_connections与数据库上限减其他消费者连接数不一致。")
            elif kind == "ci_checks" and "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in {"passed", "failed", "unknown"}:
                    raise ValueError("CI status只能为passed、failed或unknown。")
            elif kind == "runbook":
                if _timestamp(body.get("valid_from"), "手册生效时间") > _timestamp(body.get("valid_until"), "手册失效时间"):
                    raise ValueError("手册生效时间晚于失效时间。")
            elif kind == "incident_case" and "verified" in body and type(body["verified"]) is not bool:
                raise ValueError("历史案例verified必须为布尔值，不代表系统已经独立核实该用户声明。")
        if kind in CURRENT_RELEASE:
            counts[kind] = len(rows)
            selected.extend(rows)
    after = {row["content"]["pool_after"] for row in groups.get("config_diff", []) if "pool_after" in row["content"]}
    pool = {row["content"]["pool_max"] for row in groups.get("dependency_budget", []) if "pool_max" in row["content"]}
    if after and pool and (len(after) != 1 or len(pool) != 1 or after != pool):
        raise ValueError("配置连接池上限与容量计算口径不一致，请核对pool_after与pool_max。")
    return _coverage(counts, selected, synthetic)


def _coverage(counts, rows, synthetic):
    times = sorted(_timestamp(row["timestamp"], "记录时间") for row in rows)
    missing = [kind for kind, count in counts.items() if not count]
    return {"counts": counts, "current_count": len(rows), "missing_kinds": missing,
            "first_observation": times[0].isoformat() if times else None,
            "last_observation": times[-1].isoformat() if times else None,
            "material_status": "empty" if not rows else "partial" if missing else "complete",
            "source": "synthetic" if synthetic else "user_supplied"}


def preview_materials(agent, payload):
    """Validate a file and derive its explicit scope without creating a task."""
    if not isinstance(agent, str) or agent not in {"ops", "release"} or not isinstance(payload, dict):
        raise ValueError("请选择助手并上传JSON材料对象。")
    _bounded(payload)
    if "agent" in payload and payload["agent"] != agent:
        raise ValueError("文件所属助手与当前页面不一致，请切换到对应助手。")
    raw = payload.get("materials", payload)
    if not isinstance(raw, dict):
        raise ValueError("materials必须为JSON对象。")
    if "format" in raw:
        if raw["format"] != "json":
            raise ValueError("文件导入需要JSON记录；简易表单请直接在网页填写。")
        data = raw.get("data")
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("材料data必须为JSON对象。")
    if agent == "ops":
        if "snapshot_id" in data:
            raise ValueError("这是发布评审材料，请切换到发布评审页面导入。")
        rows = []
        for kind in OPS_KINDS:
            items = data.get(kind, [])
            if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
                raise ValueError(f"{kind}必须是JSON记录数组。")
            if kind != "runbooks":
                rows.extend(items)
        if not rows:
            raise ValueError("文件缺少当前指标、日志、链路或变更记录。")
        scopes = {(row.get("service"), row.get("environment")) for row in rows
                  if isinstance(row.get("service"), str) and isinstance(row.get("environment"), str)}
        if len(scopes) != 1 or any((row.get("service"), row.get("environment")) not in scopes for row in rows):
            raise ValueError("一次只能导入一个服务和环境的材料，请先拆分多范围记录。")
        service, environment = scopes.pop()
        times = [_timestamp(row.get("timestamp"), "记录时间") for row in rows]
        params = {"service": service, "environment": environment,
                  "start": min(times).isoformat(), "end": max(times).isoformat()}
        all_rows = rows + data.get("runbooks", [])
    else:
        changes = data.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("这是故障诊断材料或缺少changes对象，请检查所选助手。")
        if len(changes) != 1:
            raise ValueError("一次只能评审一个变更，请保留本次变更后再导入。")
        change_id, change = next(iter(changes.items()))
        if not isinstance(change, dict) or not isinstance(change.get("scope"), dict):
            raise ValueError("变更缺少scope范围信息。")
        scope = change["scope"]
        params = {"change_id": change_id, "service": scope.get("service"), "environment": scope.get("environment")}
        groups = change.get("evidence")
        if not isinstance(groups, dict) or any(not isinstance(rows, list) for rows in groups.values()):
            raise ValueError("变更evidence必须为证据记录数组的集合。")
        all_rows = [row for rows in groups.values() for row in rows]
    if not all_rows or any(not isinstance(row, dict) or type(row.get("synthetic")) is not bool for row in all_rows):
        raise ValueError("每条记录必须明确标注synthetic为true或false。")
    origins = {row["synthetic"] for row in all_rows}
    if len(origins) != 1:
        raise ValueError("同一份文件混用了模拟和非模拟记录，请核对来源标记。")
    synthetic = origins.pop()
    if "format" in raw and (type(raw.get("synthetic")) is not bool or raw["synthetic"] != synthetic):
        raise ValueError("文件外层与记录中的synthetic来源标记不一致。")
    materials = {"format": "json", "synthetic": synthetic, "data": data}
    _, observed, label = prepare_materials(agent, materials, params)
    return {"params": params, "materials": materials, "coverage": observed, "source_label": label}


def prepare_materials(agent, raw, params):
    """Return (filename-to-object mapping, coverage, source label), without IO."""
    if not isinstance(agent, str) or agent not in {"ops", "release"} or not isinstance(raw, dict) or not isinstance(params, dict):
        raise ValueError("自填材料参数无效。")
    _bounded(raw)
    for key in ("service", "environment") + (("change_id",) if agent == "release" else ()):
        _identifier(params.get(key), key)
    if type(raw.get("synthetic")) is not bool:
        raise ValueError("请明确选择模拟资料或用户提供资料。")
    synthetic = raw["synthetic"]
    if raw.get("format") == "fields":
        fields = set(OPS_KINDS) if agent == "ops" else {"release_purpose", "change_summary", "owner", "rollback_plan", "validation_plan", "config_before", "config_after", "code_diff", "ci_status", "ci_details", "replicas", "pool_max", "db_max_connections", "other_connections"}
        if set(raw) - (fields | {"format", "synthetic", "observed_at"}):
            raise ValueError("自填材料包含未知字段。")
        _timestamp(raw.get("observed_at"), "材料观测时间")
        data = _ops_fields(raw, params) if agent == "ops" else _release_fields(raw, params)
    elif raw.get("format") == "json":
        if set(raw) != {"format", "synthetic", "data"}:
            raise ValueError("JSON材料仅接受format、synthetic和data字段。")
        data = deepcopy(raw["data"])
    else:
        raise ValueError("材料格式必须为fields或json。")
    _bounded(data)
    observed = _validate_ops(data, params, synthetic) if agent == "ops" else _validate_release(data, params, synthetic)
    if not observed["current_count"]:
        raise ValueError("所选服务、环境和时间内没有当前材料，请至少填写一条当前指标、日志、链路、变更或发布资料。")
    filename = "demo_data.json" if agent == "ops" else "fixtures.json"
    return {filename: data}, observed, "用户自填模拟资料" if synthetic else "用户提供资料（未核实）"


def material_template(agent, project, profile_id):
    """Get a trusted built-in scenario as editable JSON, never an arbitrary path."""
    if not isinstance(agent, str) or agent not in {"ops", "release"} or not isinstance(profile_id, str):
        raise ValueError("模板参数无效。")
    profile = next((item for item in scenarios(agent, Path(project)) if item["id"] == profile_id), None)
    if profile is None:
        raise ValueError("材料模板不存在。")
    params = deepcopy(profile["params"])
    if agent == "ops":
        raw = read_json(profile["data_dir"] / "demo_data.json")
        start, end = stamp(params["start"]), stamp(params["end"])
        data = {"metadata": deepcopy(raw.get("metadata", {}))}
        for kind in OPS_KINDS:
            data[kind] = [deepcopy(row) for row in raw.get(kind, [])
                if row.get("service") == params["service"] and row.get("environment") == params["environment"]
                and (kind == "runbooks" or start <= stamp(row["timestamp"]) <= end)]
            if kind == "runbooks":
                # A shared '*' reference is bound to this editable material copy.
                data[kind] = [deepcopy(row) for row in raw.get(kind, [])
                    if row.get("service") == params["service"] and row.get("environment") in {params["environment"], "*"}
                    and stamp(row["valid_from"]) <= end <= stamp(row["valid_until"])]
                for row in data[kind]:
                    row["environment"] = params["environment"]
    else:
        raw = read_json(profile["data_dir"] / "fixtures.json")
        change = deepcopy(raw["changes"][params["change_id"]])
        params.update({key: change["scope"][key] for key in ("service", "environment")})
        params["observed_at"] = change["as_of"]
        data = {"snapshot_id": raw["snapshot_id"], "changes": {params["change_id"]: change}}
    return {"materials": {"format": "json", "data": data, "synthetic": True}, "params": params}
