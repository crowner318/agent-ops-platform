"""Offline checks only. These do not claim a Docker build or Redis integration."""
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parent


def module(name):
    spec = spec_from_file_location("platform_" + name, ROOT / (name + ".py"))
    result = module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    def test_compose_only_publishes_workbench_to_loopback(self):
        services = self.compose["services"]
        self.assertEqual({"redis", "storage", "kafka", "kafka-init", "workbench", "worker"}, set(services))
        self.assertEqual({"workbench"}, {name for name, service in services.items() if "ports" in service})
        self.assertEqual(["127.0.0.1:8765:8765"], services["workbench"]["ports"])
        self.assertTrue(self.compose["networks"]["backend"]["internal"])
        self.assertEqual(["backend"], services["storage"]["networks"])

    def test_report_redis_is_persistent_without_eviction(self):
        command = self.compose["services"]["redis"]["command"]
        self.assertEqual("yes", command[command.index("--appendonly") + 1])
        self.assertEqual("everysec", command[command.index("--appendfsync") + 1])
        self.assertEqual("noeviction", command[command.index("--maxmemory-policy") + 1])
        self.assertEqual(["60", "1"], command[command.index("--save") + 1:command.index("--save") + 3])
        self.assertEqual({"storage_data", "redis_data", "worker_runtime", "kafka_data"}, set(self.compose["volumes"]))

    def test_java_and_python_token_contract_and_health_dependencies(self):
        services = self.compose["services"]
        self.assertEqual(services["storage"]["environment"]["STORAGE_SERVICE_TOKEN"],
                         services["workbench"]["environment"]["STORAGE_SERVICE_TOKEN"])
        self.assertIn(":?", services["storage"]["environment"]["STORAGE_SERVICE_TOKEN"])
        self.assertEqual("service_healthy", services["storage"]["depends_on"]["redis"]["condition"])
        self.assertEqual("service_healthy", services["workbench"]["depends_on"]["storage"]["condition"])
        self.assertEqual("java", services["workbench"]["environment"]["WORKBENCH_STORAGE_MODE"])
        self.assertEqual("http://storage:8780", services["workbench"]["environment"]["STORAGE_SERVICE_URL"])

    def test_build_contexts_and_private_material_exclusion(self):
        standard = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for service in ("storage", "worker"):
            build = self.compose["services"][service]["build"]
            self.assertEqual("..", build["context"])
            dockerfile = ROOT.parent / build["dockerfile"]
            self.assertTrue(dockerfile.is_file())
            self.assertEqual(standard, dockerfile.with_name(dockerfile.name + ".dockerignore").read_text(encoding="utf-8"))
        for pattern in ("**/settings.py", "**/runtime/**", "**/.venv/**", "**/.env", "**/workbench.config.json"):
            self.assertIn(pattern, standard)
        java = (ROOT / "Dockerfile.java").read_text()
        self.assertNotIn("COPY .", java)
        python = (ROOT / "Dockerfile.python").read_text()
        self.assertIn("settings.example.py", python)
        self.assertNotIn("ARG MODEL_API_KEY", python)
        self.assertIn("confluent-kafka==2.16.1", python)

    def test_kafka_topic_and_two_workers_match_and_gate_web_startup(self):
        services = self.compose["services"]
        kafka, init, worker, web = (services[name] for name in ("kafka", "kafka-init", "worker", "workbench"))
        self.assertEqual("registry-1.docker.io/apache/kafka:4.1.2", kafka["image"])
        self.assertEqual(kafka["image"], init["image"])
        self.assertEqual("PLAINTEXT://kafka:9092", kafka["environment"]["KAFKA_ADVERTISED_LISTENERS"])
        self.assertEqual("false", kafka["environment"]["KAFKA_AUTO_CREATE_TOPICS_ENABLE"])
        self.assertEqual(["kafka_data:/var/lib/kafka/data"], kafka["volumes"])
        self.assertIn("--partitions 4 --replication-factor 1", init["command"][0])
        self.assertIn("--if-not-exists", init["command"][0])
        self.assertEqual("service_healthy", init["depends_on"]["kafka"]["condition"])
        for service in (worker, web):
            self.assertEqual("service_completed_successfully", service["depends_on"]["kafka-init"]["condition"])
            self.assertEqual("kafka", service["environment"]["WORKBENCH_QUEUE_MODE"])
            self.assertEqual("kafka:9092", service["environment"]["KAFKA_BOOTSTRAP_SERVERS"])
            self.assertEqual(init["environment"]["KAFKA_TOPIC"], service["environment"]["KAFKA_TOPIC"])
        self.assertEqual("service_healthy", web["depends_on"]["worker"]["condition"])
        self.assertEqual(2, worker["deploy"]["replicas"])
        self.assertEqual(worker["image"], web["image"])
        self.assertEqual(worker["volumes"], web["volumes"])

    def test_entrypoint_only_creates_owned_runtime_and_no_secret_config(self):
        launcher = module("entrypoint")
        scratch = ROOT / ".test_tmp"
        scratch.mkdir(exist_ok=True)
        path = scratch / ("agent-platform-test-" + uuid4().hex)
        path.mkdir()
        try:
            self.assertEqual(scratch.resolve(), path.resolve().parent)
            config = launcher.make_config(root=path / "source", runtime=path / "worker")
            self.assertTrue((path / "worker/release").is_dir())
            self.assertFalse((path / "source").exists())
            self.assertNotIn("api_key", json.dumps(config))
            self.assertNotIn("STORAGE_SERVICE_TOKEN", json.dumps(config))
        finally:
            self.assertEqual(scratch.resolve(), path.resolve().parent)
            shutil.rmtree(path)
        with patch.dict("os.environ", {"WORKBENCH_CONTAINER": "0"}), self.assertRaises(SystemExit):
            launcher.main()

    def test_health_requires_real_java_sqlite_redis_and_kafka_flags(self):
        health = module("healthcheck")
        good = {"mode": "java", "reports": "redis", "java": {"available": True},
                "sqlite": {"available": True}, "redis": {"available": True}, "queue": {"mode": "kafka", "available": True}}
        with patch.dict(os.environ, {"WORKBENCH_ROLE": "web"}), patch.object(health, "urlopen", return_value=BytesIO(json.dumps(good).encode())):
            health.main()
        for altered in ({"mode": "legacy"}, {"reports": "files"}, {"redis": {"available": False}}, {"java": {"available": "true"}},
                        {"queue": {"mode": "local", "available": True}}, {"queue": {"mode": "kafka", "available": False}}, {"queue": None}):
            body = json.dumps(dict(good, **altered)).encode()
            with self.subTest(altered=altered), patch.dict(os.environ, {"WORKBENCH_ROLE": "web"}), patch.object(health, "urlopen", return_value=BytesIO(body)), self.assertRaises(SystemExit):
                health.main()

    def test_worker_health_rejects_stale_or_missing_heartbeat(self):
        health = module("healthcheck")
        missing = ROOT / ("worker-heartbeat-test-" + uuid4().hex)
        self.assertFalse(health.worker_healthy(missing))
        timestamp = Path(__file__).stat().st_mtime
        self.assertTrue(health.worker_healthy(__file__, timestamp + 4))
        self.assertFalse(health.worker_healthy(__file__, timestamp + 26))

    def test_setup_env_generates_token_without_model_key_and_never_overwrites(self):
        setup = module("setup_env")
        scratch = ROOT / ".test_tmp"
        scratch.mkdir(exist_ok=True)
        path = scratch / ("setup-env-" + uuid4().hex)
        path.mkdir()
        try:
            self.assertTrue(setup.ensure_env(path))
            content = (path / ".env").read_text(encoding="utf-8")
            token = next(line.split("=", 1)[1] for line in content.splitlines() if line.startswith("STORAGE_SERVICE_TOKEN="))
            self.assertEqual(64, len(token))
            self.assertIn("MODEL_API_KEY=\n", content)
            with patch.object(Path, "open", side_effect=AssertionError("must not open existing configuration")):
                self.assertFalse(setup.ensure_env(path))
            self.assertEqual(content, (path / ".env").read_text(encoding="utf-8"))
        finally:
            self.assertEqual(scratch.resolve(), path.resolve().parent)
            shutil.rmtree(path)

    def test_docker_missing_or_daemon_down_does_not_generate_env(self):
        setup = module("setup_env")
        with patch.object(setup, "find_docker", return_value=None), patch.object(setup, "ensure_env") as create, redirect_stderr(StringIO()):
            self.assertEqual(1, setup.launch())
            create.assert_not_called()
        with patch.object(setup, "find_docker", return_value="docker"), patch.object(setup.subprocess, "run") as run, patch.object(setup, "ensure_env") as create, redirect_stderr(StringIO()):
            run.return_value.returncode = 1
            run.return_value.stderr = "Cannot connect to the Docker daemon"
            self.assertEqual(1, setup.launch())
            create.assert_not_called()

    def test_docker_discovery_uses_path_before_registry(self):
        setup = module("setup_env")
        with patch.dict("os.environ", {"DOCKER_EXE": ""}), patch.object(setup.shutil, "which", return_value="docker-from-path") as which, patch.object(setup, "_windows_docker_roots") as roots:
            self.assertEqual(str(Path("docker-from-path").absolute()), setup.find_docker())
            which.assert_called_once_with("docker")
            roots.assert_not_called()

    def test_docker_discovery_accepts_a_registered_non_default_drive(self):
        setup = module("setup_env")
        install_root = ROOT / "docker desktop custom"
        cli = install_root / "resources" / "bin" / "docker.exe"
        with patch.dict("os.environ", {"DOCKER_EXE": ""}), patch.object(setup.sys, "platform", "win32"), patch.object(setup.shutil, "which", return_value=None), patch.object(setup, "_registered_docker_roots", return_value=iter([install_root])), patch.object(Path, "is_file", autospec=True, side_effect=lambda candidate: candidate == cli):
            self.assertEqual(str(cli), setup.find_docker())
        self.assertNotIn("where docker", (ROOT / "start-docker.cmd").read_text(encoding="utf-8").casefold())

    def test_docker_exe_override_is_explicit_and_validated(self):
        setup = module("setup_env")
        cli = ROOT / "docker.exe"
        with patch.dict("os.environ", {"DOCKER_EXE": f'"{cli}"'}), patch.object(Path, "is_file", return_value=True), patch.object(setup.shutil, "which") as which:
            self.assertEqual(str(cli), setup.find_docker())
            which.assert_not_called()
        with patch.dict("os.environ", {"DOCKER_EXE": "./docker.exe"}), patch.object(setup.subprocess, "run") as run, redirect_stderr(StringIO()) as output:
            self.assertEqual(1, setup.launch())
            self.assertIn("DOCKER_EXE", output.getvalue())
            run.assert_not_called()

    def test_docker_launch_checks_health_and_never_removes_volumes(self):
        setup = module("setup_env")
        executable = str(ROOT / "docker-custom" / "resources" / "bin" / "docker.exe")
        with patch.dict(os.environ, {"PATH": str(ROOT)}), patch.object(setup, "find_docker", return_value=executable), patch.object(setup.subprocess, "run") as run, patch.object(setup, "ensure_env", return_value=False), patch.object(setup, "wait_ready", return_value=True) as wait, redirect_stdout(StringIO()) as output:
            run.return_value.returncode = 0
            run.return_value.stdout = "linux\n"
            self.assertEqual(0, setup.launch())
            self.assertIn("http://127.0.0.1:8765", output.getvalue())
            wait.assert_called_once_with(180)
            self.assertEqual([executable, "compose", "up", "-d", "--build", "--wait", "--wait-timeout", "180"], run.call_args_list[-1].args[0])
            self.assertTrue(all(call.kwargs.get("shell") is False for call in run.call_args_list))
            self.assertFalse(any("down" in call.args[0] for call in run.call_args_list))
            self.assertTrue(all(call.kwargs["env"]["PATH"].split(os.pathsep)[0] == str(Path(executable).parent)
                                for call in run.call_args_list))
            self.assertEqual(str(ROOT), os.environ["PATH"])

    def test_docker_engine_permission_and_windows_mode_have_distinct_messages(self):
        setup = module("setup_env")
        for returncode, stdout, stderr, expected in ((1, "", "Access is denied", "拒绝"), (0, "windows\n", "", "切换到 Linux 容器")):
            with self.subTest(expected=expected), patch.object(setup, "find_docker", return_value="docker"), patch.object(setup.subprocess, "run") as run, patch.object(setup, "ensure_env") as create, redirect_stderr(StringIO()) as output:
                run.return_value.returncode = returncode
                run.return_value.stdout = stdout
                run.return_value.stderr = stderr
                self.assertEqual(1, setup.launch())
                self.assertIn(expected, output.getvalue())
                create.assert_not_called()

    def test_cold_base_image_is_pulled_by_engine_before_build(self):
        setup = module("setup_env")
        calls = []

        def run(argv, **kwargs):
            calls.append(argv[1:])
            missing = argv[1:3] == ["image", "inspect"] and "/maven:" in argv[-1]
            return subprocess.CompletedProcess(argv, 1 if missing else 0, stdout="linux\n", stderr="")

        with patch.object(setup, "find_docker", return_value="docker"), patch.object(setup.subprocess, "run", side_effect=run), patch.object(setup, "ensure_env", return_value=False), patch.object(setup, "wait_ready", return_value=True), redirect_stdout(StringIO()):
            self.assertEqual(0, setup.launch())

        pulls = [args for args in calls if args[0] == "pull"]
        self.assertEqual([["pull", "registry-1.docker.io/library/maven:3.9-eclipse-temurin-21"]], pulls)
        self.assertEqual(["compose", "up", "-d", "--build", "--wait", "--wait-timeout", "180"], calls[-1])

    def test_docker_health_timeout_is_failure_not_success(self):
        setup = module("setup_env")
        with patch.object(setup, "find_docker", return_value="docker"), patch.object(setup.subprocess, "run") as run, patch.object(setup, "ensure_env", return_value=True), patch.object(setup, "wait_ready", return_value=False), redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
            run.return_value.returncode = 0
            run.return_value.stdout = "linux\n"
            self.assertEqual(1, setup.launch())
            self.assertNotIn("启动成功", output.getvalue())

    def test_setup_wait_rejects_legacy_even_when_http_is_available(self):
        setup = module("setup_env")
        ticks = iter((0, 0, 11))
        opener = unittest.mock.Mock()
        opener.open.return_value = BytesIO(json.dumps({"mode": "legacy", "reports": "files"}).encode())
        self.assertFalse(setup.wait_ready(10, clock=lambda: next(ticks), sleep=lambda seconds: None, opener=opener))

    def test_setup_wait_accepts_only_real_storage_health(self):
        setup = module("setup_env")
        ticks = iter((0, 0))
        good = {"mode": "java", "reports": "redis", "java": {"available": True}, "sqlite": {"available": True},
                "redis": {"available": True}, "queue": {"mode": "kafka", "available": True}}
        opener = unittest.mock.Mock()
        opener.open.return_value = BytesIO(json.dumps(good).encode())
        self.assertTrue(setup.wait_ready(10, clock=lambda: next(ticks), sleep=lambda seconds: None, opener=opener))


if __name__ == "__main__":
    unittest.main()
