from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops_agent.skills import SkillRegistry


def registry(service="order-service", environment="demo", start="2026-10-19T09:55:00+08:00", end="2026-10-19T10:20:00+08:00"):
    return SkillRegistry(ROOT / "data", service, environment, start, end)


class SkillTests(unittest.TestCase):
    def test_eight_schemas_and_allowlist(self):
        reg = registry()
        schemas = reg.schemas()
        self.assertEqual(8, len(schemas))
        self.assertEqual(8, len({s["function"]["name"] for s in schemas}))
        self.assertTrue(all(s["function"]["parameters"]["additionalProperties"] is False for s in schemas))
        self.assertEqual(["search_logs"], [s["function"]["name"] for s in reg.schemas(["search_logs"])])
        self.assertEqual([], reg.schemas([]))
        with self.assertRaises(ValueError):
            reg.schemas(["execute_shell"])

    def test_scope_filters_service_environment_and_window(self):
        reg = registry()
        for name in ("query_metrics", "search_logs", "analyze_traces", "get_changes"):
            result = reg.execute(name, {})
            self.assertTrue(result["ok"])
            self.assertTrue(result["evidence"])
            for row in result["evidence"]:
                self.assertEqual("order-service", row["service"])
                self.assertEqual("demo", row["environment"])
                self.assertTrue(row["synthetic"])
                self.assertLessEqual(datetime.fromisoformat(reg.start_time), datetime.fromisoformat(row["timestamp"]))
                self.assertGreaterEqual(datetime.fromisoformat(reg.end_time), datetime.fromisoformat(row["timestamp"]))
        ids = reg.observed
        self.assertNotIn("m-order-before", ids)
        self.assertNotIn("m-order-staging", ids)
        self.assertNotIn("m-payment-001", ids)
        payment = registry(service="payment-service").execute("query_metrics", {})
        self.assertTrue(payment["evidence"])
        self.assertTrue(all(r["service"] == "payment-service" for r in payment["evidence"]))

    def test_empty_window_has_no_invented_records(self):
        reg = registry(start="2026-10-20T09:55:00+08:00", end="2026-10-20T10:20:00+08:00")
        for name in ("query_metrics", "search_logs", "analyze_traces", "get_changes"):
            result = reg.execute(name, {})
            self.assertTrue(result["ok"])
            self.assertEqual([], result["evidence"])
            self.assertEqual("no_data", result["data"]["status"])
        self.assertIsNone(reg.execute("analyze_traces", {})["data"]["max_duration_ms"])
        self.assertFalse(reg.observed)

    def test_reject_unknown_parameters_and_bad_types(self):
        reg = registry()
        for name, arguments in [("query_metrics", {"service": "payment-service"}),
                                ("get_changes", {"environment": "staging"}),
                                ("analyze_traces", {"start_time": "2020-01-01"}),
                                ("search_logs", {"keyword": 4}),
                                ("search_runbooks", {}),
                                ("unknown_skill", {}),
                                ("query_metrics", []),
                                ("assess_remediation", {"action": "rollback", "evidence_ids": "m-order-001"})]:
            with self.subTest(name=name, arguments=arguments):
                result = reg.execute(name, arguments)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertEqual([], result["evidence"])

    def test_requires_timezone_and_valid_interval(self):
        with self.assertRaises(ValueError):
            registry(start="2026-10-19T09:55:00")
        with self.assertRaises(ValueError):
            registry(start="2026-10-20T09:55:00+08:00")

    def test_historical_knowledge_validity_and_service(self):
        result = registry().execute("search_runbooks", {"query": "pool timeout"})
        self.assertTrue(result["ok"])
        ids = {r["id"] for r in result["evidence"]}
        self.assertIn("rb-order-pool-v3", ids)
        self.assertNotIn("rb-order-expired", ids)
        self.assertNotIn("rb-payment-v1", ids)
        for row in result["evidence"]:
            self.assertEqual("historical_knowledge", row["evidence_type"])
            self.assertFalse(row["current_telemetry"])
        self.assertEqual([], registry().execute("search_runbooks", {"query": "nothing-matches-xyz"})["evidence"])

    def test_cannot_cite_unobserved_or_fabricated_evidence(self):
        reg = registry()
        for skill, argument in [("assess_remediation", "action"), ("build_postmortem", "summary")]:
            for eid in ("m-order-001", "fabricated-123", "m-payment-001"):
                self.assertFalse(reg.execute(skill, {argument: "rollback review", "evidence_ids": [eid]})["ok"])
        result = reg.execute("query_metrics", {"metric": "request_error_rate"})
        ids = [r["id"] for r in result["evidence"]]
        plan = reg.execute("assess_remediation", {"action": "rollback v2.8.1", "evidence_ids": ids})
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["data"]["executed"])
        self.assertTrue(plan["data"]["human_approval_required"])
        self.assertFalse(plan["data"]["approval_granted"])
        report = reg.execute("build_postmortem", {"summary": "Suspected connection pool regression", "evidence_ids": ids})
        self.assertTrue(report["ok"])
        self.assertFalse(report["data"]["root_cause"]["confirmed"])
        self.assertFalse(report["data"]["recovery_verified"])
        self.assertEqual([], report["data"]["actions_executed"])

    def test_thread_safe_observation_and_return_isolation(self):
        reg = registry()
        names = ("query_metrics", "search_logs", "analyze_traces", "get_changes")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda name: reg.execute(name, {}), names))
        ids = {row["id"] for result in results for row in result["evidence"]}
        self.assertEqual(ids, reg.observed)
        results[0]["evidence"][0]["id"] = "mutated-by-caller"
        reread = reg.execute("query_metrics", {})
        self.assertNotIn("mutated-by-caller", {r["id"] for r in reread["evidence"]})
        self.assertTrue(reg.execute("build_postmortem", {"summary": "draft", "evidence_ids": sorted(ids)})["ok"])
        json.dumps(results + [reread], ensure_ascii=False)

    def test_observed_evidence_is_canonical_copy(self):
        reg = registry()
        self.assertEqual([], reg.observed_evidence())
        returned = reg.execute("search_logs", {"keyword": "timeout"})
        expected_ids = {row["id"] for row in returned["evidence"]}
        snapshot = reg.observed_evidence()
        self.assertEqual(expected_ids, {row["id"] for row in snapshot})
        original_id = snapshot[0]["id"]
        original_message = snapshot[0]["message"]
        snapshot[0]["id"] = "forged-id"
        snapshot[0]["message"] = "forged-content"
        snapshot.append({"id": "additional-forgery"})
        canonical = reg.observed_evidence()
        self.assertEqual(expected_ids, {row["id"] for row in canonical})
        self.assertEqual(original_message, next(row["message"] for row in canonical if row["id"] == original_id))
        self.assertNotIn("forged-id", reg.observed)

    def test_collect_evidence_has_compact_summaries_and_unique_canonical_rows(self):
        reg = registry()
        kinds = ["metrics", "logs", "traces", "changes", "runbooks"]
        result = reg.execute("collect_evidence", {"kinds": kinds})
        self.assertTrue(result["ok"])
        self.assertFalse(result["cache_hit"])
        self.assertGreaterEqual(result["duration_ms"], 0)
        ids = [row["id"] for row in result["evidence"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), reg.observed)
        self.assertEqual(reg.observed_evidence(), sorted(result["evidence"], key=lambda row: row["id"]))
        self.assertEqual(len(ids), result["data"]["evidence_count"])
        for kind, summary in result["data"]["by_kind"].items():
            self.assertNotIn("rows", summary)
            self.assertNotIn(kind, summary if kind != "metrics" else {})
            self.assertNotIn("samples", json.dumps(summary))
            self.assertNotIn('"id"', json.dumps(summary))
        self.assertEqual({"backend_executions": 5, "cache_hits": 0, "cache_misses": 5}, reg.cache_stats())
        again = reg.execute("collect_evidence", {"kinds": list(reversed(kinds)), "query": "timeout connection pool latency"})
        self.assertTrue(again["cache_hit"])
        self.assertEqual(5, reg.cache_stats()["backend_executions"])
        result["evidence"][0]["id"] = "forged"
        self.assertNotIn("forged", {row["id"] for row in again["evidence"]})

    def test_collect_reuses_existing_leaf_cache_and_later_queries_reuse_collection(self):
        reg = registry()
        reg.execute("query_metrics", {})
        reg.execute("search_logs", {"keyword": ""})
        collected = reg.execute("collect_evidence", {"kinds": ["metrics", "logs", "changes"]})
        self.assertTrue(collected["ok"])
        self.assertEqual({"backend_executions": 3, "cache_hits": 2, "cache_misses": 3}, reg.cache_stats())
        self.assertTrue(reg.execute("get_changes", {})["cache_hit"])
        self.assertEqual(3, reg.cache_stats()["cache_hits"])

    def test_cache_normalizes_defaults_but_keeps_different_arguments_separate(self):
        reg = registry()
        self.assertFalse(reg.execute("query_metrics", {})["cache_hit"])
        self.assertTrue(reg.execute("query_metrics", {"metric": "all"})["cache_hit"])
        self.assertFalse(reg.execute("query_metrics", {"metric": "request_error_rate"})["cache_hit"])
        self.assertFalse(reg.execute("search_logs", {})["cache_hit"])
        self.assertTrue(reg.execute("search_logs", {"keyword": ""})["cache_hit"])
        self.assertFalse(reg.execute("search_logs", {"keyword": "timeout"})["cache_hit"])
        self.assertFalse(reg.execute("search_logs", {"keyword": "TIMEOUT"})["cache_hit"])
        self.assertEqual({"backend_executions": 5, "cache_hits": 2, "cache_misses": 5}, reg.cache_stats())
        independent = registry(service="payment-service")
        self.assertFalse(independent.execute("query_metrics", {})["cache_hit"])
        self.assertEqual(1, independent.cache_stats()["backend_executions"])
        stats = reg.cache_stats()
        stats["backend_executions"] = -1
        self.assertEqual(5, reg.cache_stats()["backend_executions"])

    def test_singleflight_runs_same_backend_only_once(self):
        reg = registry()
        original = reg._query_metrics
        barrier = threading.Barrier(8)

        def slow_metrics(**arguments):
            time.sleep(0.03)
            return original(**arguments)

        def request(_):
            barrier.wait(timeout=5)
            return reg.execute("query_metrics", {})

        with patch.object(reg, "_query_metrics", side_effect=slow_metrics) as backend:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(request, range(8)))
            self.assertEqual(1, backend.call_count)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(7, sum(result["cache_hit"] for result in results))
        self.assertEqual({"backend_executions": 1, "cache_hits": 7, "cache_misses": 1}, reg.cache_stats())
        results[0]["evidence"][0]["id"] = "forged"
        self.assertTrue(all(result["evidence"][0]["id"] != "forged" for result in results[1:]))

    def test_failures_are_not_retained_and_partial_collection_can_retry(self):
        reg = registry()
        original = reg._search_logs
        with patch.object(reg, "_search_logs", side_effect=[OSError("temporary IO failure"), original()]):
            failed = reg.execute("collect_evidence", {"kinds": ["metrics", "logs"]})
            self.assertFalse(failed["ok"])
            self.assertTrue(failed["evidence"])
            self.assertEqual("partial_failure", failed["data"]["status"])
            recovered = reg.execute("collect_evidence", {"kinds": ["metrics", "logs"]})
            self.assertTrue(recovered["ok"])
            self.assertFalse(recovered["cache_hit"])
        self.assertEqual({"backend_executions": 3, "cache_hits": 1, "cache_misses": 3}, reg.cache_stats())
        self.assertTrue(reg.execute("collect_evidence", {"kinds": ["metrics", "logs"]})["cache_hit"])
        draft_args = {"summary": "draft", "evidence_ids": ["c-order-001"]}
        # An initially unobserved citation can succeed after the required read.
        first = reg.execute("get_changes", {})
        evidence_id = first["evidence"][0]["id"]
        independent = registry()
        draft_args["evidence_ids"] = [evidence_id]
        self.assertFalse(independent.execute("build_postmortem", draft_args)["ok"])
        independent.execute("get_changes", {})
        self.assertTrue(independent.execute("build_postmortem", draft_args)["ok"])

    def test_collect_validates_local_schema_before_backend_execution(self):
        reg = registry()
        for arguments in ({}, {"kinds": []}, {"kinds": ["metrics", "metrics"]},
                          {"kinds": ["unknown"]}, {"kinds": "metrics"}, {"kinds": [3]},
                          {"kinds": ["metrics"], "extra": True}, {"kinds": ["runbooks"], "query": ""}):
            with self.subTest(arguments=arguments):
                self.assertFalse(reg.execute("collect_evidence", arguments)["ok"])
        self.assertEqual({"backend_executions": 0, "cache_hits": 0, "cache_misses": 0}, reg.cache_stats())


if __name__ == "__main__":
    unittest.main()
