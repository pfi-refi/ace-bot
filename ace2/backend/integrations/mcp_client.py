"""
MCP client for Ace 2.0 — in-process bridge to a self-hosted MCP server
(google_workspace_mcp first), per the 2026-07-18 design spec.

WHY in-process (not Anthropic's server-side MCP connector): the connector
executes tools on Anthropic's side, which would bypass Ace's harness — the WS
orb events, mid-turn confirmations, and the confirm-before-send guard. Here the
MCP server's tools are folded into Ace's OWN tool loop: same interception, same
safety, just a different executor. PII never leaves our infra except to Google.

DORMANT BY DEFAULT: everything is gated on MCP_SERVER_URL. Unset (today) →
tool_schemas() returns [] and nothing changes anywhere. The activation session
(Brady's one-time Google consent + a private workspace-mcp service + setting
MCP_SERVER_URL) flips it on with zero code changes.

Schemas are fetched ONCE and cached for the process lifetime — the tool list
must stay byte-stable across turns for prompt caching.
"""

import asyncio
import logging
import os

logger = logging.getLogger("ace2.mcp")

try:  # the `mcp` SDK is only needed once MCP is activated; never break boot without it
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    _SDK = True
except Exception:  # pragma: no cover
    _SDK = False

_lock = asyncio.Lock()
_schemas: list = []          # Anthropic-shaped tool schemas (cached once)
_names: set = set()
_loaded = False

# Belt-and-suspenders: never register write/destructive MCP tools even if the
# server exposes them (server should be --read-only anyway; see spec §security).
_DENY_HINTS = ("send", "delete", "remove", "create", "update", "share", "move", "trash", "modify", "write")


def _url() -> str:
    return os.environ.get("MCP_SERVER_URL", "").strip()


def enabled() -> bool:
    return bool(_url()) and _SDK


async def _load():
    """Fetch tool schemas from the MCP server once. Best-effort — failure = stay dormant."""
    global _loaded
    async with _lock:
        if _loaded:
            return
        try:
            async with streamablehttp_client(_url()) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
            for t in result.tools:
                name = t.name
                if any(h in name.lower() for h in _DENY_HINTS):
                    continue  # read-only surface for now; writes stay on Ace's own guarded tools
                _schemas.append({
                    "name": f"mcp_{name}",
                    "description": (t.description or name)[:900],
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                })
                _names.add(f"mcp_{name}")
            logger.info("MCP: loaded %d read tools from %s", len(_schemas), _url())
        except Exception as e:
            logger.warning("MCP: load failed (%s) — staying dormant this process", e)
        finally:
            _loaded = True


async def tool_schemas() -> list:
    """Anthropic tool schemas for the MCP server's (read) tools; [] when dormant."""
    if not enabled():
        return []
    if not _loaded:
        await _load()
    return _schemas


def is_mcp_tool(name: str) -> bool:
    return name in _names


async def call(name: str, arguments: dict) -> str:
    """Execute one MCP tool call → flattened text. Never raises into the turn."""
    if not enabled() or not is_mcp_tool(name):
        return f"⚠️ MCP tool unavailable: {name}"
    real = name[len("mcp_"):]
    try:
        async with streamablehttp_client(_url()) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(real, arguments or {})
        parts = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        out = "\n".join(parts).strip() or "(no content returned)"
        return out[:24000]
    except Exception as e:
        logger.error("MCP call %s failed: %s", real, e)
        return f"⚠️ MCP {real} failed: {e}"
