"""运行入口：python run.py --demo，或填好settings.py后 python run.py。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import sys
import threading
from uuid import uuid4

from ops_agent.application import Orchestrator, save_report
from ops_agent.config import PROJECT_ROOT, load_client_config
from ops_agent.demo import DemoClient
from ops_agent.harness import Harness
from ops_agent.llm import ChatCompletionClient
from ops_agent.memory import MemoryStore
from ops_agent.skills import SkillRegistry


def parser():
    p = argparse.ArgumentParser(description="IT故障诊断Agent，模型由settings.py配置，支持Qwen/DeepSeek。")
    p.add_argument("-q", "--question", default="订单服务发布后错误率升高，请分析候选根因、给出处置建议并生成复盘草稿。")
    p.add_argument("--demo", action="store_true", help="确定性离线演示，不调用API、不测试模型效果。")
    p.add_argument("--check-config", action="store_true", help="只检查配置，不联网、不显示API Key。")
    p.add_argument("--mode", choices=["auto", "single", "swarm"], default="auto")
    p.add_argument("--no-prefetch", action="store_true", help="关闭程序基础取证，改由模型选择工具，供对照测试。")
    p.add_argument("--service", default="order-service")
    p.add_argument("--environment", default="demo")
    p.add_argument("--start", default="2026-10-19T09:55:00+08:00")
    p.add_argument("--end", default="2026-10-19T10:20:00+08:00")
    p.add_argument("--session", default=None, help="重复使用同一ID可检索同服务/环境的近期对话。")
    p.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    p.add_argument("--local-input", action="store_true", help="由操作者明确绑定本次本地资料的服务/环境；仍只读取证，不执行生产操作。")
    p.add_argument("--harness", type=Path, default=PROJECT_ROOT / "harness.yaml")
    p.add_argument("--runtime-dir", type=Path, default=PROJECT_ROOT / "runtime")
    p.add_argument("--quiet", action="store_true")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--list-cases", action="store_true")
    action.add_argument("--confirm-case", metavar="CASE_ID", help="人工审核报告后确认案例；不会执行生产操作。")
    action.add_argument("--export-sft", type=Path, metavar="JSONL", help="只导出人工确认且完整的真实API轨迹。")
    action.add_argument("--add-practice", nargs=2, metavar=("CASE_ID", "TEXT"), help="由已确认案例人工沉淀L3规则。")
    return p


def _local_input_harness(harness: Harness, service: str, environment: str) -> Harness:
    """Bind operator-selected local material to one exact scope for this process.

    The model cannot set this CLI flag or expand these values. Keep the normal
    role/tool, risk and execution-budget policy on an independent copy.
    """
    for name, value in (("服务", service), ("环境", environment)):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is None:
            raise ValueError(f"本地资料{name}仅允许1至80个英文字母、数字、下划线或连字符。")
    local = deepcopy(harness)
    local.allowed_services = [service]
    local.allowed_environments = [environment]
    return local


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.check_config:
            config = load_client_config()
            config.validate()
            print(json.dumps({"provider": config.resolved_provider, "model": config.model,
                "base_url": config.base_url, "api_key_configured": bool(config.api_key),
                "network_request_sent": False}, ensure_ascii=False, indent=2))
            return 0
        harness = Harness(args.harness)
        if args.local_input:
            harness = _local_input_harness(harness, args.service, args.environment)
        harness.validate_scope(args.service, args.environment)
        memory = MemoryStore(args.runtime_dir / "memory.sqlite3")
        if args.list_cases:
            print(json.dumps(memory.list_cases(args.service, args.environment), ensure_ascii=False, indent=2))
            return 0
        if args.confirm_case:
            scoped_ids = {r["id"] for r in memory.list_cases(args.service, args.environment)}
            if args.confirm_case not in scoped_ids:
                raise ValueError("案例不存在于指定服务/环境。")
            if not memory.confirm_case(args.confirm_case):
                raise ValueError("案例不存在。")
            print("案例已由人工确认；离线演示轨迹仍不会进入SFT导出。")
            return 0
        if args.export_sft:
            count = memory.export_sft(args.export_sft)
            print(f"已导出 {count} 条人工确认的完整API轨迹到 {args.export_sft.resolve()}")
            return 0
        if args.add_practice:
            case_id, text = args.add_practice
            practice_id = memory.add_practice(args.service, args.environment, text, case_id)
            print("已保存实践：" + practice_id)
            return 0
        session = args.session or uuid4().hex[:16]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
            raise ValueError("session仅允许1至64个英文字母、数字、下划线或连字符。")
        registry = SkillRegistry(args.data_dir, args.service, args.environment, args.start, args.end)
        if args.demo:
            factory = DemoClient
        else:
            config = load_client_config()
            config.validate()
            factory = lambda: ChatCompletionClient(config)
        lock = threading.Lock()

        def progress(event):
            if not args.quiet:
                with lock:
                    if event["event"] == "tool":
                        text = f"调用工具 {event['name']}"
                    elif event["event"] == "tool_result":
                        text = f"工具完成 {event['name']}；缓存={'命中' if event.get('cache_hit') else '未命中'}；新证据={event.get('new_evidence_count', event.get('evidence_count', 0))}"
                    else:
                        text = f"模型步骤 {event['step']}"
                    print(f"[{event['role']}] {text}", flush=True)

        print(f"会话：{session}；数据源：本地JSON；模型：{'离线脚本' if args.demo else config.model}")
        app = Orchestrator(factory, registry, harness, memory, session, args.service, args.environment,
                           progress, prefetch=not args.no_prefetch)
        result = app.run(args.question, args.mode)
        md, machine = save_report(result, args.runtime_dir / "reports", args.service,
                                  args.environment, args.start, args.end, args.demo)
        print("\n" + result.answer)
        for warning in result.warnings:
            print("运行提示：" + warning, file=sys.stderr)
        ex = result.execution
        print("\n运行状态：" + ("已生成待审核结论" if result.complete else "未完成或需要复核"))
        print("阶段状态：" + "；".join(f"{s['role']}={'通过' if s['complete'] else '未通过'}"
                                      for s in ex["stage_status"]))
        print(f"报告纠正请求：格式={ex['format_repair_requests']}；质量={ex['quality_repair_requests']}")
        print(f"\n执行统计：{ex['elapsed_ms'] / 1000:.3f}秒；模型步骤={ex['model_steps']}；"
              f"模型工具请求={ex['model_tool_requests']}；叶子工具实际执行={ex['cache']['backend_executions']}；缓存命中={ex['cache']['cache_hits']}")
        print("API用量：" + json.dumps(ex["usage"], ensure_ascii=False) +
              ("（完整）" if ex["usage_complete"] else "（部分或未知，不据此估算费用）"))
        print(f"\n报告：{md.resolve()}\nJSON：{machine.resolve()}\n案例：{result.case_id}（待人工审核）")
        return 0 if result.complete else 2
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print("启动失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
