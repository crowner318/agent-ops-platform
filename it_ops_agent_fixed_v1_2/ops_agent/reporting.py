"""Bounded final-report checks, not semantic proof, causality or authorization.

Canonical observations alone supply the fact section. Model text is displayed
only as an unverified candidate, proposal, evidence gap or process question.
The known-phrase checks are limited and do not establish general entailment.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any

from .harness import redact
from .protocol import strict_json_document
from .materials import summarize_materials


_TEXT = {"type": "string", "minLength": 1, "maxLength": 600}
_SHORT = {"type": "string", "minLength": 1, "maxLength": 300}
_IDS = {"type": "array", "maxItems": 30, "items": {"type": "string", "minLength": 1, "maxLength": 128}}
FINAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["hypotheses", "recommendations", "missing_evidence", "process_checks"],
    "properties": {
        "hypotheses": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "evidence_ids", "validation"],
            "properties": {"text": _TEXT, "evidence_ids": _IDS,
                "validation": {"type": "array", "minItems": 1, "maxItems": 4, "items": _SHORT}}}},
        "recommendations": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "evidence_ids"], "properties": {"text": _TEXT, "evidence_ids": _IDS}}},
        "missing_evidence": {"type": "array", "maxItems": 10, "items": _SHORT},
        "process_checks": {"type": "array", "maxItems": 5, "items": _SHORT},
    },
}
_KEYS = FINAL_SCHEMA["required"]
_LIMITATION = ("程序仅核对结构、引用范围和已知过度推断模式，不能证明文字被证据语义支持，"
               "不能确认因果、校准置信度或批准变更。所有候选与建议仍需人工复核。")


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _literal(value: Any, limit: int = 500) -> str:
    text = json.dumps(redact(value), ensure_ascii=False, allow_nan=False, sort_keys=True)
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[:limit] + "…（数据节选）"


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", str(redact(value))).replace("`", "").replace("#", "＃").strip()


def _bounded_top_arrays(parsed: dict, normalizations: list[dict]) -> dict:
    """Only normalize top-level array counts; retain original item ordering.

    Every original item still has to satisfy its structural schema. Strings,
    nested arrays, missing fields and extra fields are never repaired here.
    """
    if not isinstance(parsed, dict):
        raise ValueError("invalid_structure")
    if set(parsed) != set(_KEYS):
        raise ValueError("unknown_keys" if set(parsed) - set(_KEYS) else "invalid_structure")
    result = dict(parsed)
    for key in _KEYS:
        schema, value = FINAL_SCHEMA["properties"][key], parsed[key]
        if isinstance(value, list) and len(value) > schema["maxItems"]:
            if not all(_matches_schema(item, schema["items"]) for item in value):
                raise ValueError("invalid_structure")
            result[key] = value[:schema["maxItems"]]
            normalizations.append({"code": "top_level_array_limited", "path": key,
                "original_count": len(value), "limit": schema["maxItems"],
                "dropped_count": len(value) - schema["maxItems"]})
    return result


def _matches_schema(value: Any, schema: dict) -> bool:
    if schema["type"] == "object":
        return (isinstance(value, dict) and set(value) == set(schema["required"])
                and all(_matches_schema(v, schema["properties"][k]) for k, v in value.items()))
    if schema["type"] == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value) <= schema["maxItems"]
                and all(_matches_schema(v, schema["items"]) for v in value))
    return (isinstance(value, str) and bool(value.strip())
            and schema["minLength"] <= len(value) <= schema["maxLength"])


_RULES = (
    ("unverified_certainty", re.compile(
        r"已(?:经)?(?:成功)?恢复|恢复成功|确认(?:了)?根因|证实(?:了)?根因|"
        r"根因(?:已|已经)(?:确定|确认|证实)|已(?:经)?(?:确定|确认|证实)(?:了)?(?:故障)?根因|"
        r"排除(?:了)?(?:连接)?泄漏|不可能(?:存在|发生)?(?:连接)?泄漏|"
        r"(?:连接)?泄漏(?:已|可以)(?:被)?排除|"
        r"confirmed\s+root\s+cause|root\s+cause\s+(?:is\s+)?confirmed|"
        r"(?:service\s+)?has\s+recovered|service\s+recovered|recovery\s+(?:is\s+)?confirmed|"
        r"(?:rule[ds]?\s+out|excluded?|impossible)\s+(?:a\s+)?(?:connection\s+)?leak", re.I)),
    ("unsupported_availability", re.compile(
        r"可用性(?:受损|下降|损失|降低)|(?:窗口|全程|总体|整体|全时段)[^，,。；;\n!?！？但却]{0,12}?可用性|"
        r"availability[^，,。；;\n!?！？]{0,20}?(?:loss|degrad|\d+(?:\.\d+)?\s*%)", re.I)),
    ("unsupported_continuity", re.compile(
        r"持续(?:至|到|直到)[^，,。；;\n!?！？但却]{0,12}?(?:窗口|时窗)[^，,。；;\n!?！？但却]{0,5}?(?:结束|末|终点)|"
        r"覆盖(?:了)?(?:整个|全部|完整)(?:查询)?(?:时间窗|时窗|窗口)|"
        r"(?:throughout|until).{0,16}(?:the\s+)?(?:query\s+)?window(?:\s+end)?", re.I)),
    ("unsupported_counterfactual", re.compile(
        r"(?:即使|即便|哪怕).{0,20}(?:流量|负载).{0,20}(?:足以|必然|一定).{0,12}饱和|"
        r"(?:缩池|连接池.{0,12}(?:缩小|降低|减少)).{0,16}(?:足以|必然|一定).{0,10}饱和", re.I)),
    ("uncalibrated_confidence", re.compile(
        r"[高中低]置信度|置信度\s*[:：]?\s*\d|confidence\s*[:：]?\s*(?:high|medium|low|\d)", re.I)),
)
_PROCESS_ASSERTION = re.compile(
    r"缺(?:少|乏)审批|未(?:经|通过)(?:过)?审批|没有审批|"
    r"未经(?:过)?(?:容量|性能|负载)评估|未(?:进行|做|开展|通过)?(?:容量|性能|负载)评估|"
    r"无告警|没有告警|缺(?:少|乏)告警|"
    r"no\s+approval|without\s+(?:a\s+)?capacity\s+assessment|no\s+capacity\s+assessment|no\s+alerts?", re.I)


def _process_evidence_gap(text: str, match: re.Match) -> bool:
    """Distinguish missing review/alert documents from missing processes.

    Only an explicit document noun completes this grammar. It does not make
    "缺少告警导致故障" or "未经容量评估即上线" an evidence-gap statement.
    """
    if not re.fullmatch(r"(?:缺少|缺乏|没有|无)(?:审批|告警)", match.group()):
        return False
    following = text[match.end():]
    local = re.split(r"[，,。；;\n!?！？：:]|但是|但|然而|却|\bbut\b|\bhowever\b",
                     following, maxsplit=1, flags=re.I)[0]
    # Allow bounded noun-phrase modifiers and conjunctions, rather than a
    # growing vocabulary of alert/approval subtypes. An assertion connector
    # cannot bridge a missing-process claim to a later document noun.
    document = re.search(r"记录|材料|证据|日志|单据|配置信息|配置详情|配置快照|明细|报告", local[:48])
    if document is None or document.start() > 46:
        return False
    modifier = local[:document.start()]
    if re.search(r"导致|造成|说明|证明|所以|因此|是(?!否)|已经|"
                 r"\b(?:caus(?:e[ds]?|ing)|proves?|shows?|therefore|because|already|is)\b", modifier, re.I):
        return False
    remainder = local[document.end():]
    return not remainder.strip() or bool(re.match(r"用于|以便|用以|以确认|以核实|供核查|供确认", remainder))


def _qualified(text: str, start: int, code: str | None = None) -> bool:
    before = text[max(0, start - 80):start]
    # A denial/question applies only to its local clause, not a new assertion
    # after punctuation or an adversative such as "but".
    before = re.split(r"[，,。；;\n!?！？]|但是|但|然而|却|\bbut\b|\bhowever\b", before, flags=re.I)[-1]
    # These are narrow grammatical negations observed in real responses, not
    # an exemption for arbitrary occurrences of "未" or "不" in a paragraph.
    if code == "unverified_certainty" and before.endswith("未") and text.startswith("确认根因", start):
        return True
    if code == "unsupported_continuity" and re.search(r"不等于.{0,20}$", before):
        return True
    if code == "unsupported_availability" and re.search(r"不以.{1,20}推算.{0,8}$", before):
        return True
    return bool(re.search(
        r"(?:尚未|未能|不能|无法|不得|不应|不可|避免|不要|并非|没有证据|不足以|缺少|缺乏|尚缺|未提供).{0,40}$|"
        r"(?:核查|验证|检查|确认是否|是否|能否|verify|check\s+whether|determine\s+whether).{0,40}$",
        before, re.I))


def _unknown_inline_ids(text: str, valid_ids: set[str], path: str, issues: list[dict]) -> bool:
    # References embedded in prose are references too, even when evidence_ids
    # itself contains only legitimate IDs. Generic semantic claims remain out
    # of scope; this catches the explicit citation syntax used by the report.
    refs = set(re.findall(r"\[([^\[\]\r\n]{1,128})\]", text))
    if refs - valid_ids - {"REDACTED"}:
        issues.append({"code": "unknown_evidence_ids", "path": path,
                       "message": "该项正文内嵌了本次证据中不存在的引用，已移除。"})
        return True
    return False


def _overclaims(text: str, path: str, issues: list[dict], end: str | None,
                last: str | None, process: bool = False) -> bool:
    codes = set()
    for code, pattern in _RULES:
        for match in pattern.finditer(text):
            if code == "unsupported_counterfactual" and re.search(r"是否|能否|\bwhether\b", match.group(), re.I):
                continue
            if not _qualified(text, match.start(), code):
                codes.add(code)
    if not process:
        for match in _PROCESS_ASSERTION.finditer(text):
            if _process_evidence_gap(text, match):
                continue
            if not _qualified(text, match.start()):
                codes.add("unverified_process_cause")
    end_dt, last_dt = _timestamp(end), _timestamp(last)
    if end_dt and last_dt and end_dt > last_dt:
        until = re.compile(r"(?:持续(?:至|到|直到)|until)[^，,。；;\n!?！？但却]{0,12}?" + re.escape(end_dt.strftime("%H:%M")), re.I)
        for match in until.finditer(text):
            if not _qualified(text, match.start(), "unsupported_continuity"):
                codes.add("unsupported_continuity")
    for code in sorted(codes):
        issues.append({"code": code, "path": path,
                       "message": "该项含已知的未经验证结论，已从候选正文移除；请按采样证据或待核查问题重写。"})
    return bool(codes)


def _canonical_facts(evidence: list[dict], scope: dict, issues: list[dict]) -> tuple[list[dict], list[dict], dict]:
    start, end = scope.get("start_time", scope.get("start")), scope.get("end_time", scope.get("end"))
    start_dt, end_dt = _timestamp(start), _timestamp(end)
    if ((start is not None and start_dt is None) or (end is not None and end_dt is None)
            or (start_dt and end_dt and start_dt > end_dt)):
        issues.append({"code": "invalid_scope", "path": "scope", "message": "查询时间范围无效，不能据此确认观测覆盖。"})
    records, seen = [], set()
    for row in evidence if isinstance(evidence, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            issues.append({"code": "invalid_evidence", "path": "evidence", "message": "忽略格式无效的证据记录。"})
            continue
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        if (scope.get("service") and row.get("service") != scope["service"]
                or scope.get("environment") and row.get("environment") not in (scope["environment"], "*")):
            issues.append({"code": "out_of_scope_evidence", "path": "evidence", "message": "忽略服务或环境不匹配的证据。"})
            continue
        timestamp = _timestamp(row.get("timestamp"))
        current = row.get("current_telemetry") is True or row.get("evidence_type") == "current_telemetry"
        if current and (timestamp is None or start_dt and timestamp < start_dt or end_dt and timestamp > end_dt):
            issues.append({"code": "out_of_scope_evidence", "path": "evidence", "message": "忽略时间缺失或位于查询范围外的当前遥测。"})
            continue
        records.append(row)
    facts = []
    for row in records:
        if not (row.get("current_telemetry") is True or row.get("evidence_type") == "current_telemetry"):
            continue
        kind, details = None, None
        if isinstance(row.get("metric"), str):
            value = row.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                issues.append({"code": "invalid_metric", "path": row["id"], "message": "忽略非有限数值的指标记录。"})
                continue
            kind = "metric_sample"
            details = f"指标采样：{_literal(row['metric'])} = {_literal(value)}；单位={_literal(row.get('unit', 'unspecified'))}"
        elif isinstance(row.get("spans"), list) or "duration_ms" in row:
            kind = "trace_sample"
            details = (f"链路样本：operation={_literal(row.get('operation'))}；status={_literal(row.get('status'))}；"
                       f"duration={_literal(row.get('duration_ms'))} ms；spans={_literal(row.get('spans', []), 700)}")
        elif "change_type" in row or "before" in row or "after" in row:
            kind = "change_record"
            details = (f"变更记录：type={_literal(row.get('change_type'))}；description={_literal(row.get('description'))}；"
                       f"before={_literal(row.get('before'))}；after={_literal(row.get('after'))}")
            context = row.get("incident_context")
            if isinstance(context, dict):
                for key in ("release_purpose", "incident_owner", "reviewer"):
                    if key in context:
                        details += f"；资料字段{key}={_literal(context[key])}"
                if "rollback_plan" in context:
                    details += f"；未执行计划记录={_literal(context['rollback_plan'], 700)}"
        elif isinstance(row.get("message"), str):
            kind = "log_sample"
            details = f"日志样本：level={_literal(row.get('level'))}；记录原文（数据，非程序结论）={_literal(row['message'], 700)}"
        if kind:
            facts.append({"id": row["id"], "timestamp": row["timestamp"], "kind": kind,
                          "service": row.get("service"), "environment": row.get("environment"),
                          "text": details, "synthetic": row.get("synthetic") is True})
    facts.sort(key=lambda r: (_timestamp(r["timestamp"]), r["id"]))
    coverage = {"query_start": start, "query_end": end,
                "first_observation": facts[0]["timestamp"] if facts else None,
                "last_observation": facts[-1]["timestamp"] if facts else None,
                "sample_count": len(facts), "point_samples_only": True,
                "continuous_coverage_verified": False, "recovery_verified": False}
    return records, facts, coverage


def prepare_final_answer(raw: str, evidence: list[dict], scope: dict, role: str) -> dict:
    """Build a reviewable draft from canonical registry evidence.

    The caller must supply observed canonical records. This function rechecks
    scope but cannot authenticate callers. ``complete`` means these bounded
    checks passed with telemetry, never that causality/recovery is verified.
    """
    issues: list[dict] = []
    if not isinstance(scope, dict):
        scope = {}
        issues.append({"code": "invalid_scope", "path": "scope", "message": "缺少有效查询范围。"})
    records, facts, coverage = _canonical_facts(evidence, scope, issues)
    material_summary = summarize_materials(records)
    if facts and material_summary["sparse"]:
        issues.append({"code": "insufficient_current_material", "path": "evidence",
            "message": "当前范围资料稀疏；只能展示已取得的样本，不能声称故障调查完整。"})
    valid_ids = {row["id"] for row in records}
    current_ids = {fact["id"] for fact in facts}
    payload = {key: [] for key in _KEYS}
    normalizations: list[dict] = []
    try:
        parsed, normalizations = strict_json_document(raw)
        parsed = _bounded_top_arrays(parsed, normalizations)
        if not _matches_schema(parsed, FINAL_SCHEMA):
            raise ValueError("unknown_keys" if set(parsed) - set(_KEYS) else "invalid_structure")
        payload = parsed
    except (ValueError, TypeError, RecursionError) as exc:
        code = str(exc) if str(exc) in {"unknown_keys", "invalid_structure", "duplicate_json_key"} else "invalid_json"
        issues.append({"code": code, "path": "final", "message": "模型最终输出未通过严格JSON合同，已使用程序生成的保守报告。"})
    kept = {key: [] for key in _KEYS}
    for group in ("hypotheses", "recommendations"):
        for index, item in enumerate(payload[group]):
            path = f"{group}[{index}]"
            ids = list(dict.fromkeys(item["evidence_ids"]))
            if set(ids) - valid_ids:
                issues.append({"code": "unknown_evidence_ids", "path": path, "message": "该项引用并非本次范围内实际取得的证据，已移除。"})
                continue
            if group == "hypotheses" and not ids:
                issues.append({"code": "unsupported_hypothesis", "path": path, "message": "该候选没有本次证据引用，不能作为有证据支持的候选展示。"})
                continue
            if group == "hypotheses" and not current_ids.intersection(ids):
                issues.append({"code": "historical_only_hypothesis", "path": path,
                               "message": "该候选未引用当前遥测，历史知识不能单独支持当前故障候选，已移除。"})
                continue
            bad = _unknown_inline_ids(item["text"], valid_ids, path + ".text", issues)
            bad = _overclaims(item["text"], path + ".text", issues, coverage["query_end"], coverage["last_observation"]) or bad
            for i, validation in enumerate(item.get("validation", [])):
                bad = _unknown_inline_ids(validation, valid_ids, path + f".validation[{i}]", issues) or bad
                bad = _overclaims(validation, path + f".validation[{i}]", issues, coverage["query_end"], coverage["last_observation"]) or bad
            if not bad:
                clean = dict(item, text=_plain(item["text"]), evidence_ids=ids)
                if "validation" in clean:
                    clean["validation"] = [_plain(text) for text in clean["validation"]]
                kept[group].append(clean)
    for group in ("missing_evidence", "process_checks"):
        for i, text in enumerate(payload[group]):
            invalid_reference = _unknown_inline_ids(text, valid_ids, f"{group}[{i}]", issues)
            invalid_claim = _overclaims(text, f"{group}[{i}]", issues, coverage["query_end"], coverage["last_observation"], process=group == "process_checks")
            if not invalid_reference and not invalid_claim:
                kept[group].append(_plain(text))
    if not facts:
        issues.append({"code": "no_current_telemetry", "path": "evidence", "message": "缺少指定范围内当前遥测，不能确认故障、根因、持续时间或恢复状态。"})
        kept["hypotheses"] = []
    lines = ["## 程序生成的观测事实", ""]
    if any(row.get("synthetic") is True for row in records):
        lines += ["本报告包含模拟数据（synthetic=true），未证明已连接生产系统。", ""]
    if any(row.get("synthetic") is False for row in records):
        lines += ["本报告使用用户提供的本地材料，尚未独立核实；synthetic=false不证明数据真实或已连接生产系统。", ""]
    lines += [f"查询范围：{_plain(str(scope.get('service', '未指定')))} / {_plain(str(scope.get('environment', '未指定')))}；"
              f"{_plain(str(coverage['query_start']))} 至 {_plain(str(coverage['query_end']))}。"]
    if facts:
        lines += [f"当前遥测共 {len(facts)} 条可展示的采样/记录；首条观测 {coverage['first_observation']}；最后遥测观测 {coverage['last_observation']}。",
                  "查询窗口终点不等于最后观测时间；离散样本不能证明期间持续异常、持续健康或已经恢复。", ""]
        lines.extend(f"- [{fact['id']}] {fact['timestamp']}；{fact['text']}。" for fact in facts)
    else:
        lines += ["指定范围内没有可用当前遥测，无法给出可靠诊断；请补充监控、日志、链路和变更数据。"]
    lines += ["", "指标按原始值和单位展示；点错误率不能推出整个窗口的可用性或总体用户影响。",
              "", "## 候选原因（均未经因果验证）", ""]
    for item in kept["hypotheses"]:
        refs = " ".join(f"[{eid}]" for eid in item["evidence_ids"])
        lines.append(f"- 待验证候选：{item['text']} {refs}")
        lines.extend(f"  - 待执行验证：{text}" for text in item["validation"])
    if not kept["hypotheses"]:
        lines.append("未保留可合规展示的候选原因，不能据此确认根因。")
    lines += ["", "## 待评审建议（未批准、未执行）", "",
              "所有恢复指标、阈值、目标和观察时长均为待评审建议，尚未批准；不表示已恢复。"]
    for item in kept["recommendations"]:
        refs = " ".join(f"[{eid}]" for eid in item["evidence_ids"])
        qualifier = "" if refs else "（无证据引用，仅为待评审建议）"
        lines.append(f"- {item['text']} {refs}{qualifier}")
    if not kept["recommendations"]:
        lines.append("暂无通过合同校验的模型建议；下一步应补齐证据并由运维人员评审。")
    lines += ["", "## 缺失证据", ""]
    lines.extend(f"- 待补充：{text}" for text in kept["missing_evidence"])
    if not kept["missing_evidence"]:
        lines.append("- 本次报告未列出阻碍当前分析的资料缺口；后续验证按候选与建议执行，不表示根因或恢复已确认。")
    lines += ["", "## 流程待核查问题（尚未证实）", ""]
    for text in kept["process_checks"]:
        lines.append(f"- 待核查（尚未证实）：{text}；需查阅对应记录，不能据此认定流程缺失。")
    if not kept["process_checks"]:
        lines.append("- 容量评估、审批和告警的实际执行情况尚未核实，不能认定这些流程缺失。")
    if issues:
        lines += ["", "## 程序审核提示", "", "模型输出包含格式、证据或已知过度推断问题，相关内容已移除；本报告需要复核。"]
        lines.extend(f"- {issue['code']}：{issue['message']}" for issue in issues)
    if normalizations:
        lines += ["", "## 格式规范化说明", ""]
        for item in normalizations:
            if item["code"] == "top_level_array_limited":
                lines.append(f"- {item['path']} 按原顺序保留前 {item['limit']} 项，省略 {item['dropped_count']} 项；未修改条目内容。")
            elif item["code"] == "plain_prose_removed":
                lines.append(f"- 已忽略JSON对象外普通说明：前缀 {item['prefix_chars']} 字符、后缀 {item['suffix_chars']} 字符；未将说明当作事实。")
            elif item["code"] == "json_fence_removed":
                lines.append("- 已移除一层JSON代码围栏，内容仍按完整合同校验。")
    lines += ["", _LIMITATION, "本程序只生成报告和计划，未执行生产变更；根因未确认，恢复未验证，建议阈值未批准。"]
    analysis = dict(kept, schema_version=1, role=role, coverage=coverage,
                    material_summary=material_summary,
                    deterministic_facts=facts, evidence_count=len(records), root_cause_confirmed=False,
                    recovery_verified=False, thresholds_approved=False, limitation=_LIMITATION,
                    normalizations=normalizations)
    return {"answer": "\n".join(lines), "issues": issues, "complete": bool(facts) and not issues,
            "analysis": analysis, "normalizations": normalizations}
