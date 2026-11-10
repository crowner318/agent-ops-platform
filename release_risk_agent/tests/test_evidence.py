from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from release_guard.evidence import EvidenceStore, tool_names

ROOT = Path(__file__).resolve().parents[1]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore(ROOT / "data")

    def load_modified(self, mutation, legacy=None):
        payload = json.loads((ROOT / "data/fixtures.json").read_text(encoding="utf-8"))
        mutation(payload)
        original_bytes = Path.read_bytes
        original_exists = Path.exists
        def read(path):
            if path.name == "fixtures.json":
                return json.dumps(payload).encode()
            if path.name == "legacy_cases.json":
                return json.dumps(legacy or []).encode()
            return original_bytes(path)
        def exists(path):
            return True if path.name == "legacy_cases.json" else original_exists(path)
        with patch.object(Path, "read_bytes", read), patch.object(Path, "exists", exists):
            return EvidenceStore(ROOT / "data")

    def test_fixed_scope_and_all_seven_tools(self):
        for change_id in ("risk-pool", "safe", "missing", "legacy-demo"):
            for name in tool_names:
                args = {"change_id": change_id}
                if name.startswith("search_"):
                    args["query"] = "pool capacity timeout"
                result = self.store.execute(name, args)
                self.assertTrue(result["ok"], result)
                self.assertEqual(change_id, result["scope"]["change_id"])
                self.assertEqual("demo" if change_id == "legacy-demo" else "staging", result["scope"]["environment"])
                for row in result["evidence"]:
                    self.assertEqual(change_id, row["change_id"])
                    self.assertEqual("order-service", row["service"])
                    self.assertTrue(row["synthetic"])
                    self.assertIsInstance(row["content"], dict)

    def test_scope_override_unknown_paths_commands_and_oversize_rejected(self):
        for name, arguments in [
            ("deploy", {"change_id": "risk-pool"}),
            ("get_config_diff", {"change_id": "risk-pool", "environment": "production"}),
            ("get_config_diff", {"change_id": "../../secrets"}),
            ("get_config_diff", {"change_id": "https://example.com"}),
            ("get_config_diff", {"change_id": "risk-pool", "command": "whoami"}),
            ("search_runbooks", {"change_id": "risk-pool", "query": "x" * 2001}),
            ("search_runbooks", {"change_id": "risk-pool"}),
        ]:
            with self.subTest(name=name, arguments=arguments):
                result = self.store.execute(name, arguments)
                self.assertFalse(result["ok"])
                self.assertFalse(result["evidence"])

    def test_missing_material_does_not_invent_checks(self):
        for name in ("get_ci_checks", "get_dependency_budget"):
            result = self.store.execute(name, {"change_id": "missing"})
            self.assertTrue(result["ok"])
            self.assertEqual([], result["evidence"])
            self.assertEqual("missing", result["data"]["status"])

    def test_returns_deep_copies(self):
        result = self.store.execute("get_config_diff", {"change_id": "risk-pool"})
        result["evidence"][0]["content"]["pool_after"] = 999
        result["scope"]["environment"] = "production"
        again = self.store.execute("get_config_diff", {"change_id": "risk-pool"})
        self.assertEqual(5, again["evidence"][0]["content"]["pool_after"])
        self.assertEqual("staging", again["scope"]["environment"])

    def test_runbook_validity_scope_and_verified_cases_filter_strictly(self):
        def mutation(payload):
            groups = payload["changes"]["risk-pool"]["evidence"]
            original = groups["runbook"][0]
            for suffix in ("expired", "future", "wrong-env", "wrong-service"):
                row = deepcopy(original)
                row["id"] += suffix
                if suffix == "expired": row["content"]["valid_until"] = "2026-01-02T00:00:00+08:00"
                if suffix == "future": row["content"]["valid_from"] = "2027-01-01T00:00:00+08:00"
                if suffix == "wrong-env": row["environment"] = "demo"
                if suffix == "wrong-service": row["service"] = "payment-service"
                groups["runbook"].append(row)
            row = deepcopy(groups["incident_case"][0])
            row["id"] += "-unverified"
            row["content"]["verified"] = False
            groups["incident_case"].append(row)
        store = self.load_modified(mutation)
        self.assertEqual(["risk-pool-runbook"], [r["id"] for r in store.execute("search_runbooks", {"change_id": "risk-pool", "query": "pool"})["evidence"]])
        self.assertEqual(["risk-pool-case"], [r["id"] for r in store.execute("search_incident_cases", {"change_id": "risk-pool", "query": "pool"})["evidence"]])

    def test_legacy_only_verified_same_scope_and_hash_changes(self):
        base = {"id": "imported-one", "service": "order-service", "environment": "staging", "verified": True,
                "summary": "pool timeout case", "synthetic": True, "imported_at": "2026-10-30T09:00:00+08:00"}
        cases = [base, dict(base, id="bad-flag", verified="true"), dict(base, id="bad-env", environment="demo"),
                 dict(base, id="bad-service", service="payment-service"), dict(base, id="not-reviewed", verified=False)]
        store = self.load_modified(lambda p: None, cases)
        result = store.execute("search_incident_cases", {"change_id": "risk-pool", "query": "pool"})
        imported = [r for r in result["evidence"] if r["id"].startswith("legacy-")]
        self.assertEqual(1, len(imported))
        self.assertEqual("imported-one", imported[0]["content"]["source_id"])
        self.assertEqual("risk-pool", imported[0]["change_id"])
        self.assertNotEqual(self.store.content_hash, store.content_hash)
        changed = self.load_modified(lambda p: p.update(snapshot_id="new-snapshot") or [c["scope"].update(snapshot_id="new-snapshot") for c in p["changes"].values()])
        self.assertNotEqual(self.store.content_hash, changed.content_hash)

    def test_duplicate_ids_and_scope_mismatch_rejected(self):
        for mutation in (
            lambda p: p["changes"]["risk-pool"]["evidence"]["config_diff"].append(deepcopy(p["changes"]["risk-pool"]["evidence"]["config_diff"][0])),
            lambda p: p["changes"]["risk-pool"]["scope"].update(environment="production"),
        ):
            with self.assertRaises(ValueError):
                self.load_modified(mutation)


if __name__ == "__main__": unittest.main()
