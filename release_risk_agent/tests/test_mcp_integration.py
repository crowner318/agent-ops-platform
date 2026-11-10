"""Real MCPAdapter protocol discovery/calls; stdio is opt-in on permitted hosts."""
import asyncio
from copy import deepcopy
from pathlib import Path
import os
import unittest
from unittest.mock import patch

from langchain_core.tools import BaseTool

from release_guard.mcp_client import MCPTools, tool_names
from release_guard.policy import evaluate

ROOT = Path(__file__).resolve().parents[1]


class Cache:
    def __init__(self): self.values = {}; self.hits = 0
    def get_cache(self, key):
        if key in self.values: self.hits += 1
        return deepcopy(self.values.get(key))
    def set_cache(self, key, value, ttl=300): self.values[key] = deepcopy(value)


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_adapter_parallel_calls_scope_policy_and_cache(self):
        cache = Cache()
        async with MCPTools(ROOT / "data", coordinator=cache, timeout=30) as mcp:
            tools = await mcp.list_tools()
            self.assertEqual(set(tool_names), {t.name for t in tools})
            self.assertTrue(all(isinstance(tool, BaseTool) for tool in tools))
            results = await asyncio.gather(*(mcp.call(name, dict(change_id="risk-pool", **({"query": "pool capacity"} if name.startswith("search_") else {}))) for name in tool_names))
            self.assertTrue(all(result["ok"] for result in results), results)
            self.assertTrue(all(result["mcp_transport"] == mcp.mcp_transport for result in results))
            self.assertTrue(all(result["scope"]["change_id"] == "risk-pool" for result in results))
            ids = [row["id"] for result in results for row in result["evidence"]]
            self.assertEqual(len(ids), len(set(ids)))
            findings = evaluate([row for result in results for row in result["evidence"]])
            self.assertIn("DEPENDENCY_BUDGET_EXCEEDED", {f["code"] for f in findings})
            second = await mcp.call("get_release_snapshot", {"change_id": "risk-pool"})
            self.assertTrue(second["cache_hit"])
            second["evidence"][0]["content"]["version"] = "tampered"
            third = await mcp.call("get_release_snapshot", {"change_id": "risk-pool"})
            self.assertNotEqual("tampered", third["evidence"][0]["content"]["version"])
            safe = await mcp.call("get_release_snapshot", {"change_id": "safe"})
            self.assertFalse(safe["cache_hit"])
            self.assertEqual("safe", safe["scope"]["change_id"])
            for name, arguments in [("deploy_release", {"change_id": "safe"}), ("get_ci_checks", {"change_id": "safe", "environment": "production"}), ("search_runbooks", {"change_id": "safe", "query": "x" * 2001})]:
                self.assertFalse((await mcp.call(name, arguments))["ok"])
            missing = await mcp.call("get_ci_checks", {"change_id": "missing"})
            self.assertTrue(missing["ok"])
            self.assertEqual([], missing["evidence"])

    @unittest.skipUnless(os.environ.get("RELEASE_MCP_TRANSPORT") == "stdio", "explicit stdio mode requires subprocess pipe permissions")
    async def test_explicit_stdio_transport(self):
        async with MCPTools(ROOT / "data", timeout=30) as mcp:
            self.assertEqual("stdio", mcp.mcp_transport)
            self.assertTrue((await mcp.call("get_ci_checks", {"change_id": "safe"}))["ok"])


if __name__ == "__main__": unittest.main()
