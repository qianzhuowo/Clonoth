"""Task management: cancel_active_tasks, list_active_tasks."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext


async def cancel_active_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Cancel all active downstream tasks in the current session."""
    try:
        # [Fork/Merge 2026-05-17] Why: this tool is session-scoped, but a caller
        # may be executing inside a temporary entry branch. How: use ToolContext's
        # parent-first route session. Purpose: sibling branch tasks under the same
        # user conversation can be cancelled together.
        route_session_id = ctx.route_session_id()
        r = await ctx.http.post(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/cancel_active_tasks",
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
        # [Fork/Merge 2026-05-17] Why: context usage events are emitted to the
        # parent route session, not to branch storage sessions. How: query the
        # same parent-first route used by tool events. Purpose: avoid returning an
        # empty branch estimate while the parent has the real usage record.
        route_session_id = ctx.route_session_id()
        r = await ctx.http.get(
            f"{ctx.supervisor_url}/v1/sessions/{route_session_id}/context_window",
        )
        if r.status_code >= 400:
            return {"ok": False, "error": r.text}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
