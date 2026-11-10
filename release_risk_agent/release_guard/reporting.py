"""Review reports with explicit provenance and no production approval claim."""
import json
from pathlib import Path
import re


def sanitize(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if normalized in {"apikey", "password", "authorization", "secret", "redisurl"}:
                result[str(key)] = "[REDACTED]"
            elif normalized not in {"reasoningcontent", "additionalreasoning"}:
                result[str(key)] = sanitize(child)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(child) for child in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
        value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        return re.sub(r"(?i)(rediss?://)[^/@\s]+@", r"\1[REDACTED]@", value)
    return value


def plain(value):
    return str(sanitize(value)).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def _material_text(value):
    """Display supplied material only; do not manufacture names or plan states."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(text for item in value if (text := _material_text(item)))
    return ""


def _owner_text(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    name = _material_text(value.get("name"))
    team = _material_text(value.get("team"))
    return name + ("（" + team + "）" if name and team else "") if name else team


def _plan_text(value, *, rollback=False):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    parts = []
    owner = _owner_text(value.get("owner"))
    if owner:
        parts.append("材料署名：" + owner)
    status = _material_text(value.get("status"))
    if status:
        states = {"planned_not_executed": "计划已提供，尚未执行"}
        parts.append("状态：" + states.get(status, status))
    fields = (("target_version", "目标版本"), ("triggers", "触发条件"),
              ("steps", "步骤"), ("verification", "回退后核验")) if rollback else (
              ("before_release", "发布前核对"), ("controlled_check", "验证安排"),
              ("acceptance_criteria", "验收标准"))
    for key, label in fields:
        text = _material_text(value.get(key))
        if text:
            parts.append(label + "：" + text)
    return "；".join(parts)


def _change_information(payload):
    """Read the scope-bound release snapshot, independent of model prose."""
    scope = payload.get("scope") or {}
    if not isinstance(scope, dict) or not all(isinstance(scope.get(key), str) and scope[key]
                                             for key in ("change_id", "service", "environment")):
        return []
    release = next((row["content"] for row in payload.get("evidence", [])
        if isinstance(row, dict) and row.get("kind") == "release_snapshot"
        and isinstance(row.get("content"), dict)
        and all(row.get(key) == scope[key] for key in ("change_id", "service", "environment"))
        and ("snapshot_id" not in row or row["snapshot_id"] == scope.get("snapshot_id"))), {})
    fields = [("发布目的", _material_text(release.get("release_purpose"))),
              ("变更说明", _material_text(release.get("change_summary"))),
              ("配置说明", _material_text(release.get("configuration_unchanged_reason"))),
              ("材料中的负责人", _owner_text(release.get("owner"))),
              ("回退方案", _plan_text(release.get("rollback_plan"), rollback=True)),
              ("验证计划", _plan_text(release.get("validation_plan"))),
              ("资料说明", _material_text(release.get("material_notice")))]
    entries = ["- " + label + "：" + plain(text) for label, text in fields if text]
    return ["## 本次变更信息", "", *entries, ""] if entries else []


def _source_notice(payload, mode):
    """Keep model execution mode independent from locally declared data origin."""
    rows = payload.get("evidence", [])
    flags = [row.get("synthetic") for row in rows if isinstance(row, dict)]
    if flags and all(flag is True for flag in flags):
        origin = "模拟材料标记synthetic=true；不能据此认定已接入生产。"
    elif any(flag is False for flag in flags):
        origin = "材料包含用户提供的本地记录（synthetic=false），尚未独立核实；未证明已连接生产系统。"
        if any(flag is True for flag in flags):
            origin += "其中synthetic=true的记录仍是模拟资料。"
    else:
        origin = "尚未取得可识别来源的材料，不能据此认定已接入生产。"
    model = ("本次模型为固定脚本，不代表Qwen效果。" if mode == "demo" else
             "本次模型通过配置的API调用；模型调用不构成材料真实性验证。")
    return origin + model


def save_report(review_id: str, state: dict, runtime_dir: Path, metadata=None):
    root = Path(runtime_dir) / "reports"
    root.mkdir(parents=True, exist_ok=True)
    payload = sanitize(dict(state, review_id=review_id, metadata=metadata or {}))
    json_path = root / (review_id + ".json")
    md_path = root / (review_id + ".md")
    scope = payload.get("scope", {})
    analysis = payload.get("analysis") or {}
    if not isinstance(analysis, dict):
        analysis = {"summary": str(analysis)}
    lines = ["# 发布风险评审报告", "", f"- 评审编号：{review_id}",
        f"- 变更编号：{plain(payload.get('change_id', ''))}",
        f"- 调查范围：{plain(scope.get('service', '未知'))} / {plain(scope.get('environment', '未知'))}",
        f"- 状态：{plain(payload.get('status', 'unknown'))}",
        f"- 评审材料指纹：{plain(payload.get('review_digest', '尚未生成'))}",
        f"- 模型模式：{plain((metadata or {}).get('mode', payload.get('mode', '未知')))}",
        f"- 协调后端：{plain((metadata or {}).get('coordination', '未知'))}", "",
        "本报告是发布风险评审材料；acknowledge只确认已阅读，不批准发布。没有执行发布、回滚或其他生产变更。",
        _source_notice(payload, (metadata or {}).get("mode", payload.get("mode"))), ""]
    lines += _change_information(payload)
    lines += ["## 规则检查结果", "", "| 级别 | 规则 | 发现与说明 | 证据 |", "|---|---|---|---|"]
    for finding in payload.get("policy_findings", []):
        lines.append("| " + " | ".join([plain(finding.get("severity", "")), plain(finding.get("code", "")),
            plain(finding.get("title", "")) + "；" + plain(finding.get("detail", "")),
            plain(", ".join(finding.get("evidence_ids", [])))]) + " |")
    if not payload.get("policy_findings"):
        lines.append("| — | — | 未生成规则发现；不能据此认为发布安全 | — |")
    lines += ["", "## 模型辅助分析（需复核）", "", plain(analysis.get("summary", "尚未生成分析。"))]
    for finding in analysis.get("risks", []):
        if isinstance(finding, dict):
            text = finding.get("detail", finding.get("description", finding.get("text", "")))
            lines.append("- " + plain(finding.get("title", "候选风险")) + "：" + plain(text) +
                         "；引用=" + plain(", ".join(finding.get("evidence_ids", []))))
            for check in finding.get("verification", []):
                lines.append("  - 待验证：" + plain(check))
    for gap in analysis.get("missing_evidence", []):
        lines.append("- 待补充：" + plain(gap))
    for check in analysis.get("process_checks", []):
        lines.append("- 流程待核查：" + plain(check))
    lines += ["", "## 审核与质量状态", ""]
    lines.append("质量问题：" + plain(json.dumps(payload.get("quality_issues", []), ensure_ascii=False)))
    lines.append("人工记录：" + plain(json.dumps(payload.get("review_record", {}), ensure_ascii=False)))
    lines += ["", "## 工具与证据", "", "| ID | 类型 | 来源服务 | 模拟数据 |", "|---|---|---|---|"]
    for row in payload.get("evidence", []):
        lines.append("| " + " | ".join(plain(row.get(key, "")) for key in ("id", "kind", "service", "synthetic")) + " |")
    lines += ["", "## 执行观测", "", "```json",
              json.dumps({key: payload.get(key) for key in ("tool_records", "model_calls", "execution")}, ensure_ascii=False, indent=2),
              "```", "", "工具返回的证据存在，不代表模型每句话都被证据充分支持；最终决定须依照独立的发布管理流程。", ""]
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path
