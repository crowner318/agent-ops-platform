"""Probe queue and storage for the web process, or the worker heartbeat."""
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen


def worker_healthy(path, now=None):
    try:
        age = (time.time() if now is None else now) - Path(path).stat().st_mtime
    except OSError:
        return False
    return 0 <= age <= 25


def main():
    if os.environ.get("WORKBENCH_ROLE") == "worker":
        if not worker_healthy(os.environ.get("KAFKA_WORKER_READY_FILE", "/tmp/agent-kafka-worker.ready")):
            raise SystemExit(1)
        return
    with urlopen("http://127.0.0.1:8765/api/storage-status", timeout=5) as response:
        payload = json.load(response)
    queue = payload.get("queue", {})
    healthy = (payload.get("mode") == "java" and payload.get("reports") == "redis"
               and isinstance(queue, dict) and queue.get("mode") == "kafka" and queue.get("available") is True
               and all(isinstance(payload.get(name), dict) and payload[name].get("available") is True
                       for name in ("java", "sqlite", "redis")))
    if not healthy:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
