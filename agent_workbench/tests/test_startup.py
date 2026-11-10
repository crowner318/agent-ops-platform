"""A second launch must reuse the exact compatible app without touching jobs."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

import run_workbench
from workbench.version import service_info


class StartupTests(unittest.TestCase):
    def test_matching_server_is_reused_without_constructing_service(self):
        runtime = Path(__file__).resolve().parent / "not-created-runtime"
        with patch("sys.argv", ["run_workbench.py", "--no-browser"]), patch.object(run_workbench, "load_config", return_value={"runtime_dir": runtime}), patch.object(run_workbench, "running_workbench", return_value=service_info(runtime)), patch.object(run_workbench, "create_server") as create, patch.object(run_workbench.webbrowser, "open") as browser, redirect_stdout(StringIO()):
            self.assertEqual(0, run_workbench.main())
        create.assert_not_called()
        browser.assert_not_called()
        self.assertFalse(runtime.exists())

    def test_legacy_or_other_runtime_does_not_modify_database_or_start_second_server(self):
        runtime = Path(__file__).resolve().parent / "not-created-runtime"
        for info in ({}, {"limits": {}}, dict(service_info(runtime), build_id="old"), service_info(runtime / "other")):
            with self.subTest(info=info), patch("sys.argv", ["run_workbench.py", "--no-browser"]), patch.object(run_workbench, "load_config", return_value={"runtime_dir": runtime}), patch.object(run_workbench, "running_workbench", return_value=info), patch.object(run_workbench, "create_server") as create, redirect_stderr(StringIO()) as error:
                self.assertEqual(1, run_workbench.main())
                self.assertIn("旧版或其他工作台", error.getvalue())
            create.assert_not_called()

    def test_probe_is_loopback_only_bounded_and_does_not_follow_redirects(self):
        with patch.object(run_workbench.http.client, "HTTPConnection") as client:
            response = client.return_value.getresponse.return_value
            response.status = 302
            response.read.return_value = b'{}'
            self.assertEqual({}, run_workbench.running_workbench(8765))
            client.assert_called_once_with("127.0.0.1", 8765, timeout=2)
            response.read.assert_called_once_with(131073)
            client.return_value.request.assert_called_once()
            client.return_value.close.assert_called_once()

    def test_probe_handles_invalid_and_oversized_json(self):
        for raw in (b'[]', b'bad-json', b' ' * 131073):
            with self.subTest(raw_length=len(raw)), patch.object(run_workbench.http.client, "HTTPConnection") as client:
                response = client.return_value.getresponse.return_value
                response.status = 200; response.read.return_value = raw
                self.assertEqual({}, run_workbench.running_workbench(8765))

    def test_public_metadata_only_contains_hashed_path_identity(self):
        value = service_info(Path("private-local-location"))
        self.assertTrue(value["capabilities"]["trash"])
        self.assertNotIn("private-local-location", str(value))
        self.assertEqual(20, len(value["runtime_id"]))

    def test_switching_storage_mode_cannot_reuse_legacy_backend(self):
        runtime = Path("isolated")
        self.assertFalse(run_workbench.compatible_workbench(service_info(runtime), runtime, "java"))
        self.assertTrue(run_workbench.compatible_workbench(service_info(runtime, "java"), runtime, "java"))
