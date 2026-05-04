from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from engine.compact import (
    _format_messages_for_summary,
    is_compact_circuit_open,
    microcompact_messages,
    record_compact_failure,
    should_compact,
)
from engine.hooks import Handler, HookContext, HookResult
from engine.inference.loop_state import _persist_ctx
from engine.protocol import ACTION_DISPATCH, TaskAction

logger = logging.getLogger(__name__)


class CompactChecker(Handler):
    """Run idle cleanup and automatic context compaction before each step."""

    name = "compact_checker"
    priority = 50

    async def handle(self, ctx: HookContext) -> HookResult | None:
        """Apply the legacy microcompact, proactive snip, and compact checks.

        Why: ai_step.py contained several context pressure checks inline. How:
        read loop state from HookContext.extra, run the same first-step cleanup,
        then dispatch the system compactor when the threshold is exceeded.
        Purpose: keep context management behavior unchanged while moving it into
        a before_step handler.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        modified = False
        step_count = int(ctx.extra.get("step_count", 0) or 0)
        if ctx.step == step_count:
            modified = await _run_idle_cleanup(ctx, ls) or modified

        action = await _check_and_compact(ctx, ls)
        # Why: snip-based compaction mutates messages but intentionally does not
        # dispatch the LLM compactor. How: _check_and_compact marks this in
        # ctx.extra, and the handler folds it into the returned HookResult.
        # Purpose: callers can observe non-terminal compaction mutations.
        modified = bool(ctx.extra.pop("compact_modified", False)) or modified
        if action is not None:
            return HookResult(action=action, modified=modified)
        if modified:
            return HookResult(modified=True)
        return None


async def _run_idle_cleanup(ctx: HookContext, ls: Any) -> bool:
    """Run first-step microcompact and proactive snip cleanup.

    Why: these operations reduce stale context before the next model call. How:
    copy the old first-iteration logic from ai_step.py into this helper. Purpose:
    preserve the old trigger timing while keeping CompactChecker readable.
    """
    modified = False
    _messages, cleared = microcompact_messages(ls.messages)
    if cleared:
        logger.info("microcompact: cleared %d tool_results", cleared)
        modified = True

    try:
        from engine.task_record import load_task_records, snip_history, snip_store

        last_ts = None
        for msg in reversed(ls.messages):
            meta = msg.get("_meta", {})
            if isinstance(meta, dict) and (meta.get("message_type") == "assistant" or msg.get("role") == "assistant"):
                ts_str = meta.get("timestamp", "")
                if ts_str:
                    try:
                        last_ts = datetime.fromisoformat(ts_str)
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                break
        if last_ts is not None:
            gap_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
            if gap_hours >= 1.0:
                proactive_max = max(int(gap_hours) * 2, 2)
                snip_sid = ls.rctx.child_session_id or ls.rctx.session_id
                snip_records = load_task_records(ls.rctx.workspace_root, snip_sid)
                if snip_records:
                    snipped, snip_count, snipped_ids = snip_history(
                        ls.messages,
                        snip_records,
                        keep_recent_tasks=3,
                        max_snip=proactive_max,
                    )
                    if snip_count > 0:
                        ls.messages = snipped
                        ctx.messages = ls.messages
                        store = getattr(ls.rctx, "conversation_store", None)
                        if store:
                            try:
                                persisted = snip_store(store.load(snip_sid), snip_records, snipped_ids)
                                store.replace_all(snip_sid, persisted)
                            except Exception as persist_error:
                                logger.warning("proactive snip persist failed: %s", persist_error)
                        logger.info(
                            "proactive snip: replaced %d tasks (gap=%.1fh, max=%d)",
                            snip_count,
                            gap_hours,
                            proactive_max,
                        )
                        modified = True
    except Exception as snip_error:
        logger.warning("proactive snip failed: %s", snip_error)

    return modified


async def _check_and_compact(ctx: HookContext, ls: Any) -> TaskAction | None:
    """Return a compactor dispatch action when the legacy threshold says so."""
    if ls.compacted or ls.compact_threshold <= 0:
        return None
    if is_compact_circuit_open(ls.rctx.session_id):
        return None
    if not should_compact(ls.messages, ls.compact_threshold, ls.last_prompt_tokens):
        return None

    try:
        from engine.task_record import load_task_records, snip_history, snip_store

        snip_sid = ls.rctx.child_session_id or ls.rctx.session_id
        snip_records = load_task_records(ls.rctx.workspace_root, snip_sid)
        if snip_records:
            snipped, snip_count, snipped_ids = snip_history(ls.messages, snip_records)
            if snip_count > 0:
                ls.messages = snipped
                ctx.messages = ls.messages
                store = getattr(ls.rctx, "conversation_store", None)
                if store:
                    try:
                        stored = store.load(snip_sid)
                        persisted = snip_store(stored, snip_records, snipped_ids)
                        store.replace_all(snip_sid, persisted)
                    except Exception as persist_error:
                        logger.warning("failed to persist snipped history: %s", persist_error)
                await ls.rctx.emit_event("snip_compact", {
                    "node_id": ls.node.id,
                    "step": ctx.step,
                    "snipped_tasks": snip_count,
                })
                logger.info("snip_compact: replaced %d tasks, skipping LLM compact", snip_count)
                ls.compacted = True
                ctx.extra["compact_modified"] = True
                return None
    except Exception as snip_error:
        logger.warning("snip_compact failed, falling through to LLM compact: %s", snip_error)

    ls.compacted = True
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": ctx.step})
        conversation_text = _format_messages_for_summary(
            [m for m in ls.messages if m.get("role") != "system" and not m.get("_dynamic")]
        )
        ptl_max_chars = 300000
        if len(conversation_text) > ptl_max_chars:
            original_len = len(conversation_text)
            conversation_text = conversation_text[-ptl_max_chars:]
            first_sep = conversation_text.find("\n\n---\n\n")
            if first_sep > 0:
                conversation_text = conversation_text[first_sep + len("\n\n---\n\n"):]
            await ls.rctx.emit_event("ptl_truncated", {
                "node_id": ls.node.id,
                "step": ctx.step,
                "original_chars": original_len,
            })
        if conversation_text.strip():
            ctx_ref = _persist_ctx(ls, ctx.step)
            return TaskAction(
                action=ACTION_DISPATCH,
                node_id=ls.node.id,
                target_node="system.compactor",
                context_ref=ctx_ref,
                dispatch_input={
                    "instruction": conversation_text,
                    "_compact_dispatch": True,
                    "context_mode": "fresh",
                    "_compact_keep_recent": ls.compact_keep_recent,
                    "_system_task": True,
                    "use_context": False,
                },
            )
    except Exception as compact_error:
        record_compact_failure(ls.rctx.session_id)
        await ls.rctx.emit_event("compact_failed", {
            "node_id": ls.node.id,
            "step": ctx.step,
            "error": str(compact_error),
        })
    return None


def estimate_context_tokens(messages: list[dict[str, Any]], last_usage: dict | None = None) -> int:
    """Estimate token usage using the same fallback rules as ai_step.py.

    Why: context usage estimation is useful outside ai_step after compaction logic
    moved into hooks. How: prefer the last provider usage, then fall back to
    stored assistant completion usage and character counts. Purpose: keep future
    dynamic-context updates able to reuse the extracted helper.
    """
    if last_usage:
        prompt_tokens = last_usage.get("prompt_tokens", 0) or 0
        completion_tokens = last_usage.get("completion_tokens", 0) or 0
        if prompt_tokens > 0:
            return prompt_tokens + completion_tokens

    total = 0
    for msg in messages:
        if msg.get("_dynamic") or msg.get("_ephemeral"):
            continue
        meta = msg.get("_meta", {})
        if isinstance(meta, dict):
            usage = meta.get("usage", {})
            if isinstance(usage, dict):
                completion_tokens = usage.get("completion_tokens", 0)
                if completion_tokens and isinstance(completion_tokens, int) and completion_tokens > 0:
                    total += completion_tokens
                    continue
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 3
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"]) // 3
    return total
