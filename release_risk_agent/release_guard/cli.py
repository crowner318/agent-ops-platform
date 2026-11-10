"""Local review UI through CLI, durable workflow, and explicit Redis jobs."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import subprocess
from uuid import uuid4

from .config import PROJECT_ROOT, Settings, load_settings
from .reporting import sanitize, save_report

COMMANDS = {"start", "resume", "retry", "status", "list", "enqueue", "worker", "import-legacy", "check-config"}


def parser():
    root = argparse.ArgumentParser(description="发布前风险评审：MCP + LangChain + LangGraph + Redis")
    sub = root.add_subparsers(dest="command", required=True)
    for name in sorted(COMMANDS):
        p = sub.add_parser(name)
        p.add_argument("--runtime-dir", type=Path, default=PROJECT_ROOT / "runtime")
        p.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
        p.add_argument("--coordination", choices=["local", "redis"], default="local")
        p.add_argument("--redis-url", default=os.environ.get("RELEASE_REDIS_URL", "redis://127.0.0.1:6379/0"))
        if name in {"start", "enqueue"}:
            p.add_argument("--change-id", default="risk-pool")
        if name == "start":
            p.add_argument("--review-id")
        if name in {"start", "worker", "check-config"}:
            p.add_argument("--demo", action="store_true")
        if name in {"resume", "retry", "status"}:
            p.add_argument("--review-id", required=True)
        if name == "resume":
            p.add_argument("--decision", choices=["acknowledge", "request_changes", "reject"], required=True)
            p.add_argument("--reviewer", required=True)
            p.add_argument("--comment", default="")
        if name == "worker":
            p.add_argument("--max-jobs", type=int, default=1)
            p.add_argument("--consumer", default="local-worker")
        if name == "import-legacy":
            p.add_argument("--source", type=Path, required=True)
            p.add_argument("--confirmed", action="store_true", help="声明已人工审核该来源；不会修改旧项目。")
    return root


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("编号只允许1-80个字母、数字、下划线和连字符。")
    return value


def create_coordinator(args, backend=None):
    from .coordination import LocalCoordinator, RedisCoordinator
    choice = backend or args.coordination
    return RedisCoordinator(args.redis_url) if choice == "redis" else LocalCoordinator()


def is_human_pause(snapshot):
    """A checkpoint may have pending nodes without being a human interrupt."""
    return bool(getattr(snapshot, "interrupts", ())) or any(
        bool(getattr(task, "interrupts", ())) for task in getattr(snapshot, "tasks", ()))


async def run_review(args, repository, coordinator, review_id, resume=None, demo=False):
    from langgraph.types import Command
    from .workflow import build_workflow
    saved_binding = repository.get_review(review_id)
    if saved_binding is None or saved_binding["change_id"] != args.change_id or saved_binding["mode"] != ("demo" if demo else "api"):
        raise ValueError("业务评审绑定与本次变更/模型模式不一致。")
    # An offline script does not depend on or read user-owned model credentials.
    settings = Settings() if demo else load_settings()
    settings.validate(demo=demo)
    lock = coordinator.acquire_lock("review:" + review_id, ttl=900)
    if lock is None:
        raise ValueError("该评审正在处理，请稍后重试。")
    started = time.monotonic()
    try:
        callback = lambda event: repository.append_event(review_id, event.get("event", "workflow"), sanitize(event))
        async with build_workflow(settings, args.runtime_dir, args.data_dir, coordinator=coordinator,
                                  demo=demo, event_callback=callback) as graph:
            config = {"configurable": {"thread_id": review_id}, "recursion_limit": 20}
            before = await graph.aget_state(config)
            if resume is not None:
                if before.values and not before.next and before.values.get("review_record"):
                    existing = before.values["review_record"]
                    if any(existing.get(key) != resume.get(key) for key in ("reviewer", "decision", "comment")):
                        raise ValueError("流程已完成复核，不能改写原意见；相同意见可重试以补齐业务记录。")
                    # Crash recovery: the durable graph already committed the
                    # human record, but the business database may not have.
                elif not before.values or not is_human_pause(before):
                    raise ValueError("该评审不存在待处理的人工复核节点；请先查看status。")
                else:
                    resume["proposed_digest"] = before.values.get("review_digest", "")
                    await graph.ainvoke(Command(resume=resume), config=config)
            else:
                if before.values or before.next:
                    if before.values.get("change_id") not in {None, args.change_id}:
                        raise ValueError("检查点绑定了其他变更。")
                    if before.next and not is_human_pause(before):
                        # A process may have crashed after collection but before
                        # policy/model nodes. Resume, do not falsely mark human wait.
                        await graph.ainvoke(None, config=config)
                else:
                    await graph.ainvoke({"change_id": args.change_id, "review_id": review_id,
                                         "mode": "demo" if demo else "api"}, config=config)
            snapshot = await graph.aget_state(config)
            state = dict(snapshot.values)
            waiting_human = is_human_pause(snapshot)
            if waiting_human:
                state["status"] = "waiting_human"
                state["pending_nodes"] = list(snapshot.next)
            elif snapshot.next:
                raise ValueError("流程仍有未完成的自动节点；请用retry恢复，任务未确认完成。")
            if state.get("review_record"):
                record = state["review_record"]
                record_id = "hr-" + hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                repository.append_human_review(review_id, record.get("reviewer", ""), record.get("decision", ""),
                    record.get("proposed_digest", ""), record.get("comment", ""), record_id=record_id)
            state["execution"] = dict(state.get("execution", {}), last_command_elapsed_ms=round((time.monotonic()-started)*1000, 3),
                                      coordinator=coordinator.stats())
            state = sanitize(state)
            repository.save_result(review_id, state)
            saved = repository.get_review(review_id)
            md, machine = save_report(review_id, state, args.runtime_dir,
                                      {"mode": saved["mode"], "coordination": saved["coordination"]})
            print(json.dumps({"review_id": review_id, "change_id": state.get("change_id"), "status": state.get("status"),
                "policy_findings": len(state.get("policy_findings", [])), "quality_issues": state.get("quality_issues", []),
                "human_review_required": waiting_human, "deployment_executed": False,
                "markdown": str(md.resolve()), "json": str(machine.resolve())}, ensure_ascii=False, indent=2))
            if waiting_human:
                command = [sys.executable, "run.py", "resume", "--review-id", review_id, "--decision", "acknowledge",
                           "--reviewer", "你的名字", "--comment", "已阅读评审材料", "--runtime-dir", str(args.runtime_dir.resolve()),
                           "--data-dir", str(args.data_dir.resolve())]
                print("下一步：" + subprocess.list2cmdline(command))
            return 0 if state.get("status") in {"waiting_human", "review_acknowledged"} else 2
    finally:
        coordinator.release_lock("review:" + review_id, lock)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS and argv[0] not in {"-h", "--help"}:
        argv.insert(0, "start")
    args = parser().parse_args(argv)
    coordinator = None
    try:
        if args.command == "check-config":
            config = load_settings()
            config.validate(demo=args.demo)
            print(json.dumps({"model": config.model, "base_url": config.base_url,
                              "api_key_configured": bool(config.api_key), "network_request_sent": False}, ensure_ascii=False))
            return 0
        if args.command == "import-legacy":
            from .legacy import import_legacy_report
            print(json.dumps(sanitize(import_legacy_report(args.source, args.data_dir, confirmed=args.confirmed)), ensure_ascii=False, indent=2))
            return 0
        from .storage import Repository
        repository = Repository(args.runtime_dir / "reviews.sqlite3")
        if args.command == "status":
            result = repository.get_review(valid_id(args.review_id))
            if result is None:
                raise ValueError("评审不存在。")
            print(json.dumps(sanitize(result), ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            print(json.dumps(sanitize(repository.list_reviews()), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"resume", "retry"}:
            review_id = valid_id(args.review_id)
            saved = repository.get_review(review_id)
            if saved is None:
                raise ValueError("评审不存在。")
            if args.command == "resume" and (not args.reviewer.strip() or len(args.reviewer) > 80 or len(args.comment) > 2000):
                raise ValueError("复核人不能为空/超长，备注最多2000字符。")
            args.change_id = saved["change_id"]
            coordinator = create_coordinator(args, saved["coordination"])
            return asyncio.run(run_review(args, repository, coordinator, review_id,
                resume={"reviewer": args.reviewer.strip(), "decision": args.decision, "comment": args.comment}
                    if args.command == "resume" else None, demo=saved["mode"] == "demo"))
        if args.command in {"enqueue", "worker"} and args.coordination != "redis":
            raise ValueError("跨进程队列必须显式使用--coordination redis；local只用于直接运行演示。")
        coordinator = create_coordinator(args)
        if args.command == "enqueue":
            job = coordinator.enqueue(valid_id(args.change_id))
            print(json.dumps({"job_id": job, "change_id": args.change_id, "backend": "redis"}, ensure_ascii=False))
            return 0
        if args.command == "worker":
            if not 1 <= args.max_jobs <= 20:
                raise ValueError("max-jobs必须为1-20。")
            result_code = 0
            for _ in range(args.max_jobs):
                job = coordinator.claim(args.consumer, wait_ms=1000)
                if job is None:
                    print("当前没有待处理任务。")
                    break
                args.change_id = valid_id(job["change_id"])
                review_id = "q-" + hashlib.sha256(job["job_id"].encode()).hexdigest()[:24]
                repository.create_review(review_id, args.change_id, "demo" if args.demo else "api", "redis")
                saved = repository.get_review(review_id)
                if saved["change_id"] != args.change_id or saved["coordination"] != "redis":
                    raise ValueError("队列任务与原评审绑定不一致。")
                code = asyncio.run(run_review(args, repository, coordinator, review_id, demo=saved["mode"] == "demo"))
                coordinator.ack(job["job_id"])
                result_code = max(result_code, code)
            return result_code
        args.change_id = valid_id(args.change_id)
        review_id = valid_id(args.review_id or "rev-" + uuid4().hex[:16])
        if not repository.create_review(review_id, args.change_id, "demo" if args.demo else "api", args.coordination):
            raise ValueError("该review-id已存在；请查看status或使用resume。")
        repository.append_event(review_id, "submitted", {"change_id": args.change_id, "mode": "demo" if args.demo else "api"})
        return asyncio.run(run_review(args, repository, coordinator, review_id, demo=args.demo))
    except KeyboardInterrupt:
        print("任务已中断；已保存的流程可用status检查。", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not dump provider requests, headers, settings, or Redis URLs.
        message = str(sanitize(str(exc)))[:600] if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__ + "（请检查依赖、数据或服务连接）"
        print("运行失败：" + message, file=sys.stderr)
        return 1
    finally:
        if coordinator is not None:
            coordinator.close()
