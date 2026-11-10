import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from ops_agent.memory import MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        # Normal mkdir also works on Windows sandbox hosts whose restricted
        # directory ACLs conflict with TemporaryDirectory's mode=0o700.
        self.root = Path(tempfile.gettempdir()) / ("ops-agent-memory-test-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup_directory)
        self.memory = MemoryStore(self.root / "memory.db")

    def cleanup_directory(self):
        # This fixture creates files only; avoid a recursive delete entirely.
        for child in self.root.iterdir():
            child.unlink()
        self.root.rmdir()

    def trajectory(self, **updates):
        payload = {
            "model": "deepseek-v4-pro", "complete": True, "source": "deepseek_api",
            "messages": [{"role": "user", "content": "订单连接池耗尽，请排查。"},
                         {"role": "assistant", "content": "检查连接泄漏与连接池配置。"}],
        }
        payload.update(updates)
        return payload

    def test_recent_preserves_order_and_isolates_session_service_environment(self):
        for index in range(5):
            self.memory.add_event("s1", "orders", "prod", "tool", {"index": index})
        self.memory.add_event("s2", "orders", "prod", "tool", {"index": 99})
        self.memory.add_event("s1", "orders", "stage", "tool", {"index": 98})
        self.memory.add_event("s1", "billing", "prod", "tool", {"index": 97})
        events = self.memory.recent("s1", "orders", "prod", limit=2)
        self.assertEqual([event["payload"]["index"] for event in events], [3, 4])
        self.assertEqual(self.memory.recent("s1", "orders", "prod", limit=0), [])

    def test_draft_cannot_be_retrieved_or_exported_even_with_model_verified_flag(self):
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory(verified=True))
        case = self.memory.save_case("s1", "orders", "prod", "订单数据库连接池耗尽")
        self.assertEqual(self.memory.search_cases("orders", "prod", "连接池"), [])
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 0)
        self.assertFalse(self.memory.list_cases("orders", "prod")[0]["verified"])
        self.assertTrue(self.memory.confirm_case(case))
        self.assertFalse(self.memory.confirm_case("does-not-exist"))
        self.assertEqual(self.memory.search_cases("orders", "prod", "连接池")[0]["id"], case)
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 1)

    def test_recent_dialogue_filters_tools_before_limiting_and_preserves_scope(self):
        self.memory.add_event("s1", "orders", "prod", "question", {"text": "先前连接池故障是什么？"})
        self.memory.add_event("s1", "orders", "prod", "answer", {"text": "连接池耗尽，等待复核。"})
        for index in range(30):
            self.memory.add_event("s1", "orders", "prod", "tool", {"index": index})
        self.memory.add_event("s2", "orders", "prod", "question", {"text": "另一会话"})
        self.memory.add_event("s1", "orders", "stage", "question", {"text": "另一环境"})
        self.memory.add_event("s1", "billing", "prod", "question", {"text": "另一服务"})
        events = self.memory.recent_dialogue("s1", "orders", "prod", limit=2)
        self.assertEqual([event["kind"] for event in events], ["question", "answer"])
        self.assertEqual(events[0]["payload"]["text"], "先前连接池故障是什么？")
        self.assertEqual(self.memory.recent_dialogue("s1", "orders", "prod", limit=0), [])

    def test_confirmed_case_export_is_scoped_and_excludes_demo_incomplete_invalid(self):
        self.memory.add_event("s1", "orders", "stage", "trajectory", self.trajectory())
        self.memory.add_event("s2", "orders", "prod", "trajectory", self.trajectory())
        self.memory.add_event("s1", "billing", "prod", "trajectory", self.trajectory())
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory(source="scripted_demo"))
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory(complete=False))
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory(messages=[
            {"role": "tool", "tool_call_id": "missing", "content": "invalid"},
            {"role": "assistant", "content": "unsupported conclusion"},
        ]))
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory(verified=False))
        case = self.memory.save_case("s1", "orders", "prod", "连接池恢复")
        self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 1)
        self.assertEqual(self.memory.search_cases("orders", "stage", "连接池"), [])

    def test_confirmation_does_not_approve_future_events_in_same_session(self):
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory())
        first = self.memory.save_case("s1", "orders", "prod", "首轮故障已审核")
        self.memory.confirm_case(first)
        self.memory.add_event("s1", "orders", "prod", "trajectory", self.trajectory())
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 1)
        second = self.memory.save_case("s1", "orders", "prod", "第二轮故障待审核")
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 1)
        self.memory.confirm_case(second)
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 2)

    def test_practices_require_human_confirmed_case_of_same_scope(self):
        case = self.memory.save_case("s1", "orders", "prod", "连接池耗尽")
        with self.assertRaises(ValueError):
            self.memory.add_practice("orders", "prod", "检查连接泄漏", case)
        self.memory.confirm_case(case)
        with self.assertRaises(ValueError):
            self.memory.add_practice("orders", "stage", "检查连接泄漏", case)
        with self.assertRaises(ValueError):
            self.memory.add_practice("billing", "prod", "检查连接泄漏", case)
        practice = self.memory.add_practice("orders", "prod", "连接池耗尽时检查连接泄漏", case)
        self.assertEqual(self.memory.search_practices("orders", "prod", "连接池")[0]["id"], practice)
        self.assertEqual(self.memory.search_practices("orders", "stage", "连接池"), [])
        with self.assertRaises(ValueError):
            self.memory.save_case("s2", "orders", "prod", "模型自称确认", verified=True)

    def test_recursively_redacts_secrets_and_drops_reasoning_before_persistence(self):
        messages = self.trajectory()["messages"]
        messages[-1]["reasoning_content"] = "private chain of thought"
        payload = self.trajectory(messages=messages)
        payload["nested"] = {"api_key": "my-private-test-token", "reasoning_content": "hidden"}
        self.memory.add_event("s1", "orders", "prod", "trajectory", payload)
        stored = self.memory.recent("s1", "orders", "prod")[0]["payload"]
        serialized = json.dumps(stored)
        self.assertNotIn("my-private-test-token", serialized)
        self.assertNotIn("reasoning_content", serialized)
        self.assertNotIn("private chain of thought", serialized)
        case = self.memory.save_case("s1", "orders", "prod", '{"api_key":"summary-secret", "issue":"pool"}')
        self.memory.confirm_case(case)
        summary = self.memory.list_cases("orders", "prod")[0]["summary"]
        self.assertNotIn("summary-secret", summary)
        self.assertEqual(json.loads(summary)["issue"], "pool")
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 1)
        self.assertNotIn("reasoning_content", (self.root / "training.jsonl").read_text(encoding="utf-8"))

    def test_export_retains_valid_redacted_tool_arguments_and_closed_calls(self):
        messages = [
            {"role": "user", "content": "检查连接池"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call1", "type": "function", "function": {
                    "name": "search_logs", "arguments": '{"api_key":"private-tool-secret", "query":"pool"}'
                }},
            ]},
            {"role": "tool", "tool_call_id": "call1", "content": '{"token":"private-token", "result":"found"}'},
            {"role": "assistant", "content": "发现连接池耗尽。"},
        ]
        tools = [{"type": "function", "function": {
            "name": "search_logs", "description": "查询故障日志", "parameters": {
                "type": "object", "properties": {"query": {"type": "string"}},
                "required": ["query"], "additionalProperties": False,
            },
        }}]
        self.memory.add_event("s1", "orders", "prod", "trajectory",
                              self.trajectory(messages=messages, tools=tools))
        case = self.memory.save_case("s1", "orders", "prod", "连接池诊断已确认")
        self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "training.jsonl"), 1)
        text = (self.root / "training.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private-tool-secret", text)
        self.assertNotIn("private-token", text)
        sample = json.loads(text)
        self.assertEqual(sample["tools"], tools)
        args = json.loads(sample["messages"][1]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["query"], "pool")
        self.assertEqual(json.loads(sample["messages"][2]["content"])["result"], "found")

    def test_json_safety_and_parallel_workers(self):
        def write(index):
            self.memory.add_event("parallel", "orders", "prod", "tool", {"index": index})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(20)))
        self.assertEqual(len(self.memory.recent("parallel", "orders", "prod", 50)), 20)
        self.memory.add_event("s1", "orders", "prod", "tool", {"non_finite": float("nan")})
        self.assertIsNone(self.memory.recent("s1", "orders", "prod")[0]["payload"]["non_finite"])
        with self.assertRaises(TypeError):
            self.memory.add_event("s1", "orders", "prod", "tool", {"object": object()})
        with self.assertRaises(ValueError):
            self.memory.export_sft(self.root / "memory.db")

    def test_normalized_final_exports_clean_bounded_json_without_rewriting_source(self):
        raw_object = {"hypotheses": [], "recommendations": [],
                      "missing_evidence": [f"待补充证据 {index}" for index in range(15)], "process_checks": []}
        raw = "以下是分析结果：\n" + json.dumps(raw_object, ensure_ascii=False)
        normalized_object = dict(raw_object, missing_evidence=raw_object["missing_evidence"][:10])
        normalized = json.dumps(normalized_object, ensure_ascii=False)
        messages = [{"role": "user", "content": "检查连接池"}, {"role": "assistant", "content": raw}]
        self.memory.add_event("normalized", "orders", "prod", "trajectory",
                              self.trajectory(messages=messages, normalized_final=normalized))
        case = self.memory.save_case("normalized", "orders", "prod", "人工审核规范输出")
        self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "normalized.jsonl"), 1)
        sample = json.loads((self.root / "normalized.jsonl").read_text(encoding="utf-8"))
        exported = json.loads(sample["messages"][-1]["content"])
        self.assertEqual(exported, normalized_object)
        self.assertEqual(len(exported["missing_evidence"]), 10)
        self.assertNotIn("以下是分析结果", sample["messages"][-1]["content"])
        persisted = self.memory.recent("normalized", "orders", "prod")[0]["payload"]
        self.assertEqual(persisted["messages"][-1]["content"], raw)
        self.assertEqual(len(json.loads(persisted["normalized_final"])["missing_evidence"]), 10)

    def test_normalized_final_does_not_bypass_review_complete_or_demo_exclusions(self):
        normalized = json.dumps({"hypotheses": [], "recommendations": [], "missing_evidence": [], "process_checks": []})
        for session, updates, confirm in [
            ("unreviewed", {}, False),
            ("failed", {"complete": False}, True),
            ("demo", {"source": "scripted_demo"}, True),
        ]:
            self.memory.add_event(session, "orders", "prod", "trajectory",
                                  self.trajectory(normalized_final=normalized, **updates))
            case = self.memory.save_case(session, "orders", "prod", "待核验")
            if confirm:
                self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "excluded.jsonl"), 0)

    def test_invalid_normalized_final_is_not_replaced_with_raw_training_target(self):
        invalid = [None, "not json", '{"only":"extra"}',
                   json.dumps({"hypotheses": [], "recommendations": [],
                               "missing_evidence": ["gap"] * 11, "process_checks": []})]
        for index, normalized in enumerate(invalid):
            session = f"invalid-{index}"
            self.memory.add_event(session, "orders", "prod", "trajectory",
                                  self.trajectory(normalized_final=normalized))
            case = self.memory.save_case(session, "orders", "prod", "人工确认不能绕过格式验证")
            self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "invalid.jsonl"), 0)

    def test_format_and_quality_repaired_trajectories_require_manual_curation(self):
        normalized = json.dumps({"hypotheses": [], "recommendations": [], "missing_evidence": [], "process_checks": []})
        for code in ("format_repair_requested", "quality_repair_requested"):
            messages = [{"role": "user", "content": "排查异常"},
                        {"role": "assistant", "content": "已被校验拒绝的原始回答"},
                        {"role": "user", "content": "请修正上一条回答"},
                        {"role": "assistant", "content": normalized}]
            self.memory.add_event(code, "orders", "prod", "trajectory", self.trajectory(
                messages=messages, normalized_final=normalized,
                protocol_events=[{"code": code, "step": 1}]))
            case = self.memory.save_case(code, "orders", "prod", "修复后最终报告经人工确认")
            self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "curation.jsonl"), 0)
        for code in ("format_repair_requested", "quality_repair_requested"):
            stored = self.memory.recent(code, "orders", "prod")[0]["payload"]
            self.assertTrue(stored["complete"])
            self.assertEqual(stored["messages"][1]["content"], "已被校验拒绝的原始回答")

        self.memory.add_event("normalized-without-repair", "orders", "prod", "trajectory", self.trajectory(
            normalized_final=normalized, protocol_events=[{"code": "plain_prose_removed", "path": "final"}]))
        case = self.memory.save_case("normalized-without-repair", "orders", "prod", "仅规范化，无拒绝或修复")
        self.memory.confirm_case(case)
        self.assertEqual(self.memory.export_sft(self.root / "curation.jsonl"), 1)
        sample = json.loads((self.root / "curation.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(sample["messages"][-1]["content"]), json.loads(normalized))


if __name__ == "__main__":
    unittest.main()
