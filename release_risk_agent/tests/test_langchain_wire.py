"""Actual LangChain + OpenAI-compatible client, using an in-memory HTTP transport."""
import json
from pathlib import Path
import unittest

import httpx
from langsmith import tracing_context

from release_guard.analysis import build_analysis_chain, PydanticAnalysis, validate_analysis
from release_guard.config import Settings
from release_guard.evidence import EvidenceStore


class LangChainWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_model_adapter_builds_qwen_compatible_request_and_parses_output(self):
        store = EvidenceStore(Path(__file__).resolve().parents[1] / "data")
        result = store.execute("get_config_diff", {"change_id": "safe"})
        expected = {"summary": "依据当前配置复核，本报告不批准发布。", "risks": [{
            "title": "核对运行参数", "detail": "目前仅有待验证的配置资料。",
            "evidence_ids": [result["evidence"][0]["id"]], "verification": ["核对实际生效配置"]}],
            "missing_evidence": [], "process_checks": ["是否完成人工复核"]}
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual("/compatible-mode/v1/chat/completions", request.url.path)
            self.assertEqual("qwen3.8-flash", body["model"])
            self.assertEqual({"type": "json_object"}, body["response_format"])
            self.assertFalse(body["enable_thinking"])
            self.assertNotIn("http_socket_options", body)
            return httpx.Response(200, json={"id": "chatcmpl-unit", "object": "chat.completion", "created": 0,
                "model": "qwen3.8-flash", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(expected, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 15, "completion_tokens": 7, "total_tokens": 22}})

        transport = httpx.MockTransport(respond)
        with tracing_context(enabled=False):
            async with httpx.AsyncClient(transport=transport, follow_redirects=False) as async_http:
                with httpx.Client(transport=transport, follow_redirects=False) as sync_http:
                    with self.assertNoLogs("langchain_openai.chat_models.base", level="WARNING"):
                        chain = build_analysis_chain(Settings(api_key="unit-placeholder"), http_client=sync_http, http_async_client=async_http)
                    answer = await chain.ainvoke({"schema": json.dumps(PydanticAnalysis.model_json_schema()),
                        "review_context": json.dumps({"scope": result["scope"], "evidence": result["evidence"], "policy_findings": []})},
                        config={"callbacks": []})
        self.assertEqual(1, len(requests))
        self.assertIsNone(answer["parsing_error"])
        self.assertEqual(expected, answer["parsed"].model_dump())
        self.assertEqual(22, answer["raw"].usage_metadata["total_tokens"])
        self.assertTrue(validate_analysis(answer["parsed"], result["evidence"], result["scope"])["ok"])


if __name__ == "__main__":
    unittest.main()
