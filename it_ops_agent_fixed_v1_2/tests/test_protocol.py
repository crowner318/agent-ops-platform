from __future__ import annotations

import json
from pathlib import Path
import unittest

from ops_agent.protocol import parse_tool_plan, strict_json_document

ROOT = Path(__file__).resolve().parents[1]


class DocumentTests(unittest.TestCase):
    def test_exact_json_and_single_fence(self):
        obj = {"a": [{"text": "braces {inside} are data"}], "b": 1}
        self.assertEqual((obj, []), strict_json_document(json.dumps(obj)))
        parsed, normalized = strict_json_document("```json\n" + json.dumps(obj) + "\n```")
        self.assertEqual(obj, parsed)
        self.assertEqual("json_fence_removed", normalized[0]["code"])
        self.assertEqual([obj], strict_json_document(json.dumps([obj]))[0])

    def test_plain_affixes_allow_one_object_and_record_only_counts(self):
        parsed, norms = strict_json_document('以下为分析结果：\n{"a":1}\n内容仅供评审。')
        self.assertEqual({"a": 1}, parsed)
        self.assertEqual("plain_prose_removed", norms[0]["code"])
        self.assertGreater(norms[0]["prefix_chars"], 0)
        self.assertGreater(norms[0]["suffix_chars"], 0)
        self.assertNotIn("以下", json.dumps(norms, ensure_ascii=False))

    def test_ambiguous_malformed_duplicate_nonfinite_and_code_are_rejected(self):
        bad = ['{} {}', '说明 {} 备注 {}', '说明 [{"a":1}]', '{bad} {"a":1}',
               '{"hypotheses":[],"recommendations":[],"missing_evidence":[],"process_checks":[]',
               '{"a":1,"a":2}', '说明 {"a":1,"a":2}', '{"a":{"x":1,"x":2}}',
               '{"a":NaN}', '说明 {"a":Infinity}', '{}\ntrue', 'true\n{}', '{}\n42',
               '{} true extra', '[{"a":1}]\n说明', '说明 [broken] {"a":1}',
               '__import__("os").system("calc")\n{"a":1}', 'print({"a":1})', '({"a":1})',
               '```python\n{"a":1}\n```', '```json\n```json\n{}\n```\n```',
               '说明 ```json\n{}\n```', '```json\n{}\n```\n说明']
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json_document(raw)


class ToolPlanTests(unittest.TestCase):
    def test_actual_single_response_is_plan(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "qwen_real_responses.json").read_text(encoding="utf-8"))
        result = parse_tool_plan(fixture["responses"]["single_diagnosis"]["content"])
        self.assertEqual("call_1", result[0]["id"])
        self.assertEqual("collect_evidence", result[0]["name"])
        self.assertEqual(["runbooks"], result[0]["arguments"]["kinds"])
        self.assertNotIn("args", result[0])

    def test_only_complete_top_level_arrays_are_plans(self):
        raw = json.dumps([{"id": "a", "name": "unknown_harness_will_reject", "args": {"x": [1, 2]}}])
        expected = [{"id": "a", "name": "unknown_harness_will_reject", "arguments": {"x": [1, 2]}}]
        self.assertEqual(expected, parse_tool_plan(raw))
        self.assertEqual(expected, parse_tool_plan("```json\n" + raw + "\n```"))
        for value in ('{"hypotheses":[]}', 'run rollback now', '说明：\n' + raw,
                      'exec("restart")', 'null', '{"plan":' + raw + '}'):
            self.assertIsNone(parse_tool_plan(value))

    def test_bad_shape_types_ids_duplicate_keys_and_budget_are_rejected(self):
        valid = {"id": "a", "name": "query_metrics", "args": {}}
        cases = [[dict(valid, extra=True)], [dict(valid, id=True)], [dict(valid, id="")],
                 [dict(valid, name="x" * 129)], [dict(valid, args=[])], [valid, valid],
                 ["query_metrics"], [[valid]], [dict(valid, arguments={})]]
        raw_cases = [json.dumps(value) for value in cases]
        raw_cases += ['[{"id":"a","name":"x","args":{"x":NaN}}]',
                      '[{"id":"a","id":"b","name":"x","args":{}}]',
                      json.dumps([valid]) + '\n说明', json.dumps([valid]) + '{}']
        for raw in raw_cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_tool_plan(raw)
        with self.assertRaises(ValueError):
            parse_tool_plan(json.dumps([valid]), max_calls=0)
        with self.assertRaises(ValueError):
            parse_tool_plan(json.dumps([valid, dict(valid, id="b")]), max_calls=1)
        with self.assertRaises(ValueError):
            parse_tool_plan("[]", max_calls=0)

    def test_empty_plan_is_rejected_without_an_execution_round(self):
        for raw in ("[]", "```json\n[]\n```"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_tool_plan(raw)


if __name__ == "__main__":
    unittest.main()
