"""Endpoint normalization checks using fake settings and no network access."""

import importlib.util
import sys
import types
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


# Load an isolated copy so neither this test nor config's import reads the user's
# real settings.py. Other tests retain their own normal config module unchanged.
_MODULE_NAME = "_ops_agent_config_endpoint_tests"
_SPEC = importlib.util.spec_from_file_location(
    _MODULE_NAME, Path(__file__).resolve().parents[1] / "ops_agent" / "config.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_CONFIG = importlib.util.module_from_spec(_SPEC)
with patch.dict(sys.modules, {"settings": types.ModuleType("settings"), _MODULE_NAME: _CONFIG}):
    _SPEC.loader.exec_module(_CONFIG)

ClientConfig = _CONFIG.ClientConfig
normalize_base_url = _CONFIG.normalize_base_url
FAKE_KEY = "test-only-fake-api-key"
WORKSPACE_HOST = "test-workspace.cn-beijing.maas.aliyuncs.com"
WORKSPACE_ROOT = f"https://{WORKSPACE_HOST}/compatible-mode/v1"


class ConfigEndpointTests(unittest.TestCase):
    def test_bare_official_workspace_gets_https_and_compatible_path(self):
        for host in (WORKSPACE_HOST, "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"):
            with self.subTest(host=host):
                self.assertEqual(normalize_base_url(host), f"https://{host}/compatible-mode/v1")

    def test_https_workspace_host_gets_compatible_path(self):
        self.assertEqual(normalize_base_url(f"https://{WORKSPACE_HOST}"), WORKSPACE_ROOT)
        self.assertEqual(normalize_base_url(f"https://{WORKSPACE_HOST}/"), WORKSPACE_ROOT)

    def test_complete_chat_endpoint_is_stripped_once(self):
        for endpoint in (WORKSPACE_ROOT + "/chat/completions", WORKSPACE_ROOT + "/chat/completions/"):
            with self.subTest(endpoint=endpoint):
                normalized = normalize_base_url(endpoint)
                self.assertEqual(normalized, WORKSPACE_ROOT)
                self.assertEqual(normalized + "/chat/completions", WORKSPACE_ROOT + "/chat/completions")

    def test_existing_version_path_is_not_duplicated(self):
        for endpoint in (WORKSPACE_ROOT, WORKSPACE_ROOT + "/", f"https://{WORKSPACE_HOST}/v1"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(normalize_base_url(endpoint), endpoint.rstrip("/"))
                self.assertEqual(normalize_base_url(normalize_base_url(endpoint)), endpoint.rstrip("/"))

    def test_outer_whitespace_is_removed(self):
        self.assertEqual(normalize_base_url(f" \t{WORKSPACE_HOST}\r\n"), WORKSPACE_ROOT)
        config = ClientConfig(api_key=FAKE_KEY, base_url=f"  {WORKSPACE_ROOT}  ")
        self.assertEqual(config.base_url, WORKSPACE_ROOT)
        config.validate()

    def test_deepseek_root_is_preserved(self):
        for endpoint in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(normalize_base_url(endpoint), endpoint)
                config = ClientConfig(api_key=FAKE_KEY, base_url=endpoint)
                config.validate()
                self.assertEqual(config.resolved_provider, "deepseek")

    def test_http_credentials_query_and_fragment_remain_rejected(self):
        invalid_endpoints = (
            f"http://{WORKSPACE_HOST}/compatible-mode/v1",
            f"https://alice:fake-password@{WORKSPACE_HOST}/compatible-mode/v1",
            f"https://alice@{WORKSPACE_HOST}/compatible-mode/v1",
            WORKSPACE_ROOT + "?api_key=fake-query-key",
            WORKSPACE_ROOT + "#fragment",
        )
        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint):
                config = ClientConfig(api_key=FAKE_KEY, base_url=endpoint)
                with self.assertRaises(ValueError):
                    config.validate()
        self.assertEqual(
            normalize_base_url(f"http://{WORKSPACE_HOST}"), f"http://{WORKSPACE_HOST}"
        )

    def test_auto_provider_uses_aliyun_endpoint_before_model_name(self):
        for endpoint in (WORKSPACE_HOST, WORKSPACE_ROOT, "https://dashscope.aliyuncs.com/compatible-mode/v1"):
            with self.subTest(endpoint=endpoint):
                config = ClientConfig(api_key=FAKE_KEY, base_url=endpoint, model="deepseek-v4-pro", provider="auto")
                config.validate()
                self.assertEqual(config.resolved_provider, "qwen")

    def test_unknown_bare_host_is_not_given_https(self):
        for endpoint in ("api.example.com", "api.example.com/v1", "test-only-fake-api-key"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(normalize_base_url(endpoint), endpoint)
                with self.assertRaises(ValueError):
                    ClientConfig(api_key=FAKE_KEY, base_url=endpoint).validate()

    def test_operator_settings_path_is_lazy_explicit_and_has_no_bytecode_copy(self):
        root = Path(__file__).resolve().parents[1] / ("config-test-" + uuid4().hex)
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root))
        path = root / "external_config.py"
        path.write_text("DEEPSEEK_API_KEY='unit-only-key'\nDEEPSEEK_BASE_URL='https://api.deepseek.com'\n"
                        "DEEPSEEK_MODEL='test-model'\nTHINKING_ENABLED=False\nREASONING_EFFORT='high'\n"
                        "REQUEST_TIMEOUT_SECONDS=20\nMAX_OUTPUT_TOKENS=1024\n", encoding="utf-8")
        with patch.dict(os.environ, {"OPS_SETTINGS_FILE": str(path.resolve())}):
            settings = _CONFIG._load_settings()
            self.assertEqual("test-model", settings.DEEPSEEK_MODEL)
            self.assertFalse((root / "__pycache__").exists())

    def test_operator_settings_failure_never_repeats_exception_credentials(self):
        root = Path(__file__).resolve().parents[1] / ("config-test-" + uuid4().hex)
        root.mkdir(); self.addCleanup(lambda: shutil.rmtree(root))
        path = root / "broken_config.py"
        path.write_text("raise ValueError('fake-sensitive-test-value')\n", encoding="utf-8")
        for value in (str(path.resolve()), "relative_config.py", str(root / "missing.py"), str(root)):
            with patch.dict(os.environ, {"OPS_SETTINGS_FILE": value}):
                with self.assertRaises(ValueError) as error:
                    _CONFIG._load_settings()
                self.assertNotIn("fake-sensitive-test-value", str(error.exception))

    def test_no_override_keeps_default_module_and_environment_overrides(self):
        fake = types.SimpleNamespace(DEEPSEEK_API_KEY="fake-key", DEEPSEEK_BASE_URL="https://api.deepseek.com",
            DEEPSEEK_MODEL="fake-model", THINKING_ENABLED=False, REASONING_EFFORT="high",
            REQUEST_TIMEOUT_SECONDS=15, MAX_OUTPUT_TOKENS=512, API_PROVIDER="auto")
        with patch.dict(os.environ, {"OPS_SETTINGS_FILE": "", "DEEPSEEK_MODEL": "env-model"}), patch.object(_CONFIG.importlib, "import_module", return_value=fake) as load:
            config = _CONFIG.load_client_config()
            self.assertEqual("env-model", config.model)
            load.assert_called_once_with("settings")


if __name__ == "__main__":
    unittest.main()
