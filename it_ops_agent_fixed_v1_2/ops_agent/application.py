"""共享基础取证、复杂度路由、协作汇总和执行指标。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import time

from .engine import AgentLoop, AgentResult, compact_tool_result
from .harness import redact
from .reporting import prepare_final_answer
from .materials import summarize_materials


@dataclass
class RunResult:
    session_id: str
    mode: str
    answer: str
    evidence: list[dict]
    warnings: list[str]
    complete: bool
    agents: list[dict]
    case_id: str
    execution: dict
    quality_issues: list[dict]


def choose_mode(question: str) -> str:
    terms = ("跨服务", "多个服务", "跨系统", "并行排查", "复杂故障", "swarm", "cross-service")
    service_names = set(re.findall(r"(?<![a-z0-9_-])[a-z0-9_-]+-service(?![a-z0-9_-])", question.casefold()))
    return "swarm" if any(t in question.casefold() for t in terms) or len(service_names) > 1 else "single"


def execution_summary(results: list[AgentResult], registry, elapsed_ms: float,
                      prefetch: dict | None) -> dict:
    calls = [call for result in results for call in result.model_calls]
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens")
    known = {key: sum(call.get("usage", {}).get(key) or 0 for call in calls)
             if any(isinstance(call.get("usage", {}).get(key), int) for call in calls) else None for key in keys}
    return {"elapsed_ms": round(elapsed_ms, 3), "model_steps": sum(r.model_steps for r in results),
        "api_attempts": sum(c.get("attempts") or 0 for c in calls)
            if all(c.get("attempts") is not None for c in calls) else None,
        "model_tool_requests": sum(r.tool_calls for r in results),
        "format_repair_requests": sum(r.format_repair_count for r in results),
        "quality_repair_requests": sum(r.quality_repair_count for r in results),
        "protocol_events": [dict(event, role=r.role) for r in results for event in r.protocol_events],
        "stage_status": [{"role": r.role, "complete": r.complete,
                          "quality_issue_count": len(r.quality_issues)} for r in results],
        "prefetch_requests": 1 if prefetch is not None else 0,
        "cache": registry.cache_stats(), "usage": known,
        "usage_complete": bool(calls) and all(all(isinstance(c.get("usage", {}).get(key), int)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")) for c in calls),
        "usage_reported_calls": sum(bool(c.get("usage_reported")) for c in calls),
        "usage_unknown_calls": sum(not c.get("usage_reported") for c in calls),
        "usage_partial_calls": sum(bool(c.get("usage_reported")) and not all(
            isinstance(c.get("usage", {}).get(key), int) for key in ("prompt_tokens", "completion_tokens", "total_tokens")) for c in calls),
        "usage_field_reported_calls": {key: sum(isinstance(c.get("usage", {}).get(key), int) for c in calls) for key in keys},
        "cache_usage_complete": bool(calls) and all(isinstance(c.get("usage", {}).get("cached_prompt_tokens"), int) for c in calls),
        "model_calls": calls, "prefetch": prefetch,
        "cost": None, "cost_note": "未配置计费单价；缺失usage不会按0估算费用。"}


class Orchestrator:
    def __init__(self, client_factory, registry, harness, memory, session_id: str,
                 service: str, environment: str, progress=None, prefetch=True):
        harness.validate_scope(service, environment)
        if registry.service != service or registry.environment != environment:
            raise ValueError("工具注册表与编排器服务/环境不一致。")
        self.client_factory, self.registry = client_factory, registry
        self.harness, self.memory = harness, memory
        self.session_id, self.service, self.environment = session_id, service, environment
        self.progress = progress or (lambda event: None)
        self.prefetch = prefetch

    def run(self, question: str, mode: str = "auto") -> RunResult:
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            raise ValueError("问题必须是1至16000字符的非空文本。")
        if mode not in {"auto", "single", "swarm"}:
            raise ValueError("mode仅支持auto/single/swarm。")
        begun = time.monotonic()
        deadline = begun + self.harness.max_seconds
        mode = choose_mode(question) if mode == "auto" else mode
        recent = self.memory.recent_dialogue(self.session_id, self.service, self.environment, limit=8)
        history = json.dumps([{"kind": row["kind"], "payload": row.get("payload", {})}
                              for row in recent], ensure_ascii=False)
        scope = {"service": self.service, "environment": self.environment,
                 "start_time": self.registry.start_time, "end_time": self.registry.end_time}
        context = {"scope": scope, "workflow": mode, "l1_recent": history[-8000:],
            "l2_verified_cases": self.memory.search_cases(self.service, self.environment, question, limit=3),
            "l3_verified_practices": self.memory.search_practices(self.service, self.environment, question, limit=3)}
        context["l2_verified_cases"] = [{key: (value[:1500] if isinstance(value, str) else value)
            for key, value in row.items() if key in {"id", "service", "environment", "summary", "relevance"}}
            for row in context["l2_verified_cases"]]
        context["l3_verified_practices"] = [{key: (value[:1000] if isinstance(value, str) else value)
            for key, value in row.items() if key in {"id", "case_id", "text", "relevance"}}
            for row in context["l3_verified_practices"]]
        self.memory.add_event(self.session_id, self.service, self.environment, "question", {"text": question, "mode": mode})
        prefetch_record = None
        if self.prefetch:
            args = {"kinds": ["metrics", "logs", "traces", "changes"]}
            self.harness.validate_tool("diagnosis", "collect_evidence", args, workflow=mode)
            self.progress({"role": "prefetch", "event": "tool", "name": "collect_evidence"})
            output = compact_tool_result(self.registry.execute("collect_evidence", args))
            if len(json.dumps(output, ensure_ascii=False)) > self.harness.max_output_chars:
                raise ValueError("基础取证结果超过字符预算，请缩小时间窗或使用--no-prefetch分项查询。")
            prefetch_record = {"role": "prefetch", "origin": "prefetch", "name": "collect_evidence",
                "arguments": args, "ok": bool(output.get("ok")), "cache_hit": bool(output.get("cache_hit")),
                "duration_ms": output.get("duration_ms"), "evidence_count": len(output.get("evidence", []))}
            self.memory.add_event(self.session_id, self.service, self.environment, "tool", dict(prefetch_record, result=output))
            self.progress(dict(prefetch_record, event="tool_result"))
            context["prefetch_summary"] = output.get("data", {})
        context["evidence"] = self.registry.observed_evidence()
        context["available_materials"] = summarize_materials(context["evidence"])

        def investigate(role, ctx):
            loop = AgentLoop(self.client_factory(), self.registry, self.harness, self.memory,
                self.session_id, self.service, self.environment, self.progress)
            return loop.run(role, question, ctx, deadline)

        results = []
        if self.prefetch and not any(r.get("current_telemetry") for r in context["evidence"]):
            prepared = prepare_final_answer("", context["evidence"], scope, "diagnosis")
            results.append(AgentResult("diagnosis", prepared["answer"], evidence=context["evidence"],
                warnings=["基础取证未得到当前遥测，本次不调用模型。"], complete=False,
                quality_issues=prepared["issues"], analysis=prepared.get("analysis", {})))
        elif mode == "single":
            results.append(investigate("diagnosis", context))
        else:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ops") as pool:
                futures = [pool.submit(investigate, role, context) for role in ("triage", "diagnosis")]
                for future in as_completed(futures):
                    results.append(future.result())
            results.sort(key=lambda r: r.role)
            # 传递验证后的结构化摘要，不重复粘贴各Agent的完整事实表。
            summaries = [{"role": r.role, "complete": r.complete,
                "hypotheses": r.analysis.get("hypotheses", [])[:3],
                "recommendations": r.analysis.get("recommendations", [])[:2],
                "process_checks": r.analysis.get("process_checks", [])[:2],
                "missing_evidence": r.analysis.get("missing_evidence", [])[:5],
                "quality_issues": r.quality_issues[:5]} for r in results]
            shared_rows = self.registry.observed_evidence()
            shared = dict(context, evidence=shared_rows, available_materials=summarize_materials(shared_rows), specialist_results=summaries)
            if time.monotonic() < deadline:
                results.append(investigate("remediation", shared))
            else:
                prepared = prepare_final_answer("", shared["evidence"], scope, "remediation")
                results.append(AgentResult("remediation", prepared["answer"], evidence=shared["evidence"],
                    warnings=["任务预算耗尽，处置汇总未完成。"], complete=False, quality_issues=prepared["issues"]))
        evidence = self.registry.observed_evidence()
        warnings = [f"{r.role}: {w}" for r in results for w in r.warnings]
        if prefetch_record is not None and not prefetch_record["ok"]:
            warnings.insert(0, "基础取证存在失败分支，仅使用成功取得的证据；请查看执行指标中的prefetch结果。")
        complete = all(r.complete for r in results)
        answer = results[-1].answer
        issues = [dict(issue, role=r.role) for r in results for issue in r.quality_issues]
        case_id = self.memory.save_case(self.session_id, self.service, self.environment, answer, verified=False)
        execution = execution_summary(results, self.registry, (time.monotonic() - begun) * 1000, prefetch_record)
        self.memory.add_event(self.session_id, self.service, self.environment, "answer", {
            "text": answer[:12000], "complete": complete, "case_id": case_id, "execution": execution,
            "quality_issues": issues})
        return RunResult(self.session_id, mode, str(redact(answer)), evidence, warnings, complete,
                         [asdict(r) for r in results], case_id, execution, issues)


def save_report(result: RunResult, output_dir: Path, service: str, environment: str,
                start: str, end: str, demo: bool) -> tuple[Path, Path]:
    from uuid import uuid4
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.session_id}-{uuid4().hex[:8]}"
    md_path, json_path = output_dir / f"{stem}.md", output_dir / f"{stem}.json"
    ex = result.execution
    lines = ["# IT运维排查报告", "", f"- 会话：{result.session_id}",
        f"- 范围：{service} / {environment}", f"- 查询时间窗：{start} — {end}",
        f"- 模式：{'离线脚本演示' if demo else '模型API'} / {result.mode}",
        f"- 状态：{'已生成待审核结论' if result.complete else '未完成或需要复核'}",
        f"- 案例：{result.case_id}（尚未人工确认）", "", result.answer,
        "", "## 执行指标", "", f"- 端到端处理耗时：{ex['elapsed_ms'] / 1000:.3f}秒（不含报告文件写入）",
        f"- 模型步骤：{ex['model_steps']}；HTTP尝试：{ex['api_attempts']}",
        f"- 模型工具请求：{ex['model_tool_requests']}；程序预取批次：{ex['prefetch_requests']}",
        f"- 报告纠正请求：格式={ex['format_repair_requests']}、质量={ex['quality_repair_requests']}；各阶段：{json.dumps(ex['stage_status'], ensure_ascii=False)}",
        f"- 协议兼容与整理记录：{json.dumps(ex['protocol_events'], ensure_ascii=False)}",
        f"- 叶子工具实际执行/缓存：{json.dumps(ex['cache'], ensure_ascii=False)}",
        f"- API已报告token：{json.dumps(ex['usage'], ensure_ascii=False)}",
        f"- usage完整：{ex['usage_complete']}；未报告usage的调用：{ex['usage_unknown_calls']}",
        f"- usage部分报告的调用：{ex['usage_partial_calls']}；缓存token统计完整：{ex['cache_usage_complete']}",
        "- 离线脚本不产生API用量；usage缺失代表未知。未配置价格，不估算费用。"]
    if result.warnings:
        lines += ["", "## 运行与质量提示", ""] + ["- " + w for w in result.warnings]
    lines += ["", "## 证据索引", "", "| ID | 时间 | 类型 |", "|---|---|---|"]
    for row in result.evidence:
        cells = [str(row.get(k, "")).replace("|", "\\|").replace("\n", " ")
                 for k in ("id", "timestamp", "evidence_type")]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "事实由工具观测生成；语义规则仅覆盖已定义的过度推断模式，不能证明通用因果关系。",
              "本程序只读取数据并生成计划，不包含生产变更执行器。", ""]
    md_path.write_text(str(redact("\n".join(lines))), encoding="utf-8")
    payload = dict(asdict(result), scope={"service": service, "environment": environment,
                                        "start": start, "end": end}, demo=demo)
    json_path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
