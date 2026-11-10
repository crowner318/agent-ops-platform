"""User input must survive compilation without inventing evidence or scope."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from workbench.materials import MAX_MATERIAL_BYTES, material_template, prepare_materials

ROOT = Path(__file__).resolve().parents[2]
AT = "2026-11-06T10:00:00+08:00"
OPS = {"service": "billing-api", "environment": "production", "start": "2026-11-06T09:55:00+08:00", "end": "2026-11-06T10:20:00+08:00"}
RELEASE = {"service": "billing-api", "environment": "production", "change_id": "release-42"}


def fields(**extra):
    return {"format": "fields", "observed_at": AT, "synthetic": False, **extra}


class MaterialTests(unittest.TestCase):
    def test_ops_simple_fields_keep_user_values_and_source(self):
        raw = fields(metrics="request_error_rate=0.183 ratio\nrequest_rate=120 requests_per_second",
            logs="ERROR connection acquisition timeout\nordinary text", traces="POST /orders|error|3000|db.acquire:2900,db.query:100",
            changes="Changed connection pool from 50 to 5")
        files, observed, source = prepare_materials("ops", raw, OPS)
        data = files["demo_data.json"]
        self.assertEqual({"metrics": 2, "logs": 2, "traces": 1, "changes": 1}, observed["counts"])
        self.assertEqual("complete", observed["material_status"])
        self.assertEqual("用户提供资料（未核实）", source)
        self.assertEqual("user_supplied", observed["source"])
        self.assertEqual([0.183, 120.0], [row["value"] for row in data["metrics"]])
        self.assertEqual(["ERROR", "UNKNOWN"], [row["level"] for row in data["logs"]])
        self.assertEqual("user_provided", data["changes"][0]["change_type"])
        self.assertEqual([2900.0, 100.0], [span["duration_ms"] for span in data["traces"][0]["spans"]])
        self.assertTrue(all(row["environment"] == "production" and row["synthetic"] is False
            for kind in ("metrics", "logs", "traces", "changes") for row in data[kind]))

    def test_sparse_ops_does_not_fabricate_missing_categories(self):
        files, observed, _ = prepare_materials("ops", fields(logs="One provided sample"), OPS)
        self.assertEqual(1, observed["current_count"])
        self.assertEqual(["metrics", "traces", "changes"], observed["missing_kinds"])
        self.assertEqual([], files["demo_data.json"]["metrics"])
        self.assertEqual("partial", observed["material_status"])

    def test_ops_unit_is_explicitly_unspecified_and_signed_metric_kept(self):
        files, _, _ = prepare_materials("ops", fields(metrics="temperature=-2.5"), OPS)
        row = files["demo_data.json"]["metrics"][0]
        self.assertEqual(-2.5, row["value"])
        self.assertEqual("unspecified", row["unit"])

    def test_log_or_change_text_is_inert_material(self):
        text = "$(echo secret); <script>alert(1)</script> https://example.invalid/ignored"
        files, _, _ = prepare_materials("ops", fields(logs=text, changes=text), OPS)
        self.assertEqual(text, files["demo_data.json"]["logs"][0]["message"])
        self.assertEqual(text, files["demo_data.json"]["changes"][0]["description"])

    def test_release_values_compile_to_native_rules_without_assuming_execution(self):
        raw = fields(release_purpose="Validate billing fields", change_summary="v1 to v2", owner="Provided owner",
            rollback_plan="Pause then follow authorized rollback", validation_plan="Run isolated smoke tests",
            config_before="50", config_after="5", code_diff="Changed validation", ci_status="passed", ci_details="CI run 12 passed",
            replicas="8", pool_max="5", db_max_connections="200", other_connections="180")
        files, observed, source = prepare_materials("release", raw, RELEASE)
        data = files["fixtures.json"]
        change = data["changes"]["release-42"]
        self.assertEqual(RELEASE | {"snapshot_id": data["snapshot_id"]}, change["scope"])
        self.assertEqual("complete", observed["material_status"])
        budget = change["evidence"]["dependency_budget"][0]["content"]
        self.assertEqual(40, budget["requested_connections"])
        self.assertEqual(20, budget["available_connections"])
        self.assertIn("excludes", budget["current_connections_scope"])
        current = change["evidence"]["release_snapshot"][0]["content"]
        self.assertEqual(raw["rollback_plan"], current["rollback_plan"])
        self.assertNotIn("executed", current)
        self.assertEqual("用户提供资料（未核实）", source)

    def test_release_partial_input_preserved_and_missing_stays_missing(self):
        raw = fields(change_summary="Provided text only", config_after=5, replicas=8, ci_status="unknown", ci_details="No conclusion")
        files, observed, _ = prepare_materials("release", raw, RELEASE)
        evidence = files["fixtures.json"]["changes"]["release-42"]["evidence"]
        self.assertEqual(["config_diff", "code_diff", "ci_checks", "dependency_budget"], observed["missing_kinds"])
        content = evidence["release_snapshot"][0]["content"]
        self.assertEqual({"pool_before": None, "pool_after": 5}, content["partial_configuration_material"])
        self.assertEqual({"replicas": 8}, content["partial_capacity_material"])
        self.assertEqual("No conclusion", content["unconfirmed_ci_material"])
        self.assertNotIn("owner", content)

    def test_release_rejects_contradictory_capacity_and_config(self):
        for extra in ({"config_after": 5, "pool_max": 50},
                      {"db_max_connections": 20, "other_connections": 21, "replicas": 1, "pool_max": 1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                prepare_materials("release", fields(change_summary="test", **extra), RELEASE)

    def test_ops_native_arrays_preserve_richer_fields(self):
        row = {"id": "change-1", "service": OPS["service"], "environment": OPS["environment"], "timestamp": AT,
            "synthetic": False, "change_type": "configuration", "description": "Pool differs", "before": {"pool": 50}, "after": {"pool": 5}, "diff_complete": False}
        files, _, _ = prepare_materials("ops", fields(changes=json.dumps([row])), OPS)
        self.assertEqual(row, files["demo_data.json"]["changes"][0])
        self.assertEqual(row["before"], {"pool": 50})

    def test_native_json_does_not_change_input_or_synthetic_flags(self):
        row = {"id": "log-1", "service": OPS["service"], "environment": OPS["environment"], "timestamp": AT,
               "synthetic": False, "message": "sample"}
        raw = {"format": "json", "synthetic": False, "data": {"logs": [row]}}
        original = deepcopy(raw)
        files, _, _ = prepare_materials("ops", raw, OPS)
        files["demo_data.json"]["logs"][0]["message"] = "changed copy"
        self.assertEqual(original, raw)
        raw["synthetic"] = True
        with self.assertRaises(ValueError):
            prepare_materials("ops", raw, OPS)

    def test_rejects_cross_scope_duplicate_ids_bad_time_and_numeric_types(self):
        row = {"id": "metric-1", "service": OPS["service"], "environment": OPS["environment"], "timestamp": AT,
               "synthetic": False, "metric": "load", "unit": "ratio", "value": 1}
        for altered in (dict(row, service="other-api"), dict(row, environment="staging"),
                        dict(row, timestamp="2026-11-06T10:00:00"), dict(row, value=True), dict(row, value="1"), dict(row, value=float("nan"))):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                prepare_materials("ops", {"format": "json", "synthetic": False, "data": {"metrics": [altered]}}, OPS)
        with self.assertRaises(ValueError):
            prepare_materials("ops", {"format": "json", "synthetic": False, "data": {"metrics": [row, row]}}, OPS)

    def test_empty_out_of_window_and_historical_only_are_rejected(self):
        for raw in (fields(), fields(observed_at="2026-11-05T10:00:00+08:00", logs="outside"), fields(runbooks="Reference only")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                prepare_materials("ops", raw, OPS)
        with self.assertRaises(ValueError):
            prepare_materials("release", fields(), RELEASE)

    def test_raw_unknown_keys_bad_formats_bounds_and_identifier_rejected(self):
        for raw in (fields(logs="x", path="C:/arbitrary"), fields(logs="x", synthetic="false"),
                    fields(logs="x", observed_at="today"), fields(logs="x" * 60001), {"format": "path", "synthetic": True},
                    fields(metrics="load=NaN"), fields(traces="POST /|error|-1"), fields(metrics='[{"id":"x","id":"duplicate"}]')):
            with self.subTest(raw=str(raw)[:100]), self.assertRaises(ValueError):
                prepare_materials("ops", raw, OPS)
        with self.assertRaises(ValueError):
            prepare_materials("ops", fields(logs="x"), dict(OPS, service="../escape"))
        with self.assertRaises(ValueError):
            prepare_materials("ops", fields(logs="\n".join("line" for _ in range(501))), OPS)
        with self.assertRaises(ValueError):
            prepare_materials("ops", fields(**{kind: "文" * 60000 for kind in ("metrics", "logs", "traces", "changes")}), OPS)
        self.assertEqual(524288, MAX_MATERIAL_BYTES)

    def test_templates_are_scope_bound_accepted_and_do_not_expose_paths(self):
        for agent, project, profile in (("ops", "it_ops_agent_fixed_v1_2", "complete-demo"),
                                        ("ops", "it_ops_agent_fixed_v1_2", "healthy-staging"),
                                        ("release", "release_risk_agent", "complete-safe"),
                                        ("release", "release_risk_agent", "complete-risk-pool"),
                                        ("release", "release_risk_agent", "complete-missing")):
            with self.subTest(profile=profile):
                template = material_template(agent, ROOT / project, profile)
                files, observed, source = prepare_materials(agent, template["materials"], template["params"])
                self.assertGreater(observed["current_count"], 0)
                self.assertEqual("用户自填模拟资料", source)
                self.assertEqual({"materials", "params"}, set(template))
                self.assertNotIn("data_dir", json.dumps(template))
                self.assertEqual(1, len(files))
        with self.assertRaises(ValueError):
            material_template("ops", ROOT / "it_ops_agent_fixed_v1_2", "../settings.py")

    def test_release_json_must_have_only_selected_change_and_exact_scope(self):
        template = material_template("release", ROOT / "release_risk_agent", "complete-safe")
        for mutate in (lambda data: data["changes"]["safe"]["scope"].update(service="other"),
                       lambda data: data["changes"].update(extra=deepcopy(data["changes"]["safe"])),
                       lambda data: data["changes"]["safe"]["evidence"]["ci_checks"][0].update(kind="logs")):
            raw = deepcopy(template["materials"])
            mutate(raw["data"])
            with self.assertRaises(ValueError):
                prepare_materials("release", raw, template["params"])

    def test_native_release_rejects_numeric_strings_and_inconsistent_budget(self):
        template = material_template("release", ROOT / "release_risk_agent", "complete-safe")
        for altered in ({"requested_connections": "200"}, {"requested_connections": 500}, {"available_connections": 300}, {"pool_max": 5, "requested_connections": 20}):
            raw = deepcopy(template["materials"])
            raw["data"]["changes"]["safe"]["evidence"]["dependency_budget"][0]["content"].update(altered)
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                prepare_materials("release", raw, template["params"])

    def test_fields_ci_bad_type_rejected_as_validation_error(self):
        with self.assertRaises(ValueError):
            prepare_materials("release", fields(change_summary="test", ci_status=[]), RELEASE)

    def test_evidence_ids_fit_model_reference_schema(self):
        template = material_template("release", ROOT / "release_risk_agent", "complete-safe")
        for invalid_id in ("x" * 129, " padded "):
            raw = deepcopy(template["materials"])
            raw["data"]["changes"]["safe"]["evidence"]["dependency_budget"][0]["id"] = invalid_id
            with self.subTest(id=invalid_id), self.assertRaises(ValueError):
                prepare_materials("release", raw, template["params"])

    def test_runbook_binding_is_explicit_and_not_current_evidence(self):
        files, observed, _ = prepare_materials("ops", fields(logs="sample", runbooks="Check pool capacity"), OPS)
        runbook = files["demo_data.json"]["runbooks"][0]
        self.assertEqual(OPS["start"], runbook["valid_from"])
        self.assertEqual(OPS["end"], runbook["valid_until"])
        self.assertIn("未核实原手册效期", runbook["content"])
        self.assertEqual(1, observed["current_count"])


if __name__ == "__main__":
    unittest.main()
