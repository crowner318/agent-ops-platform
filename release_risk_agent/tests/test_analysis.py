from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from release_guard.analysis import (PydanticAnalysis, _usage, build_analysis_chain,
                                    review_analysis, validate_analysis)
from release_guard.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "data" / "fixtures.json").read_text(encoding="utf-8"))


def material(change="safe"):
    case = FIXTURE["changes"][change]
    return [deepcopy(row) for rows in case["evidence"].values() for row in rows], deepcopy(case["scope"])


def body(evidence):
    current = next(row for row in evidence if row["kind"] == "config_diff")
    return {"summary": "报告只提供待评审说明，不批准发布。", "risks": [{"title": "配置仍需独立验证",
        "detail": "请结合测试与容量检查验证配置。", "evidence_ids": [current["id"]],
        "verification": ["复核运行时配置与测试记录。"]}], "missing_evidence": [], "process_checks": []}


def historical_only_body(evidence):
    candidate = body(evidence)
    history = next(row for row in evidence if row["kind"] == "runbook")
    extra = deepcopy(candidate["risks"][0])
    extra.update(title="历史资料提示的检查方向", detail="REJECTED_PARSED_TEXT_NOT_FOR_FEEDBACK",
                 evidence_ids=[history["id"]])
    candidate["risks"].append(extra)
    return candidate


def structured_response(candidate, input_tokens=20, output_tokens=5, cached_tokens=3):
    raw = AIMessage(content="RAW_PROVIDER_TEXT_NOT_FOR_FEEDBACK",
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "input_token_details": {"cache_read": cached_tokens}},
        additional_kwargs={"reasoning_content": "PRIVATE_REASONING_NOT_FOR_PERSISTENCE"})
    return {"raw": raw, "parsed": PydanticAnalysis.model_validate(candidate), "parsing_error": None}


