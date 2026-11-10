from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from release_guard.storage import Repository


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("release-storage-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.path = self.root / "business.sqlite3"
        self.repo = Repository(self.path)
        self.repo.create_review("review-1", "CHG-001", "demo", "local")

    def cleanup(self):
        for child in self.root.iterdir():
            child.unlink()
        self.root.rmdir()

    def test_original_change_mode_and_coordinator_survive_restart_and_duplicate_id(self):
        self.assertFalse(self.repo.create_review("review-1", "CHG-OTHER", "live", "redis"))
        self.repo.save_result("review-1", {"status": "waiting_human", "review_digest": "a" * 64})
        reopened = Repository(self.path).get_review("review-1")
        self.assertEqual((reopened["change_id"], reopened["mode"], reopened["coordination"]),
                         ("CHG-001", "demo", "local"))
        self.assertEqual(reopened["status"], "waiting_human")
        self.assertIsNone(self.repo.get_review("unknown"))
        self.assertEqual(len(self.repo.list_reviews()), 1)
        with closing(sqlite3.connect(self.path)) as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE reviews SET change_id='CHG-OTHER' WHERE review_id='review-1'")

    def test_parallel_event_writes_preserve_append_order(self):
        def append(index):
            self.repo.append_event("review-1", "node", {"index": index})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(24)))
        events = self.repo.get_review("review-1")["events"]
        self.assertEqual(len(events), 24)
        self.assertEqual({event["payload"]["index"] for event in events}, set(range(24)))
        self.assertEqual([event["id"] for event in events], sorted(event["id"] for event in events))

    def test_human_records_append_only_and_bound_to_review_digest(self):
        self.repo.save_result("review-1", {"status": "waiting_human", "review_digest": "b" * 64})
        record = self.repo.append_human_review("review-1", "Local reviewer", "acknowledge", "b" * 64, "已阅读，仍需独立发布流程")
        self.assertEqual(record["proposed_digest"], "b" * 64)
        self.repo.append_human_review("review-1", "Local reviewer", "request_changes", "b" * 64)
        self.assertEqual(len(self.repo.get_review("review-1")["human_reviews"]), 2)
        with self.assertRaises(ValueError):
            self.repo.append_human_review("review-1", "Local reviewer", "acknowledge", "c" * 64)
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE human_reviews SET decision='acknowledge'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM human_reviews")
        self.assertEqual(len(self.repo.get_review("review-1")["human_reviews"]), 2)

    def test_no_model_event_or_status_can_become_production_approval(self):
        for kind in ("human_review", "human_review_recorded", "review_record", "approval"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.repo.append_event("review-1", kind, {"decision": "approve"})
        for decision in ("approve", "approved", "deploy", ""):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.repo.append_human_review("review-1", "reviewer", decision, "a" * 64)
        with self.assertRaises(ValueError):
            self.repo.save_result("review-1", {"status": "approved"})
        self.assertEqual(self.repo.get_review("review-1")["human_reviews"], [])

    def test_stable_human_record_id_retries_return_original_row_once(self):
        self.repo.save_result("review-1", {"status": "waiting_human", "review_digest": "d" * 64})
        first = self.repo.append_human_review("review-1", "reviewer", "acknowledge", "d" * 64,
                                             "已阅读", record_id="hr-stable-1")
        repeated = Repository(self.path).append_human_review("review-1", "reviewer", "acknowledge", "d" * 64,
                                                            "已阅读", record_id="hr-stable-1")
        self.assertEqual(first, repeated)
        self.assertEqual(len(self.repo.get_review("review-1")["human_reviews"]), 1)

    def test_stable_human_record_id_rejects_different_content_or_review(self):
        self.repo.append_human_review("review-1", "reviewer", "acknowledge", "e" * 64,
                                     "原备注", record_id="hr-collision")
        self.repo.create_review("review-2", "CHG-002", "demo", "local")
        for changes in ({"review_id": "review-2"}, {"reviewer": "another"}, {"decision": "reject"},
                        {"proposed_digest": "f" * 64}, {"comment": "修改备注"}):
            fields = dict(review_id="review-1", reviewer="reviewer", decision="acknowledge",
                          proposed_digest="e" * 64, comment="原备注", record_id="hr-collision")
            fields.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.repo.append_human_review(**fields)
        stored = self.repo.get_review("review-1")["human_reviews"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["comment"], "原备注")

    def test_events_and_results_are_recursively_redacted(self):
        payload = {"api_key": "private-value", "nested": {"reasoning_content": "private-reasoning"},
                   "arguments": '{"password":"private-password","query":"ok"}',
                   "error": "redis://user:private-redis@localhost:6379/0"}
        self.repo.append_event("review-1", "model_call", payload)
        self.repo.save_result("review-1", {"status": "blocked", **payload})
        stored = self.repo.get_review("review-1")
        raw = json.dumps(stored)
        for secret in ("private-value", "private-reasoning", "private-password", "private-redis", "reasoning_content"):
            self.assertNotIn(secret, raw)
        arguments = json.loads(stored["events"][0]["payload"]["arguments"])
        self.assertEqual(arguments["query"], "ok")
        with self.assertRaises(ValueError):
            self.repo.append_event("review-1", "model_call", {"invalid": float("nan")})


if __name__ == "__main__":
    unittest.main()
