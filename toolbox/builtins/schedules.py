"""Schedule management: create_schedule, list_schedules, delete_schedule."""
from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


async def create_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return {"ok": False, "error": "empty schedule id"}
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return {"ok": False, "error": "invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed"}

    cron_expr = str(args.get("cron") or "").strip()
    if not cron_expr:
        return {"ok": False, "error": "empty cron expression"}
    parts = cron_expr.split()
    if len(parts) != 5:
        return {"ok": False, "error": "cron must be 5 fields: minute hour day month weekday"}

    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}

    conv_key = str(args.get("conversation_key") or f"scheduler:{sid}").strip()
    workflow_id = str(args.get("workflow_id") or "").strip()
    enabled = bool(args.get("enabled", True))
    once = bool(args.get("once", False))

    _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "schedule_id": sid})
    if err is not None:
        return err

    schedules = load_schedules(ctx.workspace_root)
    entry = {
        "id": sid,
        "cron": cron_expr,
        "text": text,
        "conversation_key": conv_key,
        "workflow_id": workflow_id,
        "enabled": enabled,
        "once": once,
    }

    replaced = False
    for i, s in enumerate(schedules):
        if str(s.get("id") or "").strip() == sid:
            schedules[i] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)

    save_schedules(ctx.workspace_root, schedules)
    return {"ok": True, "schedule": entry, "replaced": replaced}


async def list_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List all scheduled tasks."""
    from supervisor.scheduler import load_schedules

    schedules = load_schedules(ctx.workspace_root)
    return {"ok": True, "schedules": schedules}


async def delete_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return {"ok": False, "error": "empty schedule id"}

    _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "delete_schedule": sid})
    if err is not None:
        return err

    schedules = load_schedules(ctx.workspace_root)
    before = len(schedules)
    schedules = [s for s in schedules if str(s.get("id") or "").strip() != sid]
    if len(schedules) == before:
        return {"ok": False, "error": f"schedule not found: {sid}"}

    save_schedules(ctx.workspace_root, schedules)
    return {"ok": True, "deleted": True, "id": sid}
