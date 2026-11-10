from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from langgraph.types import Command
from langchain_core.runnables import RunnableLambda

from release_guard.analysis import PydanticAnalysis
from release_guard.config import Settings
from release_guard.workflow import build_workflow

ROOT = Path(__file__).resolve().parents[1]


def review(digest, decision="acknowledge"):
    return {"reviewer": "test-reviewer", "decision": decision, "comment": "已阅读风险，非生产审批。", "proposed_digest": digest}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_root = (ROOT / "tests" / ".workflow_tmp").resolve()
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.runtime = self.temp_root / uuid4().hex
        self.runtime.mkdir()
        self.config = {"configurable": {"thread_id": "review-thread"}}

    async def asyncTearDown(self):
        resolved = self.runtime.resolve()
        if resolved != self.temp_root and resolved.is_relative_to(self.temp_root):
            shutil.rmtree(resolved)

    def workflow(self, demo=True, **kwargs):
        return build_workflow(Settings(), self.runtime, ROOT / "data", demo=demo, **kwargs)

    async def test_real_mcp_sqlite_graph_always_pauses_and_blocker_cannot_be_acknowledged_away(self):
        async with self.workflow() as graph:
            result = await graph.ainvoke({"change_id": "risk-pool"}, self.config)
            self.assertTrue(result.get("__interrupt__"))
            paused = await graph.aget_state(self.config)
            state = paused.values
            self.assertEqual("waiting_human", state["status"])
            self.assertEqual(7, len(state["tool_records"]))
            self.assertTrue(all(row["ok"] for row in state["tool_records"]))
            self.assertTrue(any(row["severity"] == "blocker" for row in state["policy_findings"]))
            resumed = await graph.ainvoke(Command(resume=review(state["review_digest"])), self.config)
            self.assertEqual("blocked", resumed["decision"])
            self.assertFalse(resumed["deployment_approved"])
            self.assertEqual("acknowledge", resumed["review_record"]["decision"])
            self.assertFalse(resumed["review_record"]["production_approval"])
        self.assertTrue((self.runtime / "checkpoints.sqlite").exists())

    async def test_safe_still_requires_human_and_new_graph_resume_does_not_call_model(self):
        async with self.workflow() as graph:
            result = await graph.ainvoke({"change_id": "safe"}, self.config)
            self.assertTrue(result.get("__interrupt__"))
            state = (await graph.aget_state(self.config)).values
            digest = state["review_digest"]
            self.assertFalse(any(f["severity"] == "blocker" for f in state["policy_findings"]))
        with patch("release_guard.workflow.review_analysis", side_effect=AssertionError("resume must not repeat model")):
            async with self.workflow() as graph:
                result = await graph.ainvoke(Command(resume=review(digest)), self.config)
        self.assertEqual("review_acknowledged", result["status"])
        self.assertFalse(result["deployment_approved"])
        self.assertEqual(1, len(result["model_calls"]))

    async def test_api_history_correction_keeps_policy_blocker_and_human_pause(self):
        for corrected in (True, False):
            with self.subTest(corrected=corrected):
                calls = []

                async def respond(value):
                    context = json.loads(value["review_context"])
                    calls.append(context)
                    historical = next(row for row in context["evidence"] if row["kind"] == "runbook")
                    candidate = {"summary": "本报告仅提供建议，不批准发布。", "risks": [],
                                 "missing_evidence": [], "process_checks": ["复核容量验证记录。"]}
                    if len(calls) == 1 or not corrected:
                        candidate["risks"] = [{"title": "仅由历史提出的风险", "detail": "待核实。",
                            "evidence_ids": [historical["id"]], "verification": ["查阅当前资料。"]}]
                    return {"raw": None, "parsed": PydanticAnalysis.model_validate(candidate), "parsing_error": None}

                config = {"configurable": {"thread_id": "api-correction-" + str(corrected)}}
                with patch("release_guard.analysis.build_analysis_chain", return_value=RunnableLambda(respond)):
                    async with build_workflow(Settings(api_key="unit-placeholder"), self.runtime, ROOT / "data", demo=False) as graph:
                        paused = await graph.ainvoke({"change_id": "risk-pool"}, config)
                        self.assertTrue(paused.get("__interrupt__"))
                        self.assertEqual(7, len(paused["tool_records"]))
                        self.assertEqual(2, paused["model_calls"][0]["attempts"])
                        self.assertEqual(not corrected, bool(paused["quality_issues"]))
                        self.assertEqual(2, len(calls))
                        for key in ("scope", "evidence", "policy_findings"):
                            self.assertEqual(calls[0][key], calls[1][key])
                        self.assertTrue(any(row["severity"] == "blocker" for row in paused["policy_findings"]))
                        completed = await graph.ainvoke(Command(resume=review(paused["review_digest"])), config)
                        self.assertEqual("blocked" if corrected else "incomplete", completed["status"])
                        self.assertFalse(completed["deployment_approved"])
                        self.assertEqual(2, len(calls))

    async def test_missing_material_is_incomplete_and_also_pauses(self):
        async with self.workflow() as graph:
            result = await graph.ainvoke({"change_id": "missing"}, self.config)
            self.assertTrue(result.get("__interrupt__"))
            self.assertEqual("incomplete", result["status"])
            self.assertTrue(result["quality_issues"])
            self.assertEqual([], result["model_calls"])
            resumed = await graph.ainvoke(Command(resume=review(result["review_digest"])), self.config)
            self.assertEqual("incomplete", resumed["status"])

    async def test_same_thread_rejects_different_change(self):
        async with self.workflow() as graph:
            await graph.ainvoke({"change_id": "safe"}, self.config)
            with self.assertRaisesRegex(ValueError, "already bound"):
                await graph.ainvoke({"change_id": "risk-pool"}, self.config)

    async def test_saved_demo_cannot_resume_as_api_or_use_wrong_digest(self):
        async with self.workflow() as graph:
            result = await graph.ainvoke({"change_id": "safe"}, self.config)
            with self.assertRaisesRegex(ValueError, "digest"):
                await graph.ainvoke(Command(resume=review("0" * 64)), self.config)
        async with self.workflow(demo=False) as graph:
            with self.assertRaisesRegex(ValueError, "different execution mode"):
                await graph.ainvoke(Command(resume=review(result["review_digest"])), self.config)

    async def test_reject_and_request_changes_are_records_not_release_approval(self):
        for decision, outcome in (("reject", "rejected"), ("request_changes", "changes_requested")):
            config = {"configurable": {"thread_id": decision}}
            async with self.workflow() as graph:
                result = await graph.ainvoke({"change_id": "safe"}, config)
                resumed = await graph.ainvoke(Command(resume=review(result["review_digest"], decision)), config)
                self.assertEqual(outcome, resumed["status"])
                self.assertFalse(resumed["deployment_approved"])

    async def test_human_cannot_submit_approve_decision(self):
        async with self.workflow() as graph:
            result = await graph.ainvoke({"change_id": "safe"}, self.config)
            with self.assertRaisesRegex(ValueError, "never deployment approval"):
                await graph.ainvoke(Command(resume=review(result["review_digest"], "approve")), self.config)

    async def test_resume_in_new_process_uses_persisted_checkpoint(self):
        async with self.workflow() as graph:
            result = await graph.ainvoke({"change_id": "safe"}, self.config)
            digest = result["review_digest"]
        script = """
import asyncio,json,sys
from pathlib import Path
from unittest.mock import patch
from langgraph.types import Command
from release_guard.config import Settings
from release_guard.workflow import build_workflow
async def main():
    with patch('release_guard.workflow.review_analysis',side_effect=AssertionError('no repeated model')):
        async with build_workflow(Settings(),Path(sys.argv[1]),Path(sys.argv[2]),demo=True) as graph:
            result=await graph.ainvoke(Command(resume={'reviewer':'new-process','decision':'acknowledge','comment':'review only','proposed_digest':sys.argv[3]}),{'configurable':{'thread_id':'review-thread'}})
            print(json.dumps({'status':result['status'],'model_calls':len(result['model_calls']),'reviewer':result['review_record']['reviewer']}))
asyncio.run(main())
"""
        # The Windows asyncio subprocess transport uses named pipes, which
        # this sandbox may forbid. Standard subprocess uses anonymous pipes.
        process = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-c", script, str(self.runtime), str(ROOT / "data"), digest],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(0, process.returncode, process.stderr)
        resumed = json.loads(process.stdout)
        self.assertEqual("review_acknowledged", resumed["status"])
        self.assertEqual("new-process", resumed["reviewer"])
        self.assertEqual(1, resumed["model_calls"])


if __name__ == "__main__":
    unittest.main()
