from __future__ import annotations

from engine.hooks import Handler, HookContext, HookResult


class AttachmentCollector(Handler):
    """Collect attachments produced by real tool calls."""

    name = "attachment_collector"
    priority = 0

    async def handle(self, ctx: HookContext) -> HookResult | None:
        """Preserve legacy attachment collection after a tool result.

        Why: real tools can return attachments that final pseudo tools later
        select or expose. How: read tool_result from ctx.extra, extend the local
        per-batch attachment list and the loop-level collected attachment lists.
        Purpose: move the after-tool side effect out of ai_step.py without
        changing final attachment behavior.
        """
        tool_result = ctx.extra.get("tool_result")
        if not isinstance(tool_result, dict) or not isinstance(tool_result.get("attachments"), list):
            return None

        attachments = list(tool_result["attachments"])
        if not attachments:
            return None

        local_attachments = ctx.extra.get("tool_attachments")
        if isinstance(local_attachments, list):
            local_attachments.extend(attachments)

        ls = ctx.extra.get("loop_state")
        if ls is not None:
            ls.collected_attachments.extend(attachments)
            ls.tool_produced_attachments.extend(attachments)

        return HookResult(modified=True)
