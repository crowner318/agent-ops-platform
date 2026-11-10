"""Material summaries are generated from evidence, never guessed by a model."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from release_guard.reporting import _change_information, save_report


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "data" / "demo_complete" / "fixtures.json").read_text(encoding="utf-8"))


def state():
    change = deepcopy(FIXTURE["changes"]["safe"])
    return {"change_id": "safe", "scope": change["scope"], "status": "waiting_human",
            "evidence": change["evidence"]["release_snapshot"], "analysis": None,
            "review_record": {}, "quality_issues": [], "policy_findings": []}


class ChangeInformationTests(unittest.TestCase):
    def test_complete_material_has_explicit_demo_owner_and_unexecuted_plans(self):
        text = "\n".join(_change_information(state()))
        self.assertIn("## 本次变更信息", text)
        self.assertIn("演示发布负责人（虚构角色）", text)
        self.assertIn("应用", text)
        self.assertIn("回退方案", text)
        self.assertIn("验证计划", text)
        self.assertIn("计划已提供，尚未执行", text)
        self.assertNotIn("TBD", text)

    def test_absent_owner_is_omitted_without_placeholder_or_fabrication(self):
        payload = state()
        content = payload["evidence"][0]["content"]
        del content["owner"]
        content.pop("rollback_plan")
        content.pop("validation_plan")
        text = "\n".join(_change_information(payload))
        self.assertIn("发布目的", text)
        self.assertNotIn("材料中的负责人", text)
        self.assertNotIn("演示发布负责人", text)
        self.assertNotIn("TBD", text)
        self.assertNotIn("未填写", text)

    def test_empty_or_other_scope_material_does_not_generate_information(self):
        payload = state()
        payload["evidence"][0]["service"] = "other-service"
        self.assertEqual(_change_information(payload), [])
        payload = state()
        payload["evidence"][0]["content"] = {}
        self.assertEqual(_change_information(payload), [])

    def test_new_markdown_adds_material_section_and_json_evidence_is_unchanged(self):
        payload = state()
        root = Path(tempfile.gettempdir()) / ("release-report-test-" + uuid4().hex)
        root.mkdir()
        try:
            markdown, document = save_report("new-report-only", payload, root, {"mode": "demo"})
            text = markdown.read_text(encoding="utf-8")
            self.assertLess(text.index("## 本次变更信息"), text.index("## 规则检查结果"))
            self.assertIn("演示发布负责人（虚构角色）", text)
            saved = json.loads(document.read_text(encoding="utf-8"))
            self.assertEqual(saved["evidence"], payload["evidence"])
            self.assertNotIn("change_information", saved)
        finally:
            reports = root / "reports"
            for child in reports.iterdir():
                child.unlink()
            reports.rmdir()
            root.rmdir()


if __name__ == "__main__":
    unittest.main()