class LocalHTTPContext:
    """Do not construct real HTTP transports in control-flow regression tests."""
    def __init__(self, *args, **kwargs):
        self.follow_redirects = kwargs.get("follow_redirects", False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class SharedTimeoutContext:
    """Expose whether both logical calls execute inside one budget context."""
    def __init__(self):
        self.active = False
        self.entries = 0
        self.exits = 0

    async def __aenter__(self):
        self.entries += 1
        self.active = True
        return self

    async def __aexit__(self, *args):
        self.exits += 1
        self.active = False
        return False


class RecordingAnalysisChain:
    def __init__(self, responses, budget):
        self.responses = list(responses)
        self.requests = []
        self.budget = budget

    async def ainvoke(self, chain_input, config=None):
        self.requests.append({"input": deepcopy(chain_input), "config": deepcopy(config),
                              "inside_shared_budget": self.budget.active})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class AnalysisValidationTests(unittest.TestCase):
    def test_valid_bounded_same_scope_analysis(self):
        evidence, scope = material()
        result = validate_analysis(PydanticAnalysis.model_validate(body(evidence)), evidence, scope)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(body(evidence), result["analysis"])

    def test_unknown_reference_in_array_or_prose_is_rejected(self):
        evidence, scope = material()
        for field in ("array", "prose"):
            candidate = body(evidence)
            if field == "array":
                candidate["risks"][0]["evidence_ids"] = ["fabricated"]
            else:
                candidate["risks"][0]["detail"] += " [fabricated]"
            self.assertFalse(validate_analysis(candidate, evidence, scope)["ok"])

    def test_scope_and_historical_only_risk_are_rejected(self):
        evidence, scope = material()
        bad_scope = dict(scope, change_id="different-change")
        self.assertFalse(validate_analysis(body(evidence), evidence, bad_scope)["ok"])
        candidate = body(evidence)
        historical = next(row for row in evidence if row["kind"] == "runbook")
        candidate["risks"][0]["evidence_ids"] = [historical["id"]]
        self.assertFalse(validate_analysis(candidate, evidence, scope)["ok"])
        history = [row for row in evidence if row["kind"] in {"runbook", "incident_case"}]
        self.assertFalse(validate_analysis(candidate, history, scope)["ok"])

    def test_extra_decision_fields_and_output_limits_are_rejected(self):
        evidence, scope = material()
        for candidate in (dict(body(evidence), decision="approved"), dict(body(evidence), summary="x" * 501)):
            self.assertFalse(validate_analysis(candidate, evidence, scope)["ok"])
        candidate = body(evidence)
        candidate["risks"] *= 6
        self.assertFalse(validate_analysis(candidate, evidence, scope)["ok"])

    def test_local_negation_allows_disclaimers_but_not_later_approval(self):
        evidence, scope = material()
        allowed = ["本程序不批准发布。", "本程序不能批准发布。", "确认阅读不代表批准发布。", "本记录不构成发布批准。", "无凭据显示 [REDACTED]"]
        for text in allowed:
            with self.subTest(text=text):
                candidate = dict(body(evidence), summary=text)
                self.assertTrue(validate_analysis(candidate, evidence, scope)["ok"])
        for text in ("批准发布。", "不能确认风险，但批准发布。", "不代表批准发布。允许上线。", "本次发布已批准。"):
            with self.subTest(text=text):
                self.assertFalse(validate_analysis(dict(body(evidence), summary=text), evidence, scope)["ok"])

    def test_usage_uses_reported_values_and_never_private_reasoning(self):
        raw = AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
            "input_token_details": {"cache_read": 4}}, additional_kwargs={"reasoning_content": "private"})
        usage = _usage(raw)
        self.assertEqual({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cached_prompt_tokens": 4}, usage)
        self.assertNotIn("private", json.dumps(usage))
        self.assertTrue(all(value is None for value in _usage(None).values()))


class AnalysisChainTests(unittest.IsolatedAsyncioTestCase):
    async def scripted_api(self, responses, *, evidence=None, scope=None, policy=None):
        fixture_evidence, fixture_scope = material()
        evidence = fixture_evidence if evidence is None else evidence
        scope = fixture_scope if scope is None else scope
        policy = [] if policy is None else policy
        budget = SharedTimeoutContext()
        chain = RecordingAnalysisChain(responses, budget)
        settings = Settings(api_key="unit-test-placeholder", timeout=5)
        with patch("release_guard.analysis.build_analysis_chain", return_value=chain) as factory, \
                patch("release_guard.analysis.httpx.Client", LocalHTTPContext), \
                patch("release_guard.analysis.httpx.AsyncClient", LocalHTTPContext), \
                patch("release_guard.analysis.asyncio.timeout", return_value=budget) as timeout:
            result = await review_analysis(settings, evidence, scope, policy, demo=False)
            factory.assert_called_once()
            timeout.assert_called_once_with(settings.timeout)
        self.assertEqual(1, budget.entries)
        self.assertEqual(1, budget.exits)
        self.assertTrue(all(request["inside_shared_budget"] for request in chain.requests))
        self.assertFalse(budget.active)
        return result, chain

    async def test_demo_is_real_runnable_with_same_pydantic_validation_no_api(self):
        evidence, scope = material()
        with patch("release_guard.analysis.ChatOpenAI", side_effect=AssertionError("demo must not construct API client")):
            result = await review_analysis(Settings(), evidence, scope, [], demo=True)
        self.assertEqual([], result["issues"])
        self.assertEqual("scripted_demo", result["source"])
        self.assertTrue(result["model_call"]["scripted"])
        self.assertEqual(0, result["model_call"]["attempts"])
        self.assertTrue(all(v is None for v in result["model_call"]["usage"].values()))

    async def test_api_chain_json_mode_no_retry_redirects_and_actual_metadata(self):
        evidence, scope = material()
        raw = AIMessage(content="{}", usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
                        additional_kwargs={"reasoning_content": "must-not-persist"})
        parsed = PydanticAnalysis.model_validate(body(evidence))
        with patch("release_guard.analysis.ChatOpenAI") as factory:
            factory.return_value.with_structured_output.return_value = RunnableLambda(
                lambda _: {"raw": raw, "parsed": parsed, "parsing_error": None})
            result = await review_analysis(Settings(api_key="unit-test-placeholder"), evidence, scope, [], demo=False)
            kwargs = factory.call_args.kwargs
            self.assertEqual(0, kwargs["max_retries"])
            self.assertFalse(kwargs["http_client"].follow_redirects)
            self.assertFalse(kwargs["http_async_client"].follow_redirects)
            self.assertEqual({"enable_thinking": False}, kwargs["extra_body"])
            factory.return_value.with_structured_output.assert_called_once_with(PydanticAnalysis, method="json_mode", include_raw=True)
        self.assertEqual([], result["issues"])
        self.assertEqual("model_api", result["source"])
        self.assertEqual(25, result["model_call"]["usage"]["total_tokens"])
        self.assertNotIn("must-not-persist", json.dumps(result))
        self.assertNotIn("unit-test-placeholder", json.dumps(result))

    async def test_model_error_fails_once_without_echoing_exception_credentials(self):
        evidence, scope = material()
        async def fail(_):
            raise RuntimeError("Authorization Bearer unit-test-placeholder")
        with patch("release_guard.analysis.build_analysis_chain", return_value=RunnableLambda(fail)) as factory:
            result = await review_analysis(Settings(api_key="unit-test-placeholder"), evidence, scope, [], demo=False)
        self.assertEqual(1, factory.call_count)
        self.assertEqual("model_review_failed", result["issues"][0]["code"])
        self.assertNotIn("unit-test-placeholder", json.dumps(result))
        self.assertIsNone(result["analysis"])

    async def test_missing_current_evidence_skips_chain(self):
        _, scope = material()
        with patch("release_guard.analysis.build_analysis_chain") as factory:
            result = await review_analysis(Settings(), [], scope, [], demo=True)
        factory.assert_not_called()
        self.assertEqual(0, result["model_call"]["attempts"])
        self.assertTrue(result["issues"])

    async def test_history_only_correction_can_remove_risk_and_keep_process_check(self):
        evidence, scope = material()
        rejected = historical_only_body(evidence)
        original_rejected = deepcopy(rejected)
        corrected = {"summary": "当前资料未支持新增风险，保留一般核查方向；本报告不批准发布。",
                     "risks": [], "missing_evidence": [],
                     "process_checks": ["根据手册核查峰值负载与连接持有时长，不把历史事故视为本次事实。"]}
        policy = [{"code": "TEST_POLICY_WARNING", "severity": "warning", "source": "policy",
                   "title": "保留程序检查", "detail": "模型不得删除。", "evidence_ids": ["safe-config"]}]
        saved_policy, saved_evidence, saved_scope = deepcopy(policy), deepcopy(evidence), deepcopy(scope)
        result, chain = await self.scripted_api([
            structured_response(rejected, 20, 5, 3), structured_response(corrected, 30, 7, 10),
            structured_response(body(evidence))], evidence=evidence, scope=scope, policy=policy)
        self.assertEqual([], result["issues"])
        self.assertEqual(corrected, result["analysis"])
        self.assertEqual([], result["analysis"]["risks"])
        self.assertEqual(2, len(chain.requests))
        self.assertEqual(1, len(chain.responses), "a third response must never be consumed")
        contexts = [json.loads(request["input"]["review_context"]) for request in chain.requests]
        for key in ("scope", "evidence", "policy_findings", "evidence_groups"):
            self.assertEqual(contexts[0][key], contexts[1][key], key)
        self.assertEqual(scope, contexts[1]["scope"])
        self.assertEqual(evidence, contexts[1]["evidence"])
        self.assertEqual(policy, contexts[1]["policy_findings"])
        current = {row["id"] for row in evidence if row["kind"] in
                   {"release_snapshot", "config_diff", "code_diff", "ci_checks", "dependency_budget"}}
        self.assertEqual(current, set(contexts[0]["evidence_groups"]["current_ids"]))
        self.assertNotIn("output_feedback", contexts[0])
        feedback = contexts[1]["output_feedback"]
        self.assertEqual("historical_only_risk", feedback["issues"][0]["code"])
        self.assertEqual("analysis.risks[1]", feedback["issues"][0]["path"])
        self.assertIn("risks=[]", feedback["instruction"])
        self.assertIn("不得随意附加无关当前ID", feedback["instruction"])
        serialized_feedback = json.dumps(contexts[1], ensure_ascii=False)
        for marker in ("RAW_PROVIDER_TEXT_NOT_FOR_FEEDBACK", "REJECTED_PARSED_TEXT_NOT_FOR_FEEDBACK",
                       "PRIVATE_REASONING_NOT_FOR_PERSISTENCE", "unit-test-placeholder"):
            self.assertNotIn(marker, serialized_feedback)
            self.assertNotIn(marker, json.dumps(result, ensure_ascii=False))
        self.assertEqual(original_rejected, rejected, "the program must not attach current IDs to rejected output")
        self.assertEqual(saved_policy, policy)
        self.assertEqual(saved_evidence, evidence)
        self.assertEqual(saved_scope, scope)
        metric = result["model_call"]
        self.assertEqual(2, metric["attempts"])
        self.assertTrue(metric["success"])
        self.assertEqual({"prompt_tokens": 50, "completion_tokens": 12, "total_tokens": 62,
                          "cached_prompt_tokens": 13}, metric["usage"])
        self.assertEqual(2, len(metric["attempt_details"]))
        self.assertEqual(["initial", "historical_only_risk"], [x["reason"] for x in metric["attempt_details"]])
        self.assertEqual([False, True], [x["success"] for x in metric["attempt_details"]])
        self.assertEqual("historical_only_risk", metric["attempt_details"][0]["issues"][0]["code"])
        self.assertEqual([], metric["attempt_details"][1]["issues"])
        self.assertEqual(25, metric["attempt_details"][0]["usage"]["total_tokens"])
        self.assertEqual(37, metric["attempt_details"][1]["usage"]["total_tokens"])

    async def test_valid_initial_response_uses_one_call_with_no_feedback(self):
        evidence, scope = material()
        result, chain = await self.scripted_api([structured_response(body(evidence)),
                                                structured_response(body(evidence))])
        self.assertTrue(result["model_call"]["success"])
        self.assertEqual(1, result["model_call"]["attempts"])
        self.assertEqual(1, len(chain.requests))
        self.assertEqual(1, len(chain.responses))
        self.assertNotIn("output_feedback", json.loads(chain.requests[0]["input"]["review_context"]))
        self.assertEqual("initial", result["model_call"]["attempt_details"][0]["reason"])

    async def test_correction_cannot_repeat_forever_or_accept_new_fake_ids_or_authority(self):
        evidence, _ = material()
        historical = historical_only_body(evidence)
        second_unknown = body(evidence)
        second_unknown["risks"][0]["evidence_ids"] = ["new-fake-correction-id"]
        second_authority = dict(body(evidence), summary="批准发布。")
        cases = [(historical, "historical_only_risk"), (second_unknown, "unknown_evidence_id"),
                 (second_authority, "model_authority_overreach")]
        for second, expected_code in cases:
            with self.subTest(second_issue=expected_code):
                result, chain = await self.scripted_api([structured_response(historical),
                    structured_response(second), structured_response(body(evidence))])
                self.assertIsNone(result["analysis"])
                self.assertFalse(result["model_call"]["success"])
                self.assertEqual(2, result["model_call"]["attempts"])
                self.assertEqual(2, len(chain.requests))
                self.assertEqual(1, len(chain.responses))
                self.assertTrue(any(issue["code"] == expected_code for issue in result["issues"]))
                self.assertEqual("historical_only_risk", result["model_call"]["attempt_details"][0]["issues"][0]["code"])
                self.assertTrue(any(issue["code"] == expected_code for issue in result["model_call"]["attempt_details"][1]["issues"]))

    async def test_initial_unknown_reference_or_authority_does_not_get_content_correction(self):
        evidence, _ = material()
        unknown = body(evidence)
        unknown["risks"][0]["evidence_ids"] = ["fake-initial-id"]
        authority = dict(body(evidence), summary="允许上线。")
        mixed = historical_only_body(evidence)
        mixed["summary"] = "批准发布。"
        for candidate, expected_code in ((unknown, "unknown_evidence_id"), (authority, "model_authority_overreach"),
                                         (mixed, "model_authority_overreach")):
            with self.subTest(initial_issue=expected_code, mixed=len(candidate["risks"]) > 1):
                result, chain = await self.scripted_api([structured_response(candidate), structured_response(body(evidence))])
                self.assertIsNone(result["analysis"])
                self.assertFalse(result["model_call"]["success"])
                self.assertEqual(1, result["model_call"]["attempts"])
                self.assertEqual(1, len(chain.requests))
                self.assertEqual(1, len(chain.responses))
                self.assertTrue(any(issue["code"] == expected_code for issue in result["issues"]))

    async def test_transport_error_is_not_retried_and_usage_stays_unknown(self):
        evidence, _ = material()
        result, chain = await self.scripted_api([RuntimeError("Bearer unit-test-placeholder"),
                                                structured_response(body(evidence))])
        self.assertIsNone(result["analysis"])
        self.assertEqual(1, len(chain.requests))
        self.assertEqual(1, result["model_call"]["attempts"])
        self.assertEqual("model_review_failed", result["issues"][0]["code"])
        self.assertTrue(all(value is None for value in result["model_call"]["usage"].values()))
        self.assertEqual(1, len(result["model_call"]["attempt_details"]))
        self.assertNotIn("unit-test-placeholder", json.dumps(result))

    async def test_second_call_timeout_keeps_first_issue_and_does_not_undercount_unknown_usage(self):
        evidence, _ = material()
        result, chain = await self.scripted_api([structured_response(historical_only_body(evidence), 30, 8, 4),
                                                TimeoutError("private provider request details"),
                                                structured_response(body(evidence))])
        self.assertIsNone(result["analysis"])
        self.assertFalse(result["model_call"]["success"])
        self.assertEqual(2, len(chain.requests))
        self.assertEqual(1, len(chain.responses))
        self.assertEqual(2, result["model_call"]["attempts"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("historical_only_risk", codes)
        self.assertIn("model_review_failed", codes)
        details = result["model_call"]["attempt_details"]
        self.assertEqual(2, len(details))
        self.assertEqual(38, details[0]["usage"]["total_tokens"])
        self.assertEqual("historical_only_risk", details[0]["issues"][0]["code"])
        self.assertEqual("historical_only_risk", details[1]["reason"])
        self.assertEqual("model_review_failed", details[1]["issues"][0]["code"])
        self.assertTrue(all(value is None for value in details[1]["usage"].values()))
        self.assertTrue(all(value is None for value in result["model_call"]["usage"].values()),
                        "the known first call cannot stand in for an unknown, possibly billed second call")
        self.assertNotIn("private provider request details", json.dumps(result))

    async def test_missing_second_usage_field_is_not_silently_zero_filled(self):
        evidence, _ = material()
        second = structured_response(body(evidence), 10, 2, 1)
        second["raw"] = AIMessage(content="{}", usage_metadata={"input_tokens": 10,
            "output_tokens": 2, "total_tokens": 12})
        result, _ = await self.scripted_api([structured_response(historical_only_body(evidence), 20, 5, 3), second])
        self.assertTrue(result["model_call"]["success"])
        self.assertEqual(37, result["model_call"]["usage"]["total_tokens"])
        self.assertIsNone(result["model_call"]["usage"]["cached_prompt_tokens"])
        self.assertEqual(3, result["model_call"]["attempt_details"][0]["usage"]["cached_prompt_tokens"])
        self.assertIsNone(result["model_call"]["attempt_details"][1]["usage"]["cached_prompt_tokens"])

    async def test_correction_preserves_blocking_policy_without_changing_caller_objects(self):
        from release_guard.policy import evaluate
        evidence, scope = material("risk-pool")
        findings = evaluate(evidence)
        original_findings = deepcopy(findings)
        self.assertTrue(any(f["severity"] == "blocker" for f in findings))
        corrected = {"summary": "没有新增模型风险，不改变程序规则；本报告不批准发布。", "risks": [],
                     "missing_evidence": [], "process_checks": ["请处理程序规则中的容量问题并提交独立验证。"]}
        result, chain = await self.scripted_api([structured_response(historical_only_body(evidence)),
            structured_response(corrected)], evidence=evidence, scope=scope, policy=findings)
        self.assertEqual([], result["issues"])
        self.assertEqual(original_findings, findings)
        for request in chain.requests:
            context = json.loads(request["input"]["review_context"])
            self.assertEqual(original_findings, context["policy_findings"])
            self.assertEqual(scope, context["scope"])
        self.assertNotIn("decision", result["analysis"])


if __name__ == "__main__":
    unittest.main()
