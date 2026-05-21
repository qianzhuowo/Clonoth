"""Built-in supervisor hook handler for scheduled memory dream tasks."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config


log = logging.getLogger(__name__)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "DreamHandler",
    "hook_points": [
        ("on_schedule_tick", "on_tick"),
    ],
    "priority": 100,
    # Why: dream reorganizes memory entries that knowledge_inject caches.
    # How: declare the dependency so loader ensures knowledge_inject loads first.
    # Purpose: fail clearly if knowledge_inject is missing.
    "requires": ["knowledge_inject"],
}


# Why: DreamHandler cannot depend on the scheduler module for cron matching.
# How: keep a local copy of the small 5-field matcher. Purpose: move dream logic
# into engine.builtin without creating an engine/supervisor import cycle.
def _match_field(field: str, value: int, max_val: int) -> bool:
    """Return whether one cron field matches the provided integer value."""
    field = field.strip()
    if field == "*":
        return True

    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False

    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                if int(lo) <= value <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue

    return False


def _cron_match(expr: str, dt: datetime) -> bool:
    """Return whether a 5-field cron expression matches a datetime."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, dt.minute, 59)
        and _match_field(hour, dt.hour, 23)
        and _match_field(day, dt.day, 31)
        and _match_field(month, dt.month, 12)
        and _match_field(weekday, dt.weekday(), 6)
    )


class DreamHandler:
    """Handle scheduled dream creation through injected supervisor callbacks.

    Why: dream was a supervisor-side handler that imported SupervisorState. How:
    read workspace, session counts, and task creation from ctx callbacks instead.
    Purpose: keep the schedule gate while allowing all built-ins to live under
    engine.builtin without supervisor imports.
    """

    name = "dream"

    def __init__(self) -> None:
        # Why: duplicate suppression belongs to the dream feature. How: keep the
        # last fired minute on the handler. Purpose: remove dream-specific fields
        # from SchedulerThread while preserving behavior.
        self._last_dream_fired: str = ""
        # Why: memory.dream.min_sessions compares current activity against the last
        # dream run. How: store that cursor here. Purpose: avoid scheduler-owned
        # dream state and keep the gate with the handler logic.
        self._dream_last_session_count: int = 0

    def on_tick(self, ctx: dict[str, Any]) -> None:
        """Run the dream schedule gate and create a system task when due."""
        if str(ctx.get("schedule_type") or "").strip() != "dream":
            return

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            return
        workspace_root = Path(workspace_root)
        now_value = ctx.get("now")
        now = now_value if isinstance(now_value, datetime) else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_key = str(ctx.get("now_key") or now.strftime("%Y-%m-%d %H:%M"))

        runtime_cfg = load_runtime_config(workspace_root)
        if not get_bool(runtime_cfg, "memory.dream.enabled", False):
            return

        cron_expr = get_str(runtime_cfg, "memory.dream.cron", "0 3 * * *").strip()
        if not cron_expr:
            return

        if self._last_dream_fired == now_key:
            return

        if not _cron_match(cron_expr, now):
            return

        session_count = ctx.get("session_count")
        current_session_count = int(session_count() or 0) if callable(session_count) else 0
        min_sessions = get_int(runtime_cfg, "memory.dream.min_sessions", 0, min_value=0, max_value=1000)
        if min_sessions > 0:
            new_sessions = current_session_count - self._dream_last_session_count
            if new_sessions < min_sessions:
                log.debug("[scheduler] dream skip: %s new sessions, need %s", new_sessions, min_sessions)
                return

        self._last_dream_fired = now_key

        node_id = get_str(runtime_cfg, "memory.dream.node_id", "system.dream").strip()
        conv_key = get_str(runtime_cfg, "memory.dream.conversation_key", "system:dream").strip()
        channel = conv_key.split(":", 1)[0] if ":" in conv_key else "system"
        text = f"[auto_dream] 定期记忆整理 ({now.strftime('%Y-%m-%d %H:%M UTC')})"
        msg_id = f"dream:{uuid.uuid4()}"

        create_task = ctx.get("create_task")
        if not callable(create_task):
            return

        try:
            # Why: this relocated handler cannot call supervisor inbound helpers.
            # How: request a system node task through the injected create_task
            # callback, using conversation_key and channel so supervisor can create
            # or reuse the session. Purpose: keep dream task creation cycle-free.
            create_task(
                channel=channel,
                conversation_key=conv_key,
                kind="node",
                node_id=node_id,
                input_data={
                    "instruction": text,
                    "context_ref": "",
                    "resume_data": {},
                    "use_context": False,
                    "_system_task": True,
                    "task_context": {
                        "conversation_key": conv_key,
                        "channel": channel,
                        "message_id": msg_id,
                        "entry_node_id": node_id,
                        "is_system_task": True,
                        "use_context": False,
                    },
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
            updated_session_count = int(session_count() or current_session_count) if callable(session_count) else current_session_count
            self._dream_last_session_count = updated_session_count
            log.info("[scheduler] dream fired -> conversation=%s", conv_key)
        except Exception as exc:
            log.warning("[scheduler] dream inject failed: %s", exc)
