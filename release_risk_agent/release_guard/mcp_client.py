"""LangChain 1.4 MCPAdapter with explicit inprocess/stdio MCP transport."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from langchain.mcp import MCPAdapter

from .evidence import EvidenceStore, MAX_RESULT_CHARS, tool_names


class MCPTools:
    tool_names = tool_names

    def __init__(self, data_dir: Path, coordinator=None, timeout: float = 20):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("MCP timeout must be between 0 and 120 seconds")
        self.data_dir, self.coordinator, self.timeout = Path(data_dir).resolve(), coordinator, timeout
        self.store = EvidenceStore(self.data_dir)
        self.mcp_transport = os.environ.get("RELEASE_MCP_TRANSPORT", "inprocess").strip().lower()
        if self.mcp_transport not in {"inprocess", "stdio"}:
            raise ValueError("RELEASE_MCP_TRANSPORT must be inprocess or stdio")
        self._adapter = None
        self._tools = {}

    async def __aenter__(self):
        if self.mcp_transport == "stdio":
            transport = StdioTransport(command=sys.executable,
                args=["-m", "release_guard.mcp_server", "--data-dir", str(self.data_dir)],
                cwd=str(Path(__file__).resolve().parents[1]), keep_alive=False)
        else:
            from .mcp_server import create_server
            transport = create_server(self.data_dir)
        self._adapter = MCPAdapter(Client(transport, timeout=self.timeout, init_timeout=self.timeout))
        await asyncio.wait_for(self._adapter.__aenter__(), self.timeout)
        try:
            tools = await asyncio.wait_for(self._adapter.list_tools(), self.timeout)
            self._tools = {tool.name: tool for tool in tools}
            if set(self._tools) != set(tool_names):
                raise ValueError("unexpected MCP server tool capabilities")
            return self
        except BaseException:
            await self._adapter.__aexit__(*sys.exc_info())
            self._adapter = None
            raise

    async def __aexit__(self, *args):
        if self._adapter is not None:
            await self._adapter.__aexit__(*args)
        self._adapter = None
        self._tools = {}

    async def list_tools(self):
        if self._adapter is None:
            raise RuntimeError("MCPTools must be used inside async with")
        return list(self._tools.values())

    async def _coordinator(self, method: str, *args, **kwargs):
        function = getattr(self.coordinator, method)
        if inspect.iscoroutinefunction(function):
            return await function(*args, **kwargs)
        result = await asyncio.to_thread(function, *args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def call(self, name: str, arguments: dict) -> dict:
        scope = {}
        try:
            self.store.validate_arguments(name, arguments)
            scope = self.store.scope(arguments["change_id"])
            if self._adapter is None:
                raise RuntimeError("MCPTools must be used inside async with")
            key_data = {"data_hash": self.store.content_hash, "scope": scope, "tool": name,
                        "args": arguments, "mcp_transport": self.mcp_transport}
            key = "release-mcp:" + hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if self.coordinator is not None:
                cached = await self._coordinator("get_cache", key)
                if cached is not None:
                    result = deepcopy(cached)
                    result["cache_hit"] = True
                    result["mcp_transport"] = self.mcp_transport
                    return result
            # Invoke the actual LangChain BaseTool produced by MCPAdapter.
            blocks = await asyncio.wait_for(self._tools[name].ainvoke(arguments), self.timeout)
            if not isinstance(blocks, list):
                raise ValueError("MCP tool response must contain text blocks")
            texts = [block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
            raw = "\n".join(texts)
            if len(raw) > MAX_RESULT_CHARS:
                raise ValueError("MCP tool response exceeds size limit")
            result = json.loads(raw)
            if (not isinstance(result, dict) or not isinstance(result.get("ok"), bool)
                    or result.get("scope") != scope or not isinstance(result.get("evidence"), list)
                    or "data" not in result):
                raise ValueError("MCP returned an invalid scoped evidence envelope")
            if result.get("ok") and (result.get("data") or {}).get("snapshot_hash") != self.store.content_hash:
                raise ValueError("MCP server data changed during snapshot initialization; reopen the client")
            for row in result["evidence"]:
                if (not isinstance(row, dict) or any(row.get(key) != scope[key] for key in ("change_id", "service", "environment"))
                        or not isinstance(row.get("id"), str) or not isinstance(row.get("content"), dict)):
                    raise ValueError("MCP evidence does not match the bound release")
            if self.coordinator is not None and result["ok"]:
                await self._coordinator("set_cache", key, deepcopy(result), ttl=300)
            result["cache_hit"] = False
            result["mcp_transport"] = self.mcp_transport
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"ok": False, "scope": scope, "evidence": [], "data": None,
                    "error": f"{type(exc).__name__}: {exc}", "cache_hit": False,
                    "mcp_transport": self.mcp_transport}
