"""Built-in supervisor hook handler for automatic memory extraction."""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config


log = logging.getLogger(__name__)


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "MemoryExtractHandler",
    "hook_points": [
        ("on_entry_task_complete", "on_task_complete"),
        ("on_inbound_message", "on_inbound"),
    ],
    "priority": 100,
}


class MemoryExtractHandler:
    """Handle idle automatic memory extraction through injected callbacks.

    Why: this handler now lives under engine.builtin and therefore cannot import
    supervisor internals. How: every supervisor operation is read from the hook
    context as a callback or plain value. Purpose: keep memory extraction behavior
    while removing the old supervisor -> handler -> supervisor dependency loop.
    """

    name = "memory_extract"

    def __init__(self) -> None:
        # Why: the extraction cursor is handler-specific state. How: store it on
        # the handler instead of SupervisorState. Purpose: avoid polluting the
        # supervisor core with memory feature internals.
        self._memory_extract_msg_counts: dict[str, int] = {}
        # Why: automatic extraction waits for user idleness. How: keep one daemon
        # Timer per session and replace it on later qualifying finishes. Purpose:
        # avoid extracting memory while the user is still sending follow-ups.
        self._memory_extract_timers: dict[str, threading.Timer] = {}
        # Why: the cursor must move only if the idle timer fires. How: keep the
        # prepared transcript until the timer callback commits it. Purpose:
        # cancelled idle windows can be retried by the next entry-node finish.
        self._memory_extract_pending: dict[str, dict[str, Any]] = {}

    def on_task_complete(self, ctx: dict[str, Any]) -> None:
        """Schedule memory extraction after a qualifying entry task finishes."""
        task = ctx.get("task")
        if task is None:
            return
        self._maybe_trigger_memory_extract_locked(ctx=ctx, task=task)

    def on_inbound(self, ctx: dict[str, Any]) -> None:
        """Cancel pending idle extraction when a new inbound message arrives."""
        session_id = str(ctx.get("session_id") or "").strip()
        if not session_id:
            return
        lock = ctx.get("acquire_lock")
        if lock is None:
            self._cancel_memory_extract_idle_locked(session_id)
            return
        # Why: inbound cancellation touches the same handler dictionaries as the
        # timer callback. How: use the injected re-entrant supervisor lock when it
        # is available. Purpose: keep cancellation serialized without importing
        # SupervisorState or reaching into private attributes.
        with lock:
            self._cancel_memory_extract_idle_locked(session_id)

    def _maybe_trigger_memory_extract_locked(self, *, ctx: dict[str, Any], task: Any) -> None:
        """Entry-node finish gate for automatic memory extraction."""
        # Why: only successful entry-node finishes represent complete conversation
        # turns. How: keep the old action/kind/system-task gates using only task
        # values. Purpose: prevent child tasks or internal system tasks from
        # recursively triggering extraction.
        act = str((getattr(task, "result", None) or {}).get("action") or "").strip()
        if act != "finish" or not _task_kind_is_node(task):
            return
        task_input = getattr(task, "input", {}) or {}
        if task_input.get("_system_task"):
            return

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            return
        workspace_root = Path(workspace_root)
        runtime_cfg = load_runtime_config(workspace_root)
        entry_node_id = get_str(runtime_cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip()
        if getattr(task, "node_id", None) != entry_node_id:
            return
        if not get_bool(runtime_cfg, "memory.auto_extract.enabled", False):
            return

        # Why: a task that explicitly saved memory has already handled the turn.
        # How: preserve the old save_memory tool-name mutual exclusion. Purpose:
        # avoid duplicate memory entries from automatic extraction.
        tool_names = (getattr(task, "result", None) or {}).get("_tool_names") or []
        if "save_memory" in tool_names:
            return

        session_messages = ctx.get("session_messages")
        if not callable(session_messages):
            return
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not session_id:
            return
        msgs = session_messages(session_id, limit=0)
        non_system = [m for m in msgs if m.get("role") != "system"]
        current_count = len(non_system)

        min_messages = get_int(runtime_cfg, "memory.auto_extract.min_messages", 4, min_value=2, max_value=100)
        if current_count < min_messages:
            return

        min_increment = get_int(runtime_cfg, "memory.auto_extract.min_increment", 10, min_value=1, max_value=100)
        last_count = self._memory_extract_msg_counts.get(session_id, 0)
        if current_count - last_count < min_increment:
            return

        transcript = _conversation_store_transcript(workspace_root, session_id, last_count, current_count)
        if not transcript:
            # Why: ConversationStore can be absent in tests or replay-only sessions.
            # How: delegate formatting to the injected callback when available.
            # Purpose: keep fallback transcript generation owned by supervisor code.
            format_transcript = ctx.get("format_transcript")
            if callable(format_transcript):
                transcript = str(format_transcript(msgs) or "")
            else:
                transcript = _format_transcript_for_extract(msgs)

        if not transcript.strip():
            return

        # [2026-04-26] P4b pre-injection stays removed: large memory lists polluted
        # the main session history and could exceed context budgets. The extractor
        # node prompt already contains duplicate-prevention instructions.
        idle_delay = get_int(runtime_cfg, "memory.auto_extract.idle_delay_sec", 15, min_value=5, max_value=120)
        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        pending_extract = {
            "session_id": session_id,
            "session_generation": int(getattr(task, "session_generation", 0) or 0),
            "transcript": transcript,
            "current_count": current_count,
            "extractor_node": extractor_node,
            "kind": getattr(task, "kind", "node"),
        }

        # Why: extraction should happen only after the user stays idle. How: keep
        # the prepared transcript in a per-session pending slot and restart that
        # session's Timer on each qualifying finish. Purpose: if a new inbound
        # arrives before expiry, on_inbound can cancel the Timer without advancing
        # the extraction cursor or creating the system task.
        old_timer = self._memory_extract_timers.pop(session_id, None)
        if old_timer is not None:
            old_timer.cancel()
        self._memory_extract_pending[session_id] = pending_extract
        timer = threading.Timer(idle_delay, self._fire_memory_extract_idle, args=[dict(ctx), session_id])
        timer.daemon = True
        self._memory_extract_timers[session_id] = timer
        timer.start()
        log.debug("Scheduled idle memory extract timer for session %s after %s seconds", session_id, idle_delay)

    def _cancel_memory_extract_idle_locked(self, session_id: str) -> None:
        """Cancel a pending idle memory extraction for one session."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        timer = self._memory_extract_timers.pop(sid, None)
        if timer is not None:
            timer.cancel()
        pending = self._memory_extract_pending.pop(sid, None)
        if timer is not None or pending is not None:
            log.debug("Cancelled idle memory extract timer for session %s (new inbound)", sid)

    def _fire_memory_extract_idle(self, ctx: dict[str, Any], session_id: str) -> None:
        """Fire a pending automatic memory extraction after the idle window."""
        sid = str(session_id or "").strip()
        if not sid:
            return

        lock = ctx.get("acquire_lock")
        if lock is None:
            return
        # Why: threading.Timer runs outside the supervisor request/event thread.
        # How: take the injected lock before touching pending state or creating
        # tasks. Purpose: serialize timer callbacks with inbound handling and task
        # routing while keeping handler-owned dictionaries consistent.
        with lock:
            pending_extract = self._memory_extract_pending.pop(sid, None)
            self._memory_extract_timers.pop(sid, None)
            if not isinstance(pending_extract, dict):
                return

            transcript = str(pending_extract.get("transcript") or "")
            if not transcript.strip():
                return
            current_count = _safe_int(pending_extract.get("current_count"), 0)
            session_generation = _safe_int(pending_extract.get("session_generation"), 0)
            if session_generation <= 0:
                current_generation = ctx.get("current_session_generation")
                session_generation = int(current_generation(sid) or 1) if callable(current_generation) else 1
            extractor_node = str(pending_extract.get("extractor_node") or "system.memory_extractor").strip()
            extractor_node = extractor_node or "system.memory_extractor"

            # Why: a cancelled idle window must not move the cursor. How: update
            # the cursor only inside the timer callback after pending data is
            # popped. Purpose: the next finish can retry the same message range if
            # a user message cancelled the previous idle timer.
            self._memory_extract_msg_counts[sid] = current_count

            create_task = ctx.get("create_task")
            if not callable(create_task):
                return
            # [2026-04-26] child_session_id isolation prevents the system task's
            # instruction from being written into the main session JSONL history.
            child_sid = f"child_{uuid.uuid4().hex[:12]}"
            create_task(
                session_id=sid,
                session_generation=session_generation,
                kind=pending_extract.get("kind") or "node",
                node_id=extractor_node,
                input_data={
                    "instruction": transcript,
                    "child_session_id": child_sid,
                    "_system_task": True,
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )


def _task_kind_is_node(task: Any) -> bool:
    """Return whether a task is a node task without depending on task enums."""
    # Why: importing the supervisor task enum would recreate the cycle this move is
    # meant to remove. How: compare the enum value when present, otherwise compare
    # the plain string. Purpose: keep the gate compatible with real tasks and
    # lightweight tests.
    kind = getattr(task, "kind", "")
    value = getattr(kind, "value", kind)
    return str(value) == "node"


def _conversation_store_transcript(workspace_root: Path, session_id: str, last_count: int, current_count: int) -> str:
    """Read the exact new non-system message range from ConversationStore."""
    # Why: extracting only the current task can skip messages from intervening
    # turns. How: read ConversationStore and slice the non-system range between
    # the last committed count and current count. Purpose: preserve the fixed
    # cursor behavior from the old router implementation.
    try:
        from engine.conversation_store import ConversationStore

        store = ConversationStore(workspace_root / "data" / "conversations")
        all_msgs = store.load(session_id)
        non_sys = [m for m in all_msgs if m.role != "system"]
        range_msgs = non_sys[last_count:current_count]
    except Exception:
        return ""

    parts: list[str] = []
    for tm in range_msgs:
        content = tm.content or ""
        if len(content) > 2000:
            content = content[:2000] + "...<truncated>"
        parts.append(f"[{tm.role}]\n{content}")
    return "\n\n---\n\n".join(parts)


def _format_transcript_for_extract(messages: list[dict[str, Any]], *, max_chars: int = 12000) -> str:
    """Local fallback formatter used only when supervisor omits the callback."""
    # Why: callback injection is the normal path, but isolated tests may construct
    # a minimal context. How: keep the pure formatting logic local without any
    # supervisor imports. Purpose: make the handler robust while preserving the new
    # dependency boundary.
    parts: list[str] = []
    total = 0
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role == "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
            content = "\n".join(texts)
        if not isinstance(content, str):
            content = str(content)
        if len(content) > 2000:
            content = content[:2000] + "...<truncated>"
        line = f"[{role}]\n{content}"
        total += len(line)
        if total > max_chars:
            break
        parts.append(line)
    parts.reverse()
    return "\n\n---\n\n".join(parts)


def _safe_int(value: Any, default: int) -> int:
    """Convert a value to int with a small fallback."""
    # Why: hook contexts can be supplied by tests or future registries. How: guard
    # integer conversion at the boundary. Purpose: keep timer callbacks best-effort
    # and avoid losing the supervisor thread to malformed context data.
    try:
        return int(value)
    except Exception:
        return default
