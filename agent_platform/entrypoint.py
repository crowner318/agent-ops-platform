"""Container-only launcher: creates paths, never embeds model credentials."""
import json
import os
from pathlib import Path
import sys


def make_config(root=Path("/app"), runtime=Path("/data/worker")):
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("workbench", "release", "empty-history", "home"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    return {"ops_project": str(root / "it_ops_agent_fixed_v1_2"),
            "release_project": str(root / "release_risk_agent"),
            "python_executable": sys.executable,
            "runtime_dir": str(runtime / "workbench"),
            "release_runtime_dir": str(runtime / "release"),
            "ops_history_project": str(runtime / "empty-history"),
            "ops_settings_file": str(root / "it_ops_agent_fixed_v1_2/settings.py")}


def main():
    if os.environ.get("WORKBENCH_CONTAINER") != "1":
        raise SystemExit("This launcher requires WORKBENCH_CONTAINER=1 inside the Compose container.")
    role = os.environ.get("WORKBENCH_ROLE", "web")
    if role not in {"web", "worker"}:
        raise SystemExit("WORKBENCH_ROLE must be web or worker.")
    config = make_config()
    config_path = Path("/tmp/agent-worker-config.json" if role == "worker" else "/data/worker/container-config.json")
    pending_config = config_path.with_suffix(".tmp")
    pending_config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    pending_config.replace(config_path)
    if role == "worker":
        Path(os.environ.get("KAFKA_WORKER_READY_FILE", "/tmp/agent-kafka-worker.ready")).unlink(missing_ok=True)
        command = [sys.executable, "-m", "workbench.kafka_worker", "--config", str(config_path)]
    else:
        command = [sys.executable, "/app/agent_workbench/run_workbench.py", "--config", str(config_path),
                   "--port", "8765", "--container-bind", "--no-browser"]
    os.chdir("/app/agent_workbench")
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
