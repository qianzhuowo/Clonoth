"""MCP client management tools."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext
from .. import mcp_runtime


async def create_or_update_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        spec = mcp_runtime.upsert_client(ctx.workspace_root, args)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "client": spec, "path": "data/mcp_clients.yaml"}


async def list_mcp_clients(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        clients = mcp_runtime.list_clients(ctx.workspace_root)
        return {"ok": True, "clients": clients}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def delete_mcp_client(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    client_id = str(args.get("id", "")).strip()
    if not client_id:
        return {"ok": False, "error": "empty client id"}
    try:
        ok = mcp_runtime.delete_client(ctx.workspace_root, client_id)
        if not ok:
            return {"ok": False, "error": f"client not found: {client_id}"}
        return {"ok": True, "deleted": True, "id": client_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
