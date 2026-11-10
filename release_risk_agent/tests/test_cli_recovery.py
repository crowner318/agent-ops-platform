import asyncio
from contextlib import asynccontextmanager, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from release_guard.cli import run_review
from release_guard.coordination import LocalCoordinator
from release_guard.storage import Repository
from release_guard.workflow import make_review_digest


class FakeGraph:
    def __init__(self, before, after):
        self.before, self.after, self.calls = before, after, []
        self.current = before

    async def aget_state(self, config):
        return self.current

    async def ainvoke(self, value, config):
        self.calls.append(value)
        self.current = self.after


def snapshot(state, next_nodes=(), interrupt=False):
    return SimpleNamespace(values=deepcopy(state), next=next_nodes, tasks=(), interrupts=("review",) if interrupt else ())


class CLIRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("release-cli-recovery-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.repo = Repository(self.root / "reviews.sqlite3")
        self.repo.create_review("rev-one", "safe", "demo", "local")
        self.coordinator = LocalCoordinator()
        self.addCleanup(self.coordinator.close)
        self.args = SimpleNamespace(runtime_dir=self.root, data_dir=Path(__file__).resolve().parents[1] / "data", change_id="safe")

    def cleanup(self):
        checked = self.root.resolve()
        assert checked.parent == Path(tempfile.gettempdir()).resolve() and checked.name.startswith("release-cli-recovery-")
        shutil.rmtree(checked)

    def run_graph(self, graph, resume=None):
        @asynccontextmanager
        async def factory(*args, **kwargs):
            yield graph
        with patch("release_guard.workflow.build_workflow", factory), redirect_stdout(io.StringIO()):
            return asyncio.run(run_review(self.args, self.repo, self.coordinator, "rev-one", resume=resume, demo=True))

    def test_existing_checkpoint_without_human_interrupt_resumes_automatic_nodes(self):
        before = snapshot({"change_id": "safe", "status": "incomplete"}, ("policy",))
        after = snapshot({"change_id": "safe", "status": "waiting_human", "review_digest": "a"*64}, ("human_review",), True)
        graph = FakeGraph(before, after)
        self.assertEqual(0, self.run_graph(graph))
        self.assertEqual([None], graph.calls)
        self.assertEqual("waiting_human", self.repo.get_review("rev-one")["status"])

    def test_real_human_pause_is_not_reexecuted_on_redelivery(self):
        paused = snapshot({"change_id": "safe", "status": "waiting_human", "review_digest": "a"*64}, ("human_review",), True)
        graph = FakeGraph(paused, paused)
        self.assertEqual(0, self.run_graph(graph))
        self.assertEqual([], graph.calls)

    def test_demo_does_not_read_user_model_settings(self):
        paused = snapshot({"change_id": "safe", "status": "waiting_human", "review_digest": "a"*64}, ("human_review",), True)
        graph = FakeGraph(paused, paused)
        with patch("release_guard.cli.load_settings", side_effect=AssertionError("demo must not read user settings")):
            self.assertEqual(0, self.run_graph(graph))

    def test_committed_graph_human_record_reconciles_and_retries_without_duplicates(self):
        record = {"reviewer": "tester", "decision": "acknowledge", "comment": "已复核", "proposed_digest": "a"*64,
                  "reviewed_at": "2026-10-30T10:00:00+00:00", "production_approval": False}
        done = snapshot({"change_id": "safe", "status": "review_acknowledged", "review_digest": "a"*64, "review_record": record})
        graph = FakeGraph(done, done)
        payload = {key: record[key] for key in ("reviewer", "decision", "comment")}
        self.assertEqual(0, self.run_graph(graph, dict(payload)))
        self.assertEqual(0, self.run_graph(graph, dict(payload)))
        self.assertEqual([], graph.calls)
        self.assertEqual(1, len(self.repo.get_review("rev-one")["human_reviews"]))
        with self.assertRaises(ValueError):
            self.run_graph(graph, dict(payload, decision="reject"))

    def test_wrong_business_binding_cannot_continue_checkpoint(self):
        self.args.change_id = "risk-pool"
        graph = FakeGraph(snapshot({}), snapshot({}))
        with self.assertRaises(ValueError):
            self.run_graph(graph)
        self.assertEqual([], graph.calls)

    def test_full_evidence_body_is_bound_to_review_digest(self):
        before = {"scope": {}, "evidence": [{"id": "same-id", "content": {"pool_after": 5}}]}
        after = deepcopy(before)
        after["evidence"][0]["content"]["pool_after"] = 50
        self.assertNotEqual(make_review_digest(before), make_review_digest(after))


if __name__ == "__main__":
    unittest.main()
