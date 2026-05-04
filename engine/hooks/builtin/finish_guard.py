from __future__ import annotations

import logging
from typing import Any

from engine.hooks import Handler, HookContext, HookResult

logger = logging.getLogger(__name__)

_REJECT_MESSAGE = (
    "\u274c REJECTED: finish() cannot be called alongside other tools "
    "(except reply). Execute your other tools first, wait for their "
    "results, then call finish() alone in a separate turn."
)


class FinishGuardHandler(Handler):
    """Reject finish() when it is colocated with non-reply tool calls."""

    name = "finish_guard"
    priority = 100

    async def handle(self, ctx: HookContext) -> HookResult | None:
        """Apply the legacy finish colocation guard.

        Why: finish terminates the task, so other same-turn tool results would
        never be read by the model. How: prefer ai_step's already-filtered legacy
        pseudo/real call lists when present, and otherwise fall back to raw
        ctx.tool_calls for isolated tests. Purpose: move the guard out of
        ai_step.py without changing existing behavior.
        """
        pseudo_calls = ctx.extra.get("pseudo_calls")
        real_tool_calls = ctx.extra.get("real_tool_calls")

        if pseudo_calls is not None or real_tool_calls is not None:
            pseudo_list = list(pseudo_calls or [])
            real_list = list(real_tool_calls or [])
            has_finish = any(_tool_name(call) == "finish" for call in pseudo_list)
            has_non_reply_others = bool(real_list) or any(
                _tool_name(call) not in ("finish", "reply") for call in pseudo_list
            )
        else:
            calls = list(ctx.tool_calls or [])
            has_finish = any(_tool_name(call) == "finish" for call in calls)
            has_non_reply_others = any(
                _tool_name(call) not in ("finish", "reply") for call in calls
            )

        if not (has_finish and has_non_reply_others):
            return None

        logger.warning(
            "Rejected finish + other tools in same turn (node=%s, step=%d, tools=%s)",
            getattr(ctx.node, "id", ""),
            ctx.step,
            [_tool_name(call) for call in (ctx.tool_calls or [])],
        )
        return HookResult(block=True, reason="finish_colocated", error_message=_REJECT_MESSAGE)


def _tool_name(call: Any) -> str:
    """Read a tool-call name from object or dict shapes.

    Why: ai_step and tests may pass Provider ToolCall objects, ParsedToolCall
    objects, or dicts. How: support attribute and dict access. Purpose: keep the
    handler decoupled from one formatter implementation.
    """
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")
