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
    # Why: memory_extract writes new entries that knowledge_inject must cache-invalidate.
    # How: declare the dependency so loader ensures knowledge_inject loads first.
    # Purpose: fail clearly if knowledge_inject is missing rather than silent runtime errors.
    "requires": ["knowledge_inject"],
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
        # [2026-05-23] Why: compacted conversations can shrink message counts,
        # while entry-task completions remain monotonic within this handler. How:
        # count every qualifying entry-task finish per session. Purpose: make the
        # auto-extract increment gate use task granularity instead of message offsets.
        self._memory_extract_task_counts: dict[str, int] = {}
        # [2026-05-23] Why: idle timers can be cancelled before extraction, but
        # those passed entry tasks should still count toward a later threshold.
        # How: keep a separate committed cursor that only moves when the timer
        # fires. Purpose: compute task increments correctly across cancelled idle windows.
        self._memory_extract_last_extracted_task_counts: dict[str, int] = {}
        # [2026-05-23] Why: task counts now decide when extraction should run,
        # but they cannot identify which messages still need to be sent to the
        # extractor. How: keep a separate message cursor that is committed only
        # after the idle callback fires. Purpose: slice the full unextracted
        # transcript range without returning to message-count trigger logic.
        self._memory_extract_msg_cursors: dict[str, int] = {}
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
            log.debug("memory_extract gate: blocked by action=%s kind=%s", act, getattr(task, "kind", "?"))
            return
        task_input = getattr(task, "input", {}) or {}
        if task_input.get("_system_task"):
            log.debug("memory_extract gate: blocked by _system_task")
            return
        # [2026-05-22] Why: child/dispatch tasks and branch tasks are short-lived
        # and do not represent real user conversation turns. How: skip tasks that
        # have a caller_task_id (dispatched by another node) or whose task_id
        # starts with 'branch_'. Purpose: prevent CPU waste from high-frequency
        # gate checks on child node callbacks.
        caller_tid = getattr(task, "caller_task_id", None)
        if caller_tid:
            log.debug("memory_extract gate: blocked by caller_task_id=%s (child task)", str(caller_tid)[:8])
            return
        # [2026-05-23 fix] Entry tasks run on temporary branch sessions (task.session_id
        # starts with 'branch_'), but they represent real user conversation turns. The
        # on_entry_task_complete hook passes the merged parent session in ctx["session_id"].
        # Only filter by task_id prefix (which is never 'branch_' for entry tasks);
        # do NOT filter by task.session_id — that would block all entry task extractions.
        task_id_str = str(getattr(task, "task_id", "") or "")
        if task_id_str.startswith("branch_"):
            log.debug("memory_extract gate: blocked by branch task_id=%s", task_id_str[:16])
            return
        log.debug("memory_extract: entered gate check for task %s node=%s", task_id_str[:8], getattr(task, 'node_id', '?'))

        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            log.debug("memory_extract gate: no workspace_root")
            return
        workspace_root = Path(workspace_root)
        runtime_cfg = load_runtime_config(workspace_root)
        entry_node_id = get_str(runtime_cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip()
        if getattr(task, "node_id", None) != entry_node_id:
            log.debug("memory_extract gate: node_id=%s != entry_node_id=%s", getattr(task, "node_id", None), entry_node_id)
            return
        if not get_bool(runtime_cfg, "memory.auto_extract.enabled", False):
            log.debug("memory_extract gate: auto_extract disabled")
            return
        log.debug(
            "memory_extract: passed gates for task %s runtime_session=%s route_session=%s",
            str(getattr(task, "task_id", "?") or "?")[:8],
            getattr(task, "session_id", "?"),
            ctx.get("session_id"),
        )

        # Why: a task that explicitly saved memory has already handled the turn.
        # How: preserve the old save_memory tool-name mutual exclusion. Purpose:
        # avoid duplicate memory entries from automatic extraction.
        tool_names = (getattr(task, "result", None) or {}).get("_tool_names") or []
        if "save_memory" in tool_names:
            log.debug("memory_extract TRACE: BLOCKED by save_memory in tool_names")
            return

        session_messages = ctx.get("session_messages")
        if not callable(session_messages):
            log.debug("memory_extract: skip, no session_messages callback")
            return
        # [Fork/Merge 2026-05-17] Why: completed entry tasks run on temporary
        # branch sessions, while on_entry_task_complete passes the merged parent
        # session in ctx["session_id"]. How: prefer the hook route session and
        # fall back to the task runtime session for legacy/non-branch tasks.
        # Purpose: min_messages and transcript extraction read durable history.
        session_id = str(ctx.get("session_id") or getattr(task, "session_id", "") or "").strip()
        if not session_id:
            log.debug("memory_extract: skip, empty session_id")
            return
        log.debug("memory_extract TRACE: session_id=%s (ctx=%s task=%s)", session_id, ctx.get('session_id'), getattr(task, 'session_id', '?'))
        # [2026-05-23] Why: session message counts can shrink after compaction,
        # so they are no longer safe as the trigger cursor. How: count each entry
        # task that has passed the gate after resolving the durable parent session.
        # Purpose: make memory.auto_extract.min_increment operate on entry tasks.
        task_count = self._memory_extract_task_counts.get(session_id, 0) + 1
        self._memory_extract_task_counts[session_id] = task_count

        msgs = session_messages(session_id, limit=0)
        non_system = [m for m in msgs if m.get("role") != "system"]
        current_msg_count = len(non_system)

        min_messages = get_int(runtime_cfg, "memory.auto_extract.min_messages", 4, min_value=2, max_value=100)
        if current_msg_count < min_messages:
            log.debug("memory_extract TRACE: BLOCKED current_msg_count=%d < min_messages=%d", current_msg_count, min_messages)
            return

        # [2026-05-23] Why: task counts and message offsets now serve different
        # purposes. How: read the last committed message cursor separately and
        # repair it when compaction makes the durable message count smaller.
        # Purpose: skip compacted summaries instead of replaying old history,
        # while leaving the task counter untouched and monotonic.
        # [2026-05-24] Scheme B: on first encounter after restart, set cursor
        # to current_msg_count so we only extract genuinely new messages, not
        # the entire session history. This avoids re-extracting stale content
        # from old conversation turns every time the engine restarts.
        _is_first_encounter = session_id not in self._memory_extract_msg_cursors
        if _is_first_encounter:
            self._memory_extract_msg_cursors[session_id] = current_msg_count
            log.debug(
                "memory_extract: first encounter for session %s, setting msg cursor to %d (skip history)",
                session_id[:12], current_msg_count,
            )
        last_msg_cursor = self._memory_extract_msg_cursors.get(session_id, 0)
        if current_msg_count < last_msg_cursor:
            log.debug(
                "memory_extract TRACE: msg cursor advance (compact detected) session=%s current_msg_count=%d < last_msg_cursor=%d -> cursor=%d",
                session_id,
                current_msg_count,
                last_msg_cursor,
                current_msg_count,
            )
            last_msg_cursor = current_msg_count
            self._memory_extract_msg_cursors[session_id] = current_msg_count

        min_increment = get_int(runtime_cfg, "memory.auto_extract.min_increment", 3, min_value=1, max_value=100)
        last_extracted_task_count = self._memory_extract_last_extracted_task_counts.get(session_id, 0)
        increment = task_count - last_extracted_task_count
        log.debug(
            "memory_extract TRACE: task_count=%d last_extracted_task_count=%d increment=%d min_incr=%d last_msg_cursor=%d current_msg_count=%d",
            task_count,
            last_extracted_task_count,
            increment,
            min_increment,
            last_msg_cursor,
            current_msg_count,
        )
        if increment < min_increment:
            log.debug(
                "memory_extract: task increment=%d < min_increment=%d (task_count=%d, last_extracted_task_count=%d)",
                increment,
                min_increment,
                task_count,
                last_extracted_task_count,
            )
            if task_count > last_extracted_task_count:
                # [2026-05-23] Why: low-volume sessions may not reach the task
                # threshold quickly. How: keep the fallback idle path, but use
                # the same message-cursor transcript range and commit only after
                # it fires. Purpose: preserve low-volume extraction while keeping
                # trigger counting independent from transcript slicing.
                fallback_delay = get_int(
                    runtime_cfg,
                    "memory.auto_extract.idle_fallback_delay_sec",
                    120,
                    min_value=5,
                    max_value=3600,
                )
                log.debug(
                    "memory_extract TRACE: scheduling FALLBACK timer (%ds) for session %s task_count=%d last_msg_cursor=%d current_msg_count=%d",
                    fallback_delay,
                    session_id,
                    task_count,
                    last_msg_cursor,
                    current_msg_count,
                )
                pending_extract = self._prepare_memory_extract_pending_locked(
                    ctx=ctx,
                    task=task,
                    workspace_root=workspace_root,
                    runtime_cfg=runtime_cfg,
                    session_id=session_id,
                    msgs=msgs,
                    task_count=task_count,
                    last_msg_cursor=last_msg_cursor,
                    current_msg_count=current_msg_count,
                )
                if pending_extract is not None:
                    self._schedule_memory_extract_idle_locked(
                        ctx=ctx,
                        session_id=session_id,
                        pending_extract=pending_extract,
                        delay_sec=fallback_delay,
                        timer_label="fallback idle",
                    )
            return
        pending_extract = self._prepare_memory_extract_pending_locked(
            ctx=ctx,
            task=task,
            workspace_root=workspace_root,
            runtime_cfg=runtime_cfg,
            session_id=session_id,
            msgs=msgs,
            task_count=task_count,
            last_msg_cursor=last_msg_cursor,
            current_msg_count=current_msg_count,
        )
        if pending_extract is None:
            return

        # [2026-04-26] P4b pre-injection stays removed: large memory lists polluted
        # the main session history and could exceed context budgets. The extractor
        # node prompt already contains duplicate-prevention instructions.
        idle_delay = get_int(runtime_cfg, "memory.auto_extract.idle_delay_sec", 30, min_value=5, max_value=120)
        log.debug(
            "memory_extract TRACE: scheduling NORMAL timer (%ds) for session %s task_count=%d last_msg_cursor=%d current_msg_count=%d",
            idle_delay,
            session_id,
            task_count,
            last_msg_cursor,
            current_msg_count,
        )
        self._schedule_memory_extract_idle_locked(
            ctx=ctx,
            session_id=session_id,
            pending_extract=pending_extract,
            delay_sec=idle_delay,
            timer_label="idle",
        )

    def _prepare_memory_extract_pending_locked(
        self,
        *,
        ctx: dict[str, Any],
        task: Any,
        workspace_root: Path,
        runtime_cfg: dict[str, Any],
        session_id: str,
        msgs: list[dict[str, Any]],
        task_count: int,
        last_msg_cursor: int,
        current_msg_count: int,
    ) -> dict[str, Any] | None:
        """Build the pending extraction payload shared by normal and fallback timers."""
        # [2026-05-23] Why: task-count cursors no longer identify message
        # offsets, but extraction must still cover every unextracted message.
        # How: build the transcript from last_msg_cursor to current_msg_count
        # without a fixed 50-message cap. Purpose: use task counts only for the
        # trigger decision and message cursors only for transcript slicing.
        transcript = _conversation_store_transcript(workspace_root, session_id, last_msg_cursor, current_msg_count)
        if not transcript:
            # Why: ConversationStore can be absent in tests or replay-only sessions.
            # How: slice the same full non-system message range before delegating
            # to the injected formatter. Purpose: keep store and callback fallbacks
            # aligned with the committed message cursor.
            range_msgs = _non_system_message_range(msgs, last_msg_cursor, current_msg_count)
            format_transcript = ctx.get("format_transcript")
            if callable(format_transcript):
                transcript = str(format_transcript(range_msgs) or "")
            else:
                transcript = _format_transcript_for_extract(range_msgs)

        if not transcript.strip():
            return None

        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        return {
            "session_id": session_id,
            "session_generation": int(getattr(task, "session_generation", 0) or 0),
            "transcript": transcript,
            "task_count": task_count,
            "last_msg_cursor": last_msg_cursor,
            "current_msg_count": current_msg_count,
            # [2026-05-23] Why: older tests and logs referred to message_count.
            # How: keep it as an alias for the current message cursor position.
            # Purpose: preserve observability while the code path moves to the
            # clearer current_msg_count name.
            "message_count": current_msg_count,
            "extractor_node": extractor_node,
            "kind": getattr(task, "kind", "node"),
        }

    def _schedule_memory_extract_idle_locked(
        self,
        *,
        ctx: dict[str, Any],
        session_id: str,
        pending_extract: dict[str, Any],
        delay_sec: int,
        timer_label: str,
    ) -> None:
        """Replace the per-session idle timer with a prepared extraction payload."""
        # Why: fallback and normal idle extraction must share one cancellation slot.
        # How: cancel any existing Timer for the session before storing the new
        # pending payload and Timer. Purpose: a later finish or inbound cannot leave
        # stale normal or fallback callbacks active for the same session.
        old_timer = self._memory_extract_timers.pop(session_id, None)
        if old_timer is not None:
            old_timer.cancel()
            log.debug("memory_extract TRACE: cancelled old timer for session %s", session_id)
        self._memory_extract_pending[session_id] = pending_extract
        timer = threading.Timer(delay_sec, self._fire_memory_extract_idle, args=[dict(ctx), session_id])
        timer.daemon = True
        self._memory_extract_timers[session_id] = timer
        timer.start()
        log.debug("memory_extract TRACE: TIMER STARTED [%s] session=%s delay=%ds", timer_label, session_id, delay_sec)

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
            log.debug("memory_extract TRACE: INBOUND CANCEL timer for session %s (had_timer=%s had_pending=%s)", sid, timer is not None, pending is not None)

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
            task_count = _safe_int(pending_extract.get("task_count"), self._memory_extract_task_counts.get(sid, 0))
            current_msg_count = _safe_int(
                pending_extract.get("current_msg_count", pending_extract.get("message_count")),
                self._memory_extract_msg_cursors.get(sid, 0),
            )
            session_generation = _safe_int(pending_extract.get("session_generation"), 0)
            if session_generation <= 0:
                current_generation = ctx.get("current_session_generation")
                session_generation = int(current_generation(sid) or 1) if callable(current_generation) else 1
            extractor_node = str(pending_extract.get("extractor_node") or "system.memory_extractor").strip()
            extractor_node = extractor_node or "system.memory_extractor"

            # [2026-05-23] Why: cancelled idle windows must not mark task or
            # message cursors as extracted. How: commit both the last-extracted
            # task count and the message cursor only inside the timer callback
            # after pending data is popped. Purpose: cancelled idle windows still
            # contribute to future task thresholds without losing transcript slices.
            self._memory_extract_task_counts[sid] = task_count
            self._memory_extract_last_extracted_task_counts[sid] = task_count
            self._memory_extract_msg_cursors[sid] = current_msg_count

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
    """Read the exact unextracted non-system message range from ConversationStore."""
    # [2026-05-23] Why: task counts no longer map to message offsets, but the
    # extractor still needs a stable transcript slice. How: read ConversationStore
    # and slice all non-system messages between the committed message cursor and
    # the current message count, with no fixed 50-message cap. Purpose: avoid
    # dropping unextracted messages while keeping trigger logic task-based.
    try:
        from engine.conversation_store import ConversationStore

        store = ConversationStore(workspace_root / "data" / "conversations")
        all_msgs = store.load(session_id)
        non_sys = [m for m in all_msgs if m.role != "system"]
        start = max(0, min(len(non_sys), int(last_count)))
        end = max(start, min(len(non_sys), int(current_count)))
        range_msgs = non_sys[start:end]
    except Exception:
        return ""

    parts: list[str] = []
    for tm in range_msgs:
        content = tm.content or ""
        if len(content) > 2000:
            content = content[:2000] + "...<truncated>"
        parts.append(f"[{tm.role}]\n{content}")
    return "\n\n---\n\n".join(parts)


def _non_system_message_range(messages: list[dict[str, Any]], last_count: int, current_count: int) -> list[dict[str, Any]]:
    """Return the same non-system cursor range for session_messages fallbacks."""
    # [2026-05-23] Why: callback-based transcript formatting must mirror the
    # ConversationStore slice. How: apply the same bounded start/end indexes to
    # the non-system dictionaries supplied by session_messages. Purpose: keep
    # fallback transcript creation faithful to the committed message cursor.
    non_system = [m for m in messages if m.get("role") != "system"]
    start = max(0, min(len(non_system), int(last_count)))
    end = max(start, min(len(non_system), int(current_count)))
    return non_system[start:end]


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
