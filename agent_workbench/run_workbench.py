"""Start the local workbench; the Agent projects keep their own credentials."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import webbrowser

from workbench.server import create_server
from workbench.version import API_VERSION, APP_ID, BUILD_ID, path_id

ROOT = Path(__file__).resolve().parent


def running_workbench(port):
    """Read only a bounded loopback response. Never follow a redirect."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/api/bootstrap", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(131073)
        if response.status != 200 or len(raw) > 131072:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, http.client.HTTPException):
        return None
    except (ValueError, RecursionError):
        return {}
    finally:
        connection.close()


def compatible_workbench(info, runtime_dir, storage_mode="legacy", queue_mode="local"):
    return (isinstance(info, dict) and info.get("api_version") == API_VERSION
            and info.get("app_id") == APP_ID and info.get("build_id") == BUILD_ID
            and info.get("runtime_id") == path_id(runtime_dir)
            and info.get("storage_mode") == storage_mode
            and info.get("queue_mode", "local") == queue_mode
            and isinstance(info.get("capabilities"), dict) and info["capabilities"].get("trash") is True)


def load_config(path: Path | None = None, runtime_dir: Path | None = None):
    values = {}
    config_path = path or ROOT / "workbench.config.json"
    if config_path.exists():
        if config_path.stat().st_size > 16384:
            raise ValueError("工作台路径配置文件过大。")
        values = json.loads(config_path.read_text(encoding="utf-8-sig"))
        allowed = {"ops_project", "release_project", "python_executable", "ops_python", "release_python", "runtime_dir", "release_runtime_dir", "ops_settings_file", "ops_history_project", "storage_mode", "storage_service_url", "queue_mode"}
        if not isinstance(values, dict) or set(values) - allowed or any(not isinstance(v, str) or not v.strip() for v in values.values()):
            raise ValueError("工作台配置只允许填写项目、Python和运行目录路径；模型密钥请继续放在各Agent配置中。")
    def resolve(value):
        p = Path(value).expanduser()
        return (p if p.is_absolute() else config_path.resolve().parent / p).resolve()
    release = resolve(os.environ.get("RELEASE_AGENT_PROJECT", values.get("release_project", "../release_risk_agent")))
    ops_candidates = [ROOT.parent / "it_ops_agent_fixed_v1_2", Path("D:/Project/agent/it_ops_agent"), ROOT.parent / "it_ops_agent"]
    default_ops = next((p for p in ops_candidates if (p / "run.py").is_file()), ops_candidates[1])
    ops = resolve(os.environ.get("OPS_AGENT_PROJECT", values.get("ops_project", str(default_ops))))
    candidates = [release / ".venv/Scripts/python.exe", release / ".venv/bin/python", Path(sys.executable)]
    default_python = next((p for p in candidates if p.is_file()), Path(sys.executable))
    python = resolve(os.environ.get("WORKBENCH_PYTHON", values.get("python_executable", str(default_python))))
    result = {"ops_project": ops, "release_project": release, "python_executable": python,
              "runtime_dir": (runtime_dir or resolve(values.get("runtime_dir", "runtime"))).resolve()}
    result["storage_mode"] = os.environ.get("WORKBENCH_STORAGE_MODE", values.get("storage_mode", "legacy"))
    result["queue_mode"] = os.environ.get("WORKBENCH_QUEUE_MODE", values.get("queue_mode", "local"))
    result["storage_service_url"] = os.environ.get("STORAGE_SERVICE_URL", values.get("storage_service_url", "http://127.0.0.1:8780"))
    legacy_ops = Path("D:/Project/agent/it_ops_agent")
    if ops != legacy_ops and (legacy_ops / "settings.py").is_file():
        result["ops_settings_file"] = legacy_ops / "settings.py"
        result["ops_history_project"] = legacy_ops
    for name in ("ops_python", "release_python", "release_runtime_dir", "ops_settings_file", "ops_history_project"):
        if name in values:
            result[name] = resolve(values[name])
    return result


def main():
    parser = argparse.ArgumentParser(description="两个Agent的统一网页工作台（本机使用）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--container-bind", action="store_true", help="仅WORKBENCH_CONTAINER=1时绑定容器网卡；宿主端口仍须仅发布127.0.0.1。")
    args = parser.parse_args()
    server = None
    try:
        config = load_config(args.config, args.runtime_dir)
        if not 1 <= args.port <= 65535:
            raise ValueError("端口必须在1至65535之间。")
        existing = running_workbench(args.port)
        url = f"http://127.0.0.1:{args.port}"
        if existing is not None:
            if compatible_workbench(existing, config["runtime_dir"], config.get("storage_mode", "legacy"), config.get("queue_mode", "local")):
                print("工作台已经运行：" + url + "；复用现有服务，不会重复启动或中断任务。", flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            print("启动未完成：该端口仍有旧版或其他工作台。请在旧启动窗口按Ctrl+C关闭，再重新启动；也可用 --port 8766 启动独立端口。", file=sys.stderr)
            return 1
        if args.container_bind and os.environ.get("WORKBENCH_CONTAINER") != "1":
            raise ValueError("容器绑定需显式设置WORKBENCH_CONTAINER=1。")
        server = create_server(config, host="0.0.0.0" if args.container_bind else "127.0.0.1", port=args.port, container_bind=args.container_bind)
        url = f"http://127.0.0.1:{server.server_port}"
        print("统一Agent工作台：" + url, flush=True)
        print("在浏览器操作即可；按 Ctrl+C 关闭工作台。", flush=True)
        if not args.no_browser:
            timer = threading.Timer(0.6, webbrowser.open, args=(url,))
            timer.daemon = True
            timer.start()
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n工作台已关闭。", flush=True)
    except RuntimeError as error:
        print("启动失败：" + str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError, json.JSONDecodeError):
        print("启动失败：端口可能仍被旧服务占用，请先关闭旧启动窗口；同时检查workbench.config.json中的项目/Python路径。", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
