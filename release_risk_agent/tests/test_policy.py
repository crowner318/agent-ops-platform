from copy import deepcopy
from pathlib import Path
import unittest

from release_guard.evidence import EvidenceStore, tool_names
from release_guard.policy import evaluate


class PolicyTests(unittest.TestCase):
    def evidence(self, change_id):
        store = EvidenceStore(Path(__file__).resolve().parents[1] / "data")
        return [row for name in tool_names[:5] for row in store.execute(name, {"change_id": change_id})["evidence"]]

    def test_pool_drop_and_dependency_blocker(self):
        findings = evaluate(self.evidence("risk-pool"))
        mapping = {f["code"]: f for f in findings}
        self.assertEqual("warning", mapping["POOL_SHARP_REDUCTION"]["severity"])
        self.assertEqual("blocker", mapping["DEPENDENCY_BUDGET_EXCEEDED"]["severity"])
        for finding in findings:
            self.assertEqual({"code", "severity", "title", "detail", "evidence_ids", "source"}, set(finding))
            self.assertEqual("policy", finding["source"])
            self.assertTrue(finding["evidence_ids"])

    def test_safe_and_missing_are_distinct(self):
        self.assertEqual([], evaluate(self.evidence("safe")))
        findings = evaluate(self.evidence("missing"))
        self.assertEqual({"MISSING_CI_MATERIAL", "MISSING_CAPACITY_MATERIAL"}, {f["code"] for f in findings})
        self.assertTrue(all(f["severity"] == "warning" for f in findings))

    def test_ci_failure_is_blocker_and_exact_half_is_warning(self):
        rows = self.evidence("safe")
        next(r for r in rows if r["kind"] == "ci_checks")["content"]["status"] = "failed"
        config = next(r for r in rows if r["kind"] == "config_diff")["content"]
        config["pool_after"] = 25
        findings = {f["code"]: f for f in evaluate(rows)}
        self.assertEqual("blocker", findings["CI_CHECK_FAILED"]["severity"])
        self.assertIn("POOL_SHARP_REDUCTION", findings)

    def test_missing_or_nonfinite_capacity_is_not_false_safe(self):
        for value in (None, True, float("nan"), "200"):
            rows = self.evidence("safe")
            next(r for r in rows if r["kind"] == "dependency_budget")["content"]["requested_connections"] = value
            self.assertIn("MISSING_CAPACITY_MATERIAL", {f["code"] for f in evaluate(rows)})

    def test_scope_mixing_rejected_and_input_not_mutated(self):
        rows = self.evidence("risk-pool")
        original = deepcopy(rows)
        evaluate(rows)
        self.assertEqual(original, rows)
        with self.assertRaises(ValueError):
            evaluate(rows + self.evidence("safe"))


if __name__ == "__main__": unittest.main()
