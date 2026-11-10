from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from ops_agent.reporting import FINAL_SCHEMA, prepare_final_answer
from ops_agent.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"service": "order-service", "environment": "demo",
         "start_time": "2026-10-19T09:55:00+08:00", "end_time": "2026-10-19T10:20:00+08:00"}


def payload():
    return {"hypotheses": [{"text": "连接池上限缩小可能与获取连接超时有关。",
            "evidence_ids": ["c-order-001", "l-order-003", "m-order-007"],
            "validation": ["核对运行实例实际配置，测量连接持有时长并在批准的验证环境复现。"]}],
            "recommendations": [{"text": "提交变更评审，讨论回退配置及验证方案。", "evidence_ids": ["c-order-001"]}],
            "missing_evidence": ["实例数量、请求并发和连接持有时长。"],
            "process_checks": ["容量评估、审批和告警覆盖是否有相应记录。"]}


class ReportingTests(unittest.TestCase):
    def setUp(self):
        reg = SkillRegistry(ROOT / "data", "order-service", "demo", SCOPE["start_time"], SCOPE["end_time"])
        for name in ("query_metrics", "search_logs", "analyze_traces", "get_changes"):
            reg.execute(name, {})
        self.evidence = reg.observed_evidence()

    def render(self, obj, evidence=None, scope=None):
        raw = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        return prepare_final_answer(raw, self.evidence if evidence is None else evidence,
                                    SCOPE if scope is None else scope, "diagnosis")

    def test_valid_json_preserves_candidates_and_generates_facts(self):
        result = self.render(payload())
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual([], result["issues"])
        self.assertEqual(1, len(result["analysis"]["hypotheses"]))
        self.assertIn("request_error_rate", result["answer"])
        self.assertIn("0.183", result["answer"])
        self.assertIn('单位="ratio"', result["answer"])
        self.assertIn("ms", result["answer"])
        self.assertIn("[c-order-001]", result["answer"])
        self.assertIn("未批准、未执行", result["answer"])
        self.assertFalse(result["analysis"]["root_cause_confirmed"])

    def test_last_observation_is_not_query_window_end(self):
        result = self.render(payload())
        coverage = result["analysis"]["coverage"]
        self.assertEqual("2026-10-19T10:15:00+08:00", coverage["last_observation"])
        self.assertEqual(SCOPE["end_time"], coverage["query_end"])
        self.assertFalse(coverage["continuous_coverage_verified"])
        self.assertIn("查询窗口终点不等于最后观测时间", result["answer"])

    def test_user_report_overclaims_are_removed(self):
        bad_texts = ["无渐进增长趋势，因此排除连接泄漏。", "即便流量不变，缩池也足以导致饱和。",
                     "异常持续至窗口结束10:20。", "故障持续到10:20。",
                     "根本原因是未经容量评估且缺少审批、无告警。", "可用性受损约17-18%。",
                     "窗口总体可用性为82%。", "确认根因，服务已恢复。", "这是高置信度结论。"]
        for text in bad_texts:
            with self.subTest(text=text):
                data = payload()
                data["hypotheses"][0]["text"] = text
                result = self.render(data)
                self.assertFalse(result["complete"])
                self.assertEqual([], result["analysis"]["hypotheses"])
                self.assertNotIn(text, result["answer"])

    def test_process_checks_are_questions_and_not_root_causes(self):
        data = payload()
        data["process_checks"] = ["缺少审批", "未经容量评估", "无告警"]
        result = self.render(data)
        self.assertTrue(result["complete"], result["issues"])
        self.assertIn("待核查（尚未证实）：缺少审批；需查阅对应记录", result["answer"])
        self.assertIn("尚未证实", result["answer"])

    def test_qualified_uncertainty_and_validation_are_allowed(self):
        data = payload()
        data["hypotheses"][0]["text"] = "现有样本不能排除连接泄漏，需继续验证连接管理行为。"
        data["hypotheses"][0]["validation"] = ["检查是否缺少审批材料；验证是否已恢复必须取得后续遥测。"]
        result = self.render(data)
        self.assertTrue(result["complete"], result["issues"])

    def test_unknown_ids_and_empty_hypothesis_citations(self):
        for ids in (["fabricated-id"], []):
            data = payload()
            data["hypotheses"][0]["evidence_ids"] = ids
            result = self.render(data)
            self.assertFalse(result["complete"])
            self.assertFalse(result["analysis"]["hypotheses"])
            self.assertNotIn("fabricated-id", result["answer"])

    def test_unreferenced_recommendation_is_explicitly_unapproved(self):
        data = payload()
        data["recommendations"] = [{"text": "建议评审恢复阈值，暂拟连续观察15分钟。", "evidence_ids": []}]
        result = self.render(data)
        self.assertTrue(result["complete"], result["issues"])
        self.assertIn("无证据引用，仅为待评审建议", result["answer"])
        self.assertFalse(result["analysis"]["thresholds_approved"])

    def test_unknown_inline_citation_cannot_hide_behind_valid_id_array(self):
        for group, field in (("hypotheses", "text"), ("hypotheses", "validation"), ("recommendations", "text")):
            data = payload()
            if field == "validation":
                data[group][0][field] = ["继续核查 [fabricated-inline]"]
            else:
                data[group][0][field] += " [fabricated-inline]"
            result = self.render(data)
            self.assertFalse(result["complete"])
            self.assertFalse(result["analysis"][group])
            self.assertNotIn("fabricated-inline", result["answer"])

    def test_no_current_telemetry_is_conservative(self):
        knowledge = [{"id": "rb-only", "service": "order-service", "environment": "demo",
                      "timestamp": "2026-09-01T00:00:00+08:00", "evidence_type": "historical_knowledge",
                      "current_telemetry": False}]
        for evidence in ([], knowledge):
            result = self.render(payload(), evidence=evidence)
            self.assertFalse(result["complete"])
            self.assertFalse(result["analysis"]["hypotheses"])
            self.assertIn("没有可用当前遥测", result["answer"])

    def test_historical_only_hypothesis_rejected_even_with_other_current_facts(self):
        data = payload()
        knowledge = {"id": "rb-only", "service": "order-service", "environment": "demo",
                     "timestamp": "2026-09-01T00:00:00+08:00", "evidence_type": "historical_knowledge",
                     "current_telemetry": False}
        data["hypotheses"][0]["evidence_ids"] = ["rb-only"]
        result = self.render(data, evidence=self.evidence + [knowledge])
        self.assertFalse(result["complete"])
        self.assertTrue(result["analysis"]["deterministic_facts"])
        self.assertFalse(result["analysis"]["hypotheses"])
        self.assertTrue(any(i["code"] == "historical_only_hypothesis" for i in result["issues"]))
        data["hypotheses"][0]["evidence_ids"].append("l-order-003")
        self.assertTrue(self.render(data, evidence=self.evidence + [knowledge])["complete"])

    def test_invalid_json_unknown_keys_duplicate_keys_and_legacy_markdown(self):
        invalid = ["# 根因\n已恢复", "not-json", "[]", '{"hypotheses": [], "hypotheses": []}',
                   dict(payload(), facts=["无审批，已恢复"]),
                   "```\n" + json.dumps(payload()) + "\n```", "前言\n[" + json.dumps(payload()) + "]"]
        malformed = payload()
        malformed["hypotheses"][0]["extra"] = "not allowed"
        invalid.append(malformed)
        for raw in invalid:
            with self.subTest(raw=raw):
                result = self.render(raw)
                self.assertFalse(result["complete"])
                self.assertTrue(result["issues"])
                self.assertNotIn("# 根因\n已恢复", result["answer"])
                self.assertFalse(result["analysis"]["hypotheses"])

    def test_single_json_fence_is_supported(self):
        result = self.render("```json\n" + json.dumps(payload(), ensure_ascii=False) + "\n```")
        self.assertTrue(result["complete"], result["issues"])

    def test_schema_limits_and_out_of_scope_records(self):
        self.assertFalse(FINAL_SCHEMA["additionalProperties"])
        for key, count in (("hypotheses", 6), ("recommendations", 6), ("missing_evidence", 11), ("process_checks", 6)):
            data = payload()
            data[key] = data[key] * count
            result = self.render(data)
            self.assertTrue(result["complete"], result["issues"])
            limit = FINAL_SCHEMA["properties"][key]["maxItems"]
            self.assertEqual(data[key][:limit], result["analysis"][key])
            self.assertEqual(count - limit, result["normalizations"][0]["dropped_count"])
        data = payload()
        data["hypotheses"][0]["text"] = "x" * 601
        self.assertFalse(self.render(data)["complete"])
        other_scope = deepcopy(SCOPE)
        other_scope["environment"] = "production"
        result = self.render(payload(), scope=other_scope)
        self.assertFalse(result["complete"])
        self.assertFalse(result["analysis"]["deterministic_facts"])

    def test_plain_explanation_is_normalized_without_contract_failure(self):
        result = self.render("以下为结构化分析：\n" + json.dumps(payload(), ensure_ascii=False) + "\n以上内容待评审。")
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual("plain_prose_removed", result["normalizations"][0]["code"])
        self.assertNotIn("以上内容待评审。", result["answer"])

    def test_real_response_preface_and_six_process_items_pass_guard(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "qwen_real_responses.json").read_text(encoding="utf-8"))
        for name in ("swarm_diagnosis", "swarm_triage", "swarm_remediation"):
            with self.subTest(name=name):
                result = prepare_final_answer(fixture["responses"][name]["content"], fixture["evidence"], fixture["scope"], name)
                self.assertTrue(result["complete"], result["issues"])
                self.assertTrue(result["analysis"]["hypotheses"])
                if name == "swarm_triage":
                    self.assertEqual(5, len(result["analysis"]["process_checks"]))
                    self.assertEqual(1, result["normalizations"][0]["dropped_count"])
        result = prepare_final_answer(fixture["responses"]["single_diagnosis"]["content"], fixture["evidence"], fixture["scope"], "diagnosis")
        self.assertFalse(result["complete"])
        self.assertEqual("invalid_structure", result["issues"][0]["code"])

    def test_only_top_level_counts_are_normalized(self):
        for mutation in ("long_text", "long_validation", "missing_key", "malformed_dropped_item"):
            data = payload()
            if mutation == "long_text":
                data["hypotheses"][0]["text"] = "x" * 601
            elif mutation == "long_validation":
                data["hypotheses"][0]["validation"] *= 5
            elif mutation == "missing_key":
                del data["process_checks"]
            else:
                data["process_checks"] = ["正常流程待核查"] * 5 + [{"unknown": "must still fail"}]
            result = self.render(data)
            self.assertFalse(result["complete"])
            self.assertTrue(result["issues"])

    def test_normalization_does_not_bypass_fake_ids_or_overclaim_checks(self):
        for bad in ("服务已恢复。", "可用性受损17-18%。", "确认根因。", "不能确认根因，但服务已恢复。"):
            data = payload()
            data["hypotheses"][0]["text"] = bad
            result = self.render("以下是报告：\n" + json.dumps(data, ensure_ascii=False))
            self.assertFalse(result["complete"])
            self.assertFalse(result["analysis"]["hypotheses"])
        data = payload()
        data["hypotheses"][0]["evidence_ids"] = ["fake-citation"]
        self.assertFalse(self.render("说明：\n" + json.dumps(data))["complete"])

    def test_live_guard_three_precise_negations_are_retained(self):
        missing = "缺少 10:15 之后至窗口终点 10:20 的遥测点，最后观测时间不等于恢复或持续到窗口结束"
        hypothesis = (
            "候选根因1（发布配置缩池导致连接池饱和）：CHG-SYNTHETIC-1019（v2.8.0→v2.8.1，10:00）将 DB_POOL_MAX 由 50 改为 5；"
            "此后 db_pool_active=5 恒等于 db_pool_max=5、db_pool_waiters 达 86/74，日志与链路出现 connection_acquisition_timeout"
            "（db.acquire ~3001ms 超过 DB_ACQUIRE_TIMEOUT_MS=3000），伴随 p95 由 180ms 升至 3150ms、错误率点值升至 0.183/0.171。"
            "database_cpu 单点 0.29 暂不支持数据库资源瓶颈，但不足以排除锁等待、慢查询或 I/O。时间相关不等于因果，未确认根因。"
        )
        recommendation = (
            "待评审：影响面尚未量化（build_postmortem 标注 impact 未由草稿计算），建议在定义观测总体后按分钟粒度统计超时错误数与请求总数占比，"
            "再评估用户影响，不以点错误率推算窗口整体可用性。"
        )
        data = payload()
        data["missing_evidence"] = [missing]
        data["hypotheses"][0]["text"] = hypothesis
        data["recommendations"][0]["text"] = recommendation
        result = self.render(data)
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual([], result["issues"])
        self.assertEqual(hypothesis, result["analysis"]["hypotheses"][0]["text"])
        self.assertEqual([missing], result["analysis"]["missing_evidence"])
        self.assertEqual(recommendation, result["analysis"]["recommendations"][0]["text"])

    def test_local_negation_does_not_cover_new_assertions(self):
        bad = ["未确认根因，但服务已恢复。", "未确认根因。服务已恢复。", "未确认根因；服务已恢复。",
               "最后观测时间不等于恢复或持续到窗口结束，但故障持续到窗口结束。",
               "最后观测时间不等于持续到窗口结束。故障持续到10:20。",
               "不以点错误率推算窗口整体可用性，但窗口整体可用性为82%。",
               "不以点错误率推算窗口整体可用性。窗口整体可用性为82%。",
               "未完成复盘并已恢复。"]
        for text in bad:
            with self.subTest(text=text):
                data = payload()
                data["hypotheses"][0]["text"] = text
                result = self.render(data)
                self.assertFalse(result["complete"])
                self.assertFalse(result["analysis"]["hypotheses"])

    def test_missing_evidence_has_no_global_semantic_exemption(self):
        for text in ("服务已恢复。", "故障持续到窗口结束。", "窗口整体可用性为82%。"):
            data = payload()
            data["missing_evidence"] = [text]
            result = self.render(data)
            self.assertFalse(result["complete"])
            self.assertFalse(result["analysis"]["missing_evidence"])

    def test_missing_process_records_are_an_explicit_evidence_gap(self):
        texts = ["缺少告警触发记录，无法确认监控系统是否及时捕获了连接池饱和事件。",
                 "缺少审批记录，无法核实审批是否完成。", "缺少审批材料。", "缺乏告警覆盖证据。",
                 "没有告警投递日志，尚不能确认通知是否送达。"]
        for text in texts:
            with self.subTest(text=text):
                data = payload()
                data["missing_evidence"] = [text]
                result = self.render(data)
                self.assertTrue(result["complete"], result["issues"])
                self.assertEqual([text], result["analysis"]["missing_evidence"])

    def test_process_record_gap_does_not_excuse_missing_process_assertions(self):
        texts = ["缺少告警导致故障。", "缺少审批导致事故。", "缺少告警触发记录，无告警。",
                 "缺少告警触发记录。无告警导致故障。", "缺少审批记录，因此没有审批。"]
        for text in texts:
            with self.subTest(text=text):
                data = payload()
                data["missing_evidence"] = [text]
                result = self.render(data)
                self.assertFalse(result["complete"])
                self.assertFalse(result["analysis"]["missing_evidence"])
                self.assertTrue(any(i["code"] == "unverified_process_cause" for i in result["issues"]))

    def test_capacity_assessment_gap_assertion_remains_a_quality_issue(self):
        text = (
            "待评审：复盘草稿要点——①配置模板变更（池 50→5）未经容量评估即随发布上线的流程缺口待核查；"
            "②db_pool_waiters/acquire_timeout 类信号是否已有告警覆盖待核查；"
            "③发布后 5~10 分钟内错误率显著上升的观测与回滚决策时延；④合成演练数据，结论仅适用于 demo 环境本次窗口。"
        )
        data = payload()
        data["recommendations"][0]["text"] = text
        result = self.render(data)
        self.assertFalse(result["complete"])
        self.assertFalse(result["analysis"]["recommendations"])
        self.assertTrue(any(i["code"] == "unverified_process_cause" for i in result["issues"]))

    def test_missing_alert_document_allows_bounded_noun_phrases(self):
        texts = ["缺少告警触发与通知链路记录，无法确认错误率/延迟升高是否被及时告警。",
                 "缺少告警规则是否因阈值变更而失效或未触发的记录",
                 "缺少审批签署与审阅结果材料，尚不能核实流程执行情况。",
                 "缺少告警规则配置与事件送达链路证据。", "缺少告警触发、通知及送达链路记录，无法核实。"]
        for text in texts:
            with self.subTest(text=text):
                data = payload()
                data["missing_evidence"] = [text]
                result = self.render(data)
                self.assertTrue(result["complete"], result["issues"])
                self.assertEqual([text], result["analysis"]["missing_evidence"])

    def test_missing_process_cannot_bridge_assertions_to_document_nouns(self):
        texts = ["缺少告警导致故障的记录。", "缺少告警造成事故的证据。", "缺少告警说明平台没有日志。",
                 "缺少告警证明流程失效的材料。", "缺少告警所以没有日志。", "缺少告警因此丢失日志。",
                 "缺少告警是事故原因的记录。", "缺少告警已经造成故障的记录。",
                 "缺少告警，后续查到触发与通知链路记录。",
                 "缺少告警触发与通知链路记录，无告警。",
                 "缺少告警规则是否因阈值变更而失效或未触发的记录，无告警。",
                 "缺少告警触发与通知链路记录但实际上无告警。",
                 "缺少告警触发与通知链路记录。因此无告警。"]
        for text in texts:
            with self.subTest(text=text):
                data = payload()
                data["missing_evidence"] = [text]
                result = self.render(data)
                self.assertFalse(result["complete"])
                self.assertFalse(result["analysis"]["missing_evidence"])
                self.assertTrue(any(i["code"] == "unverified_process_cause" for i in result["issues"]))

    def test_saved_workbench_missing_configuration_information_is_not_a_process_claim(self):
        fixture = json.loads((ROOT / "tests/fixtures/process_gap_regression.json").read_text(encoding="utf-8"))
        result = self.render(fixture["response"])
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual(fixture["response"]["missing_evidence"], result["analysis"]["missing_evidence"])

    def test_process_document_requests_remain_visible_but_assertions_are_blocked(self):
        allowed = ["缺少审批/压测记录，无法确认这次变更是否完成评估。",
                   "缺少告警配置信息以确认是否覆盖当前异常。",
                   "缺少告警配置快照以核实当时规则是否生效。"]
        rejected = ["缺少审批导致故障。", "缺少告警配置信息，缺少审批导致故障。",
                    "缺少告警配置信息但实际上无告警。", "缺少告警配置信息，因此没有审批。"]
        for text in allowed + rejected:
            data = payload(); data["missing_evidence"] = [text]
            result = self.render(data)
            if text in allowed:
                self.assertTrue(result["complete"], result["issues"])
                self.assertEqual([text], result["analysis"]["missing_evidence"])
            else:
                self.assertFalse(result["complete"])
                self.assertTrue(any(i["code"] == "unverified_process_cause" for i in result["issues"]))

    def test_sparse_staging_samples_do_not_mean_complete_investigation(self):
        reg = SkillRegistry(ROOT / "data", "order-service", "staging", SCOPE["start_time"], SCOPE["end_time"])
        for name in ("query_metrics", "search_logs", "analyze_traces", "get_changes"):
            reg.execute(name, {})
        data = {key: [] for key in ("hypotheses", "recommendations", "missing_evidence", "process_checks")}
        result = prepare_final_answer(json.dumps(data), reg.observed_evidence(), dict(SCOPE, environment="staging"), "diagnosis")
        self.assertFalse(result["complete"])
        self.assertIn("insufficient_current_material", {i["code"] for i in result["issues"]})
        self.assertEqual(2, result["analysis"]["coverage"]["sample_count"])


if __name__ == "__main__":
    unittest.main()
