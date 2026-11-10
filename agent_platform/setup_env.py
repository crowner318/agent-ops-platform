"""Create a new local token once, or launch the Docker stack without shell code."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent


def _registered_docker_roots():
    """Docker Desktop registers the actual path for both install modes."""
    import winreg

    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as opened:
                    location, _ = winreg.QueryValueEx(opened, "InstallLocation")
            except OSError:
                continue
            if isinstance(location, str) and location.strip():
                yield Path(os.path.expandvars(location.strip().strip('"')))


def _windows_docker_roots():
    yield from _registered_docker_roots()
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
    if program_files:
        yield Path(program_files) / "Docker" / "Docker"
    app_data = os.environ.get("LOCALAPPDATA")
    if app_data:
        yield Path(app_data) / "Programs" / "DockerDesktop"


def find_docker():
    override = os.environ.get("DOCKER_EXE", "").strip().strip('"')
    if override:
        executable = Path(os.path.expandvars(override))
        if (not executable.is_absolute() or not executable.is_file()
                or (sys.platform == "win32" and executable.suffix.lower() != ".exe")):
            raise ValueError("DOCKER_EXE 必须指向 Docker 可执行文件的绝对路径。请更正该变量，或清除它以启用自动查找。")
        return str(executable)

    on_path = shutil.which("docker")
    if on_path:
        return str(Path(on_path).absolute())
    if sys.platform == "win32":
        for root in _windows_docker_roots():
            executable = root / "resources" / "bin" / "docker.exe"
            if executable.is_absolute() and executable.is_file():
                return str(executable)
    return None


def ensure_env(directory=ROOT):
    """Never open or overwrite an existing .env, including existing model keys."""
    path = Path(directory) / ".env"
    if path.exists():
        return False
    text = ("# Local configuration. Keep this file private.\n"
            + "STORAGE_SERVICE_TOKEN=" + secrets.token_hex(32) + "\n"
            + "MODEL_API_KEY=\n"
            + "MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            + "MODEL_NAME=qwen3.8-flash\nMODEL_PROVIDER=qwen\n")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError:
        return False
    return True


def healthy_status(value):
    if not isinstance(value, dict):
        return False
    queue = value.get("queue", {})
    return (value.get("mode") == "java" and value.get("reports") == "redis"
            and isinstance(queue, dict) and queue.get("mode") == "kafka" and queue.get("available") is True
            and all(isinstance(value.get(kind), dict) and value[kind].get("available") is True
                    for kind in ("java", "sqlite", "redis")))


def wait_ready(timeout, *, clock=time.monotonic, sleep=time.sleep, opener=None):
    opener = opener or build_opener(ProxyHandler({}))
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            with opener.open("http://127.0.0.1:8765/api/storage-status", timeout=5) as response:
                raw = response.read(65537)
            if len(raw) <= 65536 and healthy_status(json.loads(raw)):
                return True
        except (OSError, HTTPError, URLError, ValueError):
            pass
        sleep(2)
    return False


def prepare_base_images(executable, directory, docker_env):
    # Docker Engine and BuildKit may use different routes to authenticate to
    # the registry. Populate the local image store before building cold images.
    for name in ("Dockerfile.java", "Dockerfile.python"):
        for line in (Path(directory) / name).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts or parts[0] != "FROM":
                continue
            image = parts[1]
            probe = subprocess.run([executable, "image", "inspect", image], cwd=directory, env=docker_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
            if probe.returncode == 0:
                continue
            print(f"正在下载基础镜像 {image}…", flush=True)
            pull = subprocess.run([executable, "pull", image], cwd=directory, env=docker_env, shell=False)
            if pull.returncode != 0:
                return False
    return True


def launch(directory=ROOT, *, timeout=180):
    try:
        executable = find_docker()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if executable is None:
        print("未找到 Docker。请检查 Docker Desktop 是否已安装，或设置 DOCKER_EXE 指向 docker.exe，再重试。", file=sys.stderr)
        return 1
    docker_env = os.environ.copy()
    # Docker resolves docker-credential-desktop and other helpers by name.
    # Add its bin directory to Docker subprocesses without changing the host PATH.
    cli_directory = str(Path(executable).absolute().parent)
    docker_env["PATH"] = os.pathsep.join(filter(None, (cli_directory, docker_env.get("PATH"))))
    try:
        engine = subprocess.run([executable, "info", "--format", "{{.OSType}}"], cwd=directory, env=docker_env,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=15, shell=False)
        if engine.returncode != 0:
            diagnostic = engine.stderr.casefold()
            if any(phrase in diagnostic for phrase in ("access is denied", "access denied", "permission denied", "拒绝访问")):
                print("Docker 引擎拒绝了当前终端的连接。请从平时运行 Docker Desktop 的账户或终端重新启动。", file=sys.stderr)
            else:
                print("无法连接 Docker 引擎。请确认 Docker Desktop 已启动并显示引擎正在运行，再重试。", file=sys.stderr)
            return 1
        if engine.stdout.strip().casefold() == "windows":
            print("当前 Docker 使用 Windows 容器。请在 Docker Desktop 中切换到 Linux 容器，再启动工作台。", file=sys.stderr)
            return 1
        created = ensure_env(directory)
        print("已生成本机存储令牌，模型密钥保持空白。" if created else "沿用已有.env，不读取或改写其中的模型密钥。", flush=True)
        checked = subprocess.run([executable, "compose", "config", "--quiet"], cwd=directory, env=docker_env, shell=False)
        if checked.returncode != 0:
            print("Compose配置未通过，请检查.env和目录结构；未删除任何数据卷。", file=sys.stderr)
            return 1
        if not prepare_base_images(executable, directory, docker_env):
            print("基础镜像下载失败，请查看上方 Docker 输出。", file=sys.stderr)
            return 1
        started = subprocess.run([executable, "compose", "up", "-d", "--build", "--wait", "--wait-timeout", str(timeout)],
                                 cwd=directory, env=docker_env, shell=False)
        if started.returncode != 0:
            print("Docker构建或启动未完成，请查看上方错误。已有数据卷保持；本机8765端口也可能仍被旧服务占用。", file=sys.stderr)
            return 1
    except (OSError, subprocess.TimeoutExpired):
        print("无法完成Docker命令，请检查Docker Desktop状态和当前账户访问权限。", file=sys.stderr)
        return 1
    print("正在核对工作台的 Kafka 与 Java 存储状态…", flush=True)
    if not wait_ready(timeout):
        print("服务尚未全部健康，未宣称启动成功。请运行 docker compose ps 和 docker compose logs --tail=80。", file=sys.stderr)
        return 1
    print("启动成功：http://127.0.0.1:8765\n再次运行会复用数据卷，不会清空案例或报告。", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成新的私有.env，或构建并启动Docker工作台；不读取已有模型密钥。")
    parser.add_argument("--launch", action="store_true", help="检查Docker后启动并等待依赖健康")
    parser.add_argument("--timeout", type=int, default=180, help="启动后的健康等待秒数（10至600）")
    args = parser.parse_args(argv)
    if not 10 <= args.timeout <= 600:
        parser.error("timeout应在10至600秒之间。")
    if args.launch:
        return launch(timeout=args.timeout)
    created = ensure_env()
    print("已生成.env：随机存储令牌已保存，MODEL_API_KEY为空。" if created else ".env已存在，保持原样。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
