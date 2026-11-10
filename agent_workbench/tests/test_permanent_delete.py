"""Permanent deletion is explicit, scoped, and cannot resurrect on rescanning."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_service as support


class PermanentDeleteTests(unittest.TestCase):
    setUp = support.ServiceTests.setUp
    tearDown = support.ServiceTests.tearDown
    wait = support.ServiceTests.wait

    def test_report_requires_exact_confirmation_and_trash_then_remains_deleted(self):
        task = self.wait(self.service.create_task({"agent": "ops", "mode": "demo", "params": {}})["id"])
        with self.assertRaises(RuntimeError): self.service.purge_task(task["id"], {"confirmation": task["id"]})
        self.service.trash_task(task["id"])
        with self.assertRaises(ValueError): self.service.purge_task(task["id"], {"confirmation": "wrong"})
        path = self.service._row(task["id"])["report_path"]
        result = self.service.purge_task(task["id"], {"confirmation": task["id"]})
        self.assertTrue(result["permanently_deleted"])
        self.assertEqual([], self.service.list_tasks())
        self.assertEqual([], self.service.list_trash())
        with self.assertRaises(KeyError): self.service.restore_task(task["id"])
        # Even an old source file recreated outside the workbench stays hidden.
        from pathlib import Path
        Path(path).write_text(json.dumps({"session_id": task["id"], "answer": "old copy", "complete": True}), encoding="utf-8")
        self.assertEqual([], self.service.list_tasks())

    def test_case_delete_removes_practices_and_does_not_return_after_restart(self):
        db = self.service.ops_runtime / "memory.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.executescript("CREATE TABLE cases(id TEXT PRIMARY KEY,session_id TEXT,service TEXT,environment TEXT,summary TEXT,verified INTEGER,created_at TEXT);CREATE TABLE practices(id TEXT,case_id TEXT);")
            connection.execute("INSERT INTO cases VALUES('case-own','test','billing-api','prod-eu','user case',1,'2026-11-09')")
            connection.execute("INSERT INTO practices VALUES('practice-own','case-own')")
        self.assertEqual(1, len(self.service.list_cases()))
        with self.assertRaises(ValueError): self.service.purge_case("case-own", {"confirmation": "wrong"})
        self.service.purge_case("case-own", {"confirmation": "case-own"})
        self.assertEqual([], self.service.list_cases())
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM practices").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM cases").fetchone()[0])
        self.service.close(); self.service = support.FakeService(self.config)
        self.assertEqual([], self.service.list_cases())

    def test_legacy_status_does_not_pretend_java_or_redis_are_connected(self):
        value = self.service.storage_status()
        self.assertEqual("legacy", value["mode"])
        self.assertFalse(value["redis"]["available"])
        self.assertFalse(value["java"]["available"])
        self.assertEqual("files", value["reports"])

    def test_report_cleanup_failure_can_be_retried_after_tombstone(self):
        task = self.wait(self.service.create_task({"agent": "ops", "mode": "demo", "params": {}})["id"])
        self.service.trash_task(task["id"])
        input_dir = self.service.runtime_dir / "inputs" / task["id"]
        input_dir.mkdir(parents=True)
        (input_dir / "demo_data.json").write_text('{}')
        with patch("workbench.service.shutil.rmtree", side_effect=OSError("temporary lock")):
            with self.assertRaises(OSError): self.service.purge_task(task["id"], {"confirmation": task["id"]})
        self.assertEqual([], self.service.list_tasks())
        self.assertTrue(self.service.purge_task(task["id"], {"confirmation": task["id"]})["permanently_deleted"])
        self.assertFalse(input_dir.exists())

    def test_case_cleanup_failure_can_be_retried_without_restoring_case(self):
        db = self.service.ops_runtime / "memory.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("CREATE TABLE cases(id TEXT,session_id TEXT,service TEXT,environment TEXT,summary TEXT,verified INTEGER,created_at TEXT)")
            connection.execute("INSERT INTO cases VALUES('case-retry','test','s','e','retry',1,'2026-11-09')")
        with patch.object(self.service, "_erase_memory_case", side_effect=sqlite3.OperationalError("temporary lock")):
            with self.assertRaises(sqlite3.OperationalError): self.service.purge_case("case-retry", {"confirmation": "case-retry"})
        self.assertEqual([], self.service.list_cases())
        self.assertTrue(self.service.purge_case("case-retry", {"confirmation": "case-retry"})["permanently_deleted"])
        self.assertEqual([], self.service._memory_cases())

    def test_java_migration_copies_case_deletion_tombstones_first(self):
        class Store:
            def __init__(self): self.deleted=[]
            def jobs(self): return []
            def import_case_tombstone(self,key): self.deleted.append(key)
        with self.service._connect() as connection:
            connection.execute("INSERT INTO case_tombstones VALUES('case-old-deleted')")
        store=Store()
        self.service.store=store
        try:
            self.service._import_legacy_jobs()
            self.assertEqual(['case-old-deleted'],store.deleted)
        finally:
            self.service.store=None
