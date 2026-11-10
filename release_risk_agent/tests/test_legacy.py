from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from release_guard.legacy import MAX_REPORT_BYTES, import_legacy_report


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("release-legacy-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.source = self.root / "old_report.json"
        self.data = self.root / "data"
        self.payload = {"scope": {"service": "order-service", "environment": "demo"},
                        "session_id": "old-session-1", "complete": True, "verified": True,
                        "answer": "连接池候选机制需要进一步验证。api_key=private-import-secret",
                        "evidence": [{"id": "old-metric-1", "service": "order-service", "environment": "demo", "synthetic": True}]}
        self.write(self.payload)

    def cleanup(self):
        if self.data.exists():
            for child in self.data.iterdir():
                child.unlink()
            self.data.rmdir()
        for child in self.root.iterdir():
            child.unlink()
        self.root.rmdir()

    def write(self, value):
        self.source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_default_is_unverified_despite_old_complete_and_verified_flags(self):
        source_before = self.source.read_bytes()
        record = import_legacy_report(self.source, self.data)
        self.assertFalse(record["verified"])
        self.assertTrue(record["report_complete"])
        self.assertTrue(record["synthetic"])
        self.assertEqual(record["original_session_id"], "old-session-1")
        self.assertEqual(record["service"], "order-service")
        self.assertEqual(self.source.read_bytes(), source_before)
        stored = (self.data / "legacy_cases.json").read_text(encoding="utf-8")
        self.assertNotIn("private-import-secret", stored)
        self.assertEqual(len(json.loads(stored)), 1)

    def test_hash_deduplication_and_explicit_human_confirmation(self):
        first = import_legacy_report(self.source, self.data)
        # Pretty-printing and key ordering do not create another case.
        self.source.write_text(json.dumps(self.payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        second = import_legacy_report(self.source, self.data)
        self.assertEqual(first["id"], second["id"])
        confirmed = import_legacy_report(self.source, self.data, confirmed=True)
        self.assertTrue(confirmed["verified"])
        self.assertIn("confirmed_at", confirmed)
        cases = json.loads((self.data / "legacy_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0]["verified"])
        self.assertTrue(import_legacy_report(self.source, self.data)["verified"])

    def test_scope_missing_wildcard_or_cross_environment_is_rejected(self):
        for mutation in ("missing", "wildcard", "wrong-evidence"):
            candidate = deepcopy(self.payload)
            if mutation == "missing":
                del candidate["scope"]
            elif mutation == "wildcard":
                candidate["scope"]["environment"] = "*"
            else:
                candidate["evidence"][0]["environment"] = "production"
            self.write(candidate)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                import_legacy_report(self.source, self.data)
        self.assertFalse((self.data / "legacy_cases.json").exists())

    def test_size_invalid_json_and_invalid_confirmation_are_rejected(self):
        self.source.write_bytes(b" " * (MAX_REPORT_BYTES + 1))
        with self.assertRaises(ValueError):
            import_legacy_report(self.source, self.data)
        self.source.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            import_legacy_report(self.source, self.data)
        self.write(self.payload)
        with self.assertRaises(ValueError):
            import_legacy_report(self.source, self.data, confirmed="true")

    def test_unknown_origin_remains_unverified_and_corrupt_target_is_preserved(self):
        self.payload["evidence"] = []
        self.write(self.payload)
        record = import_legacy_report(self.source, self.data)
        self.assertFalse(record["synthetic"])
        self.assertIn("not independently verified", record["synthetic_notice"])
        target = self.data / "legacy_cases.json"
        target.write_text("do not overwrite this malformed library", encoding="utf-8")
        with self.assertRaises(ValueError):
            import_legacy_report(self.source, self.data, confirmed=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "do not overwrite this malformed library")


if __name__ == "__main__":
    unittest.main()
