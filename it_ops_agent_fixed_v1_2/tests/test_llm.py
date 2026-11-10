from __future__ import annotations

import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError

from ops_agent.config import ClientConfig
from ops_agent.llm import DeepSeekClient, LLMError


def response(message=None, reason="stop", usage=None):
    payload = {"choices": [{"finish_reason": reason,
        "message": message or {"role": "assistant", "content": "完成"}}]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


class HTTPBytes(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return HTTPBytes(item)


class ClientTests(unittest.TestCase):
    def config(self, **kwargs):
        return ClientConfig(api_key="test-credential-never-send", **kwargs)

    def test_default_model_endpoint_and_auth(self):
        opener = Opener(response())
        client = DeepSeekClient(self.config(), opener)
        self.assertEqual(client.complete([{"role": "user", "content": "test"}])["content"], "完成")
        req, timeout = opener.requests[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertNotIn("response_format", body)
        self.assertEqual(client.telemetry()[0]["response_format"], "none")
        self.assertTrue(req.get_header("Authorization").startswith("Bearer "))
        self.assertGreater(timeout, 0)

    def test_thinking_roundtrip_preserves_provider_field(self):
        opener = Opener(response({"role": "assistant", "content": "done", "reasoning_content": "private"}))
        client = DeepSeekClient(self.config(thinking=True), opener)
        message = client.complete([{"role": "user", "content": "test"}], tools=[{"type": "function"}])
        self.assertEqual(message["reasoning_content"], "private")
        body = json.loads(opener.requests[0][0].data)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning_effort"], "high")

    def test_qwen_uses_compatible_endpoint_and_enable_thinking(self):
        opener = Opener(response())
        config = self.config(base_url="ws-example.cn-beijing.maas.aliyuncs.com", model="qwen3.8-flash")
        client = DeepSeekClient(config, opener)
        client.complete([{"role": "user", "content": "test"}])
        req = opener.requests[0][0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(body["model"], "qwen3.8-flash")
        self.assertIs(body["enable_thinking"], False)
        self.assertNotIn("thinking", body)
        self.assertNotIn("extra_body", body)
        self.assertNotIn("reasoning_effort", body)

    def test_qwen_thinking_preserves_history_in_separate_field(self):
        opener = Opener(response({"role": "assistant", "content": "done", "reasoning_content": "private"}), response())
        config = self.config(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.8-flash",
                             thinking=True, reasoning_effort="high")
        client = DeepSeekClient(config, opener)
        tools = [{"type": "function", "function": {"name": "read_only_test", "parameters": {"type": "object"}}}]
        history = [{"role": "user", "content": "test"}]
        returned = client.complete(history, tools=tools)
        client.complete(history + [returned, {"role": "user", "content": "next"}], tools=tools)
        body = json.loads(opener.requests[1][0].data)
        self.assertIs(body["enable_thinking"], True)
        self.assertIs(body["preserve_thinking"], True)
        self.assertEqual(body["reasoning_effort"], "xhigh")
        self.assertEqual(body["messages"][1]["reasoning_content"], "private")
        self.assertEqual(body["messages"][1]["content"], "done")
        self.assertNotIn("thinking", body)

    def test_qwen_normal_tool_response_works(self):
        tool_call = {"id": "qwen-call-1", "type": "function", "function": {"name": "query_metrics", "arguments": "{}"}}
        opener = Opener(response({"role": "assistant", "content": None, "tool_calls": [tool_call]}, "tool_calls"))
        cfg = self.config(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.8-flash")
        message = DeepSeekClient(cfg, opener).complete([{"role": "user", "content": "test"}], tools=[{"type": "function"}])
        self.assertEqual(message["tool_calls"][0]["id"], "qwen-call-1")

    def test_qwen_errors_use_provider_name(self):
        error = HTTPError("https://dashscope.aliyuncs.com", 401, "error", {},
                          io.BytesIO(b'{"error":{"message":"invalid key"}}'))
        cfg = self.config(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.8-flash")
        with self.assertRaises(LLMError) as result:
            DeepSeekClient(cfg, Opener(error)).complete([])
        self.assertIn("Qwen", str(result.exception))
        self.assertNotIn("DeepSeek HTTP", str(result.exception))

    def test_429_retries_but_does_not_retry_401_or_leak_key(self):
        error = lambda code: HTTPError("https://api.deepseek.com", code, "error", {},
            io.BytesIO(json.dumps({"error": {"message": "test-credential-never-send"}}).encode()))
        opener = Opener(error(429), response())
        client = DeepSeekClient(self.config(), opener, sleep=lambda _: None)
        client.complete([{"role": "user", "content": "test"}])
        self.assertEqual(len(opener.requests), 2)
        opener = Opener(error(401))
        client = DeepSeekClient(self.config(), opener)
        with self.assertRaises(LLMError) as result:
            client.complete([{"role": "user", "content": "test"}])
        self.assertNotIn("test-credential-never-send", str(result.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_and_truncated_responses_fail(self):
        for raw in (b"invalid-json", response(reason="length"), b'{"choices": []}',
                    response(reason="content_filter"), response(reason="insufficient_system_resource"),
                    response(reason="tool_calls"), response(reason="unknown"),
                    response({"role": "assistant", "content": ""})):
            with self.subTest(raw=raw), self.assertRaises(LLMError):
                DeepSeekClient(self.config(), Opener(raw)).complete([])

    def test_network_errors_are_not_replayed(self):
        opener = Opener(URLError("test"))
        with self.assertRaises(LLMError):
            DeepSeekClient(self.config(), opener).complete([])
        self.assertEqual(len(opener.requests), 1)

    def test_key_and_endpoint_validation(self):
        for config in (ClientConfig(api_key=""), self.config(base_url="http://localhost"),
                       self.config(base_url="https://name:secret@example.test")):
            with self.subTest(config=config.base_url), self.assertRaises(ValueError):
                DeepSeekClient(config, Opener())

    def test_deepseek_usage_is_reported_separately_from_message(self):
        client = DeepSeekClient(self.config(), Opener(response(usage={
            "prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92,
            "prompt_cache_hit_tokens": 32,
        })))
        message = client.complete([{"role": "user", "content": "test"}])
        self.assertEqual(message, {"role": "assistant", "content": "完成"})
        records = client.telemetry()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["usage"], {
            "prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92,
            "cached_prompt_tokens": 32,
        })
        self.assertTrue(records[0]["usage_reported"])
        self.assertTrue(records[0]["success"])
        self.assertEqual(records[0]["http_status"], 200)
        self.assertEqual(records[0]["attempts"], 1)
        self.assertEqual(records[0]["model"], "deepseek-v4-pro")
        self.assertEqual(records[0]["provider"], "deepseek")
        self.assertGreaterEqual(records[0]["latency_ms"], 0)

    def test_qwen_cached_usage_and_missing_fields_remain_unknown(self):
        config = self.config(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                             model="qwen3.8-flash")
        client = DeepSeekClient(config, Opener(response(usage={
            "prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
            "prompt_tokens_details": {"cached_tokens": 64},
        }), response(), response(usage={"prompt_tokens": 0}), response(usage={
            "prompt_tokens": True, "completion_tokens": -1, "total_tokens": "100",
            "prompt_tokens_details": {"cached_tokens": 3.5},
        })))
        for _ in range(4):
            client.complete([])
        records = client.telemetry()
        self.assertEqual(records[0]["provider"], "qwen")
        self.assertEqual(records[0]["usage"]["cached_prompt_tokens"], 64)
        unknown = dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens"))
        self.assertEqual(records[1]["usage"], unknown)
        self.assertFalse(records[1]["usage_reported"])
        self.assertEqual(records[2]["usage"]["prompt_tokens"], 0)
        self.assertTrue(records[2]["usage_reported"])
        self.assertIsNone(records[2]["usage"]["total_tokens"])
        self.assertIsNone(records[2]["usage"]["cached_prompt_tokens"])
        self.assertEqual(records[3]["usage"], unknown)
        self.assertFalse(records[3]["usage_reported"])

    def test_retry_attempts_and_failures_each_have_one_logical_record(self):
        def http_error(code):
            return HTTPError("https://api.deepseek.com", code, "error", {},
                             io.BytesIO(b'{"error":{"message":"private error details"}}'))
        opener = Opener(http_error(429), response(), http_error(503), http_error(503), http_error(503))
        client = DeepSeekClient(self.config(), opener, sleep=lambda _: None)
        client.complete([])
        with self.assertRaises(LLMError):
            client.complete([])
        records = client.telemetry()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["attempts"], 2)
        self.assertTrue(records[0]["success"])
        self.assertEqual(records[0]["http_status"], 200)
        self.assertEqual(records[1]["attempts"], 3)
        self.assertFalse(records[1]["success"])
        self.assertEqual(records[1]["http_status"], 503)
        self.assertFalse(records[1]["usage_reported"])
        self.assertNotIn("private error details", json.dumps(records))

    def test_network_budget_and_invalid_response_failures_are_recorded(self):
        client = DeepSeekClient(self.config(), Opener(URLError("private connection details"),
            response(reason="length", usage={"completion_tokens": 4096}), b"invalid-json"))
        for _ in range(3):
            with self.assertRaises(LLMError):
                client.complete([])
        with self.assertRaises(LLMError):
            client.complete([], timeout=0)
        records = client.telemetry()
        self.assertEqual([record["attempts"] for record in records], [1, 1, 1, 0])
        self.assertTrue(all(not record["success"] for record in records))
        self.assertIsNone(records[0]["http_status"])
        self.assertIsNone(records[3]["http_status"])
        self.assertEqual(records[1]["usage"]["completion_tokens"], 4096)
        self.assertNotIn("private connection details", json.dumps(records))

    def test_telemetry_has_no_request_secret_or_reasoning_and_snapshot_is_deep(self):
        client = DeepSeekClient(self.config(thinking=True), Opener(response({
            "role": "assistant", "content": "sensitive body", "reasoning_content": "private-reasoning-text",
        }, usage={"prompt_tokens": 5})))
        client.complete([{"role": "user", "content": "private-request-text"}])
        records = client.telemetry()
        serialized = json.dumps(records)
        for private in ("test-credential-never-send", "private-reasoning-text", "private-request-text",
                        "sensitive body", "Authorization", "headers", "messages", "reasoning_content"):
            self.assertNotIn(private, serialized)
        self.assertEqual(set(records[0]), {"model", "provider", "latency_ms", "attempts", "success",
                                           "http_status", "usage", "usage_reported", "response_format"})
        records[0]["usage"]["prompt_tokens"] = 999
        records[0]["success"] = False
        records.append({"injected": True})
        fresh = client.telemetry()
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["usage"]["prompt_tokens"], 5)
        self.assertTrue(fresh[0]["success"])

    def test_parallel_completion_usage_does_not_cross_threads(self):
        class ConcurrentOpener:
            def open(self, req, timeout):
                body = json.loads(req.data)
                token_count = int(body["messages"][0]["content"])
                return HTTPBytes(response(usage={"prompt_tokens": token_count}))
        client = DeepSeekClient(self.config(), ConcurrentOpener())
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda value: client.complete([{"role": "user", "content": str(value)}]), range(16)))
        records = client.telemetry()
        self.assertEqual(len(records), 16)
        self.assertEqual(sorted(record["usage"]["prompt_tokens"] for record in records), list(range(16)))
        self.assertTrue(all(record["attempts"] == 1 and record["success"] for record in records))

    def test_json_object_mode_is_top_level_and_tools_none_omits_tool_choice(self):
        configurations = [self.config(), self.config(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.8-flash")]
        for config in configurations:
            with self.subTest(provider=config.resolved_provider):
                opener = Opener(response({"role": "assistant", "content": '{"status":"review"}'}))
                client = DeepSeekClient(config, opener)
                self.assertTrue(client.supports_json_mode())
                returned = client.complete([{"role": "user", "content": "Return a JSON object."}],
                    tools=None, tool_choice="none", response_format={"type": "json_object"})
                self.assertEqual(json.loads(returned["content"]), {"status": "review"})
                body = json.loads(opener.requests[0][0].data)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertNotIn("tools", body)
                self.assertNotIn("tool_choice", body)
                self.assertEqual(client.telemetry()[0]["response_format"], "json_object")
                self.assertEqual(client.telemetry()[0]["attempts"], 1)
                self.assertTrue(client.telemetry()[0]["success"])

    def test_json_mode_invalid_formats_are_rejected_before_http_and_recorded(self):
        opener = Opener()
        client = DeepSeekClient(self.config(), opener)
        invalid = [{}, [], "json_object", {"type": "json_schema"},
                   {"type": "json_object", "schema": {"secret": "must-not-enter-telemetry"}},
                   {"type": None}]
        for response_format in invalid:
            with self.subTest(response_format=response_format), self.assertRaises(ValueError):
                client.complete([], response_format=response_format)
        self.assertEqual(opener.requests, [])
        records = client.telemetry()
        self.assertEqual(len(records), len(invalid))
        self.assertTrue(all(record["attempts"] == 0 and not record["success"] for record in records))
        self.assertTrue(all(record["response_format"] == "none" for record in records))
        self.assertNotIn("must-not-enter-telemetry", json.dumps(records))

    def test_json_mode_thinking_rejected_without_changing_config(self):
        configurations = [self.config(thinking=True), self.config(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.8-flash", thinking=True)]
        for config in configurations:
            with self.subTest(provider=config.resolved_provider):
                opener = Opener()
                client = DeepSeekClient(config, opener)
                self.assertFalse(client.supports_json_mode())
                with self.assertRaisesRegex(ValueError, "思考模式"):
                    client.complete([], response_format={"type": "json_object"})
                self.assertIs(config.thinking, True)
                self.assertEqual(opener.requests, [])
                record = client.telemetry()[0]
                self.assertEqual(record["response_format"], "json_object")
                self.assertEqual(record["attempts"], 0)
                self.assertFalse(record["success"])
                self.assertIsNone(record["http_status"])


if __name__ == "__main__":
    unittest.main()
