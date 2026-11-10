"""Regression coverage for importing JSON with stale form scope/source values."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import test_http as support
from workbench.materials import preview_materials, prepare_materials


ROOT = Path(__file__).resolve().parents[2]


def ops_document():
    return {"logs": [
        {"id": "line-1", "service": "edge-dns", "environment": "test", "timestamp": "2024-06-28T02:51:00+08:00",
         "synthetic": True, "message": "A route change was observed"},
        {"id": "line-2", "service": "edge-dns", "environment": "test", "timestamp": "2024-06-27T20:08:00Z",
         "synthetic": True, "message": "A second observation"}]}


class MaterialPreviewTests(unittest.TestCase):
    def test_ops_infers_scope_origin_and_chronological_time_across_offsets(self):
        document = ops_document()
        original = deepcopy(document)
        result = preview_materials("ops", document)
        self.assertEqual("edge-dns", result["params"]["service"])
        self.assertEqual("test", result["params"]["environment"])
        self.assertTrue(result["materials"]["synthetic"])
        self.assertEqual("2024-06-28T02:51:00+08:00", result["params"]["start"])
        self.assertEqual("2024-06-27T20:08:00+00:00", result["params"]["end"])
        self.assertEqual(2, result["coverage"]["current_count"])
        self.assertEqual(document, original)

    def test_accepts_native_wrapper_and_task_export_but_rejects_wrong_agent(self):
        raw = {"format": "json", "synthetic": True, "data": ops_document()}
        expected = preview_materials("ops", raw)
        self.assertEqual(expected, preview_materials("ops", {"agent": "ops", "materials": raw}))
        with self.assertRaisesRegex(ValueError, "助手"):
            preview_materials("release", {"agent": "ops", "materials": raw})
        raw["synthetic"] = False
        with self.assertRaisesRegex(ValueError, "来源标记不一致"):
            preview_materials("ops", raw)

    def test_rejects_mixed_or_missing_scope_and_origin_without_relabeling(self):
        for field, value in (("service", "other"), ("environment", "production"), ("synthetic", False),
                             ("synthetic", "true"), ("service", None)):
            with self.subTest(field=field, value=value):
                data = ops_document()
                data["logs"][1][field] = value
                with self.assertRaises(ValueError):
                    preview_materials("ops", data)

    def test_current_release_fixture_recognizes_and_round_trips_without_changes(self):
        fixtures = json.loads((ROOT / "release_risk_agent/data/demo_complete/fixtures.json").read_bytes())
        fixtures["changes"] = {"safe": fixtures["changes"]["safe"]}
        result = preview_materials("release", fixtures)
        self.assertEqual({"change_id": "safe", "service": "order-service", "environment": "staging"}, result["params"])
        self.assertEqual([], result["coverage"]["missing_kinds"])
        files, _, _ = prepare_materials("release", result["materials"], result["params"])
        self.assertEqual(fixtures, files["fixtures.json"])
        with self.assertRaisesRegex(ValueError, "发布评审"):
            preview_materials("ops", fixtures)
        with self.assertRaises(ValueError):
            preview_materials("release", ops_document())

    def test_rejects_multiple_changes_and_invalid_input(self):
        fixtures = json.loads((ROOT / "release_risk_agent/data/demo_complete/fixtures.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "一个变更"):
            preview_materials("release", fixtures)
        for agent, data in (([], {}), ("ops", {}), ("ops", []), ("ops", {"logs": [{}]})):
            with self.assertRaises(ValueError):
                preview_materials(agent, data)


class MaterialPreviewHTTPTests(unittest.TestCase):
    setUp = support.HTTPTests.setUp
    tearDown = support.HTTPTests.tearDown
    request = support.HTTPTests.request
    post = support.HTTPTests.post
    raw_headers = support.HTTPTests.raw_headers

    def test_preview_validates_without_creating_task_and_requires_token(self):
        payload = {"agent": "ops", "document": ops_document()}
        self.assertEqual(403, self.post("/api/material-preview", payload, token=None, headers_only=True)[0])
        self.assertEqual(403, self.post("/api/material-preview", payload, origin="http://evil.invalid", headers_only=True)[0])
        status, _, body = self.post("/api/material-preview", payload)
        self.assertEqual(200, status)
        self.assertEqual("edge-dns", json.loads(body)["params"]["service"])
        self.assertEqual([], self.service.created)
        self.assertEqual(400, self.post("/api/material-preview", {"agent": "ops", "document": {}})[0])
