"""Run Agent tasks received from Kafka in an independent worker process."""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from uuid import uuid4

from .kafka_queue import kafka_config, topic_ready


GROUP_ID = "agent-platform-workers-v1"
READY_PATH = "/tmp/agent-kafka-worker.ready"
RETRY_SECONDS = 2
CLAIM_STALE_AFTER = timedelta(minutes=20)


def _utc_now():
    return datetime.now(timezone.utc)


def _event_id(value):
    if not isinstance(value, bytes) or len(value) > 80:
        return None
    if not re.fullmatch(rb"[A-Za-z0-9_-]{1,80}", value):
        return None
    return value.decode("ascii")


def execute_event(event_id, store, run_task, worker_id, stop):
    """Return True once the broker offset may safely advance."""
    while not stop.is_set():
        try:
            claim = store.dispatch_claim(event_id, worker_id, _utc_now().isoformat())
            break
        except KeyError:
            # An obsolete event has no remaining authoritative work.
            return True
        except (RuntimeError, OSError):
            stop.wait(RETRY_SECONDS)
    else:
        return False

    if not claim["claimed"]:
        return True

    try:
        if not stop.is_set():
            run_task(claim["task_id"], claim["action"])
    except Exception:
        print("Agent 执行未正常结束，正在结算任务。", file=sys.stderr, flush=True)
    # Never call run_task again if finish failed or its reply was lost. Retry
    # only the completion write and leave the offset uncommitted on shutdown.
    while True:
        try:
            store.dispatch_finish(event_id, worker_id, _utc_now().isoformat())
            return True
        except (RuntimeError, OSError):
            if stop.wait(RETRY_SECONDS):
                return False


def consume(consumer, store, run_task, worker_id, stop):
    next_recovery = 0
    while not stop.is_set():
        if time.monotonic() >= next_recovery:
            next_recovery = time.monotonic() + 60
            now = _utc_now()
            try:
                store.dispatch_recover((now - CLAIM_STALE_AFTER).isoformat(), now.isoformat())
            except (RuntimeError, OSError):
                print("暂时无法检查已中断的 Kafka 任务，稍后重试。", file=sys.stderr, flush=True)
        message = consumer.poll(1)
        if message is None or message.error():
            continue
        event_id = _event_id(message.value())
        if event_id is None:
            print("Kafka 队列中有格式无效的调度编号，已跳过。", file=sys.stderr, flush=True)
        elif not execute_event(event_id, store, run_task, worker_id, stop):
            break
        # A failed commit stops this worker so it can rejoin its consumer
        # group from the recorded offset. SQLite claims prevent re-execution.
        partitions = consumer.commit(message=message, asynchronous=False)
        if any(partition.error for partition in partitions or ()):
            raise RuntimeError("Kafka 未确认消费进度。")


class WorkerHeartbeat:
    def __init__(self, path):
        self.path = Path(path)
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        self.path.touch()
        self.thread = threading.Thread(target=self._run, name="worker-health", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.wait(4):
            try:
                self.path.touch()
            except OSError:
                return

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="从 Kafka 领取并执行工作台任务。")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)

    from confluent_kafka import Consumer
    from run_workbench import load_config
    from .service import WorkbenchService

    config = load_config(args.config)
    if config.get("queue_mode") != "kafka":
        parser.error("Kafka worker 需要 WORKBENCH_QUEUE_MODE=kafka。")
    config["role"] = "worker"
    service = WorkbenchService(config)
    stop = threading.Event()

    def handle_stop(_signal, _frame):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=service.close, name="worker-stop", daemon=True).start()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    ready_path = Path(os.environ.get("KAFKA_WORKER_READY_FILE", READY_PATH))
    ready_path.unlink(missing_ok=True)
    worker_id = "kafka-worker-" + uuid4().hex[:24]
    servers, topic = kafka_config()
    consumer = None
    try:
        consumer = Consumer({
            "bootstrap.servers": servers,
            "client.id": worker_id,
            "group.id": GROUP_ID,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "allow.auto.create.topics": False,
            "max.poll.interval.ms": 900000,
        })
        while not stop.is_set():
            try:
                if topic_ready(consumer, topic):
                    break
            except Exception:
                pass
            stop.wait(RETRY_SECONDS)
        if stop.is_set():
            return 0
        consumer.subscribe([topic])
        with WorkerHeartbeat(ready_path):
            print("Kafka 任务执行进程已就绪。", flush=True)
            consume(consumer, service.store, service._worker, worker_id, stop)
        return 0
    except Exception:
        print("Kafka 任务执行进程异常退出，已结算任务不会重复运行。", file=sys.stderr, flush=True)
        return 1
    finally:
        if consumer is not None:
            consumer.close()
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
