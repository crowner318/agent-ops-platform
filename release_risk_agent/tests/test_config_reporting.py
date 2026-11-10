import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from uuid import uuid4
import shutil

from release_guard.config import Settings, load_settings
from release_guard.reporting import sanitize, save_report


@contextmanager
def temporary_directory():
    parent = Path(tempfile.gettempdir()).resolve()
    root = parent / ("release-report-test-" + uuid4().hex)
    root.mkdir()
    try:
        yield str(root)
    finally:
        checked = root.resolve()
        if checked.parent != parent or not checked.name.startswith("release-report-test-"):
            raise ValueError("Unsafe test cleanup target")
        shutil.rmtree(checked)


class ConfigReportingTests(unittest.TestCase):
    def test_demo_needs_no_secret_and_api_requires_one(self):
        Settings().validate(demo=True)
        with self.assertRaises(ValueError):
            Settings().validate()

    def test_invalid_endpoints_and_thinking_fail_locally(self):
        for base in ("http://example.com", "https://user:password@example.com", "https://example.com?a=1", "https://example.com/#fragment"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                Settings(base_url=base).validate(demo=True)
        with self.assertRaises(ValueError):
            Settings(thinking=True).validate(demo=True)

    def test_key_not_part_of_dataclass_representation(self):
        self.assertNotIn("private-test-value", repr(Settings(api_key="private-test-value")))

    def test_environment_overrides_empty_template(self):
        with temporary_directory() as directory, patch("release_guard.config.PROJECT_ROOT", Path(directory)), patch.dict(
            "os.environ", {"RELEASE_API_KEY": "private-test-value", "RELEASE_MODEL": "qwen3.8-flash",
                           "RELEASE_BASE_URL": "https://example.com/v1/chat/completions"}):
            settings = load_settings()
            self.assertEqual(settings.base_url, "https://example.com/v1")
            self.assertEqual(settings.api_key, "private-test-value")

    def test_published_report_keeps_policy_and_human_boundary(self):
        state = {"change_id": "safe", "status": "waiting_human", "scope": {"service": "order-service", "environment": "demo"},
            "policy_findings": [{"code": "REVIEW_NEEDED", "severity": "warning", "title": "材料待核查", "detail": "需要复核", "evidence_ids": ["e1"]}],
            "analysis": {"summary": "仅供复核", "risks": [{"title": "候选", "detail": "待验证", "evidence_ids": ["e1"], "verification": ["核对生效配置"]}],
                         "process_checks": ["是否具有测试记录"]},
            "api_key": "private-test-value", "reasoning_content": "private-reasoning", "evidence": []}
        with temporary_directory() as directory:
            md, machine = save_report("rev-test", state, Path(directory), {"mode": "demo", "coordination": "local"})
            body = md.read_text(encoding="utf-8")
            payload = json.loads(machine.read_text(encoding="utf-8"))
            self.assertIn("REVIEW_NEEDED", body)
            self.assertIn("核对生效配置", body)
            self.assertIn("不批准发布", body)
            self.assertIn("是否具有测试记录", body)
            self.assertNotIn("private-test-value", machine.read_text(encoding="utf-8"))
            self.assertNotIn("reasoning_content", payload)

    def test_redis_password_and_common_secrets_removed(self):
        cleaned = sanitize({"redis_url": "redis://alice:secret@localhost:6379/0",
                            "text": "redis://alice:secret@localhost:6379/0", "authorization": "Bearer private-test-value"})
        self.assertNotIn("secret@", str(cleaned))
        self.assertNotIn("private-test-value", str(cleaned))


if __name__ == "__main__":
    unittest.main()
