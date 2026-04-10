"""Task management: cancel_active_tasks, list_active_tasks."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext


async def cancel_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Cancel all active downstream tasks in the current session."""
    try:
        r = await ctx.http.post(
            f"{ctx.supervisor_url}/v1/sessions/{ctx.session_id}/cancel_active_tasks",
            params={"exclude_task_id": ctx.task_id},
        )
        if r.status_code >= 400:
            return {"ok": False, "error": r.text}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def list_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List active tasks in the current session."""
    return {"ok": True, "note": "活跃任务信息已在系统上下文中注入，无需额外查询。"}


async def get_context_window(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get current context window token usage for the session."""
    try:
        r = await ctx.http.get(
            f"{ctx.supervisor_url}/v1/sessions/{ctx.session_id}/context_window",
        )
        if r.status_code >= 400:
            return {"ok": False, "error": r.text}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
