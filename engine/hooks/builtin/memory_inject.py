from __future__ import annotations

from clonoth_runtime import get_int
from engine.hooks import Handler, HookContext, HookResult
from engine.memory import build_memory_messages


class MemoryInjector(Handler):
    """Glue handler for build_memory_messages."""

    name = "memory_inject"
    priority = 40

    async def handle(self, ctx: HookContext) -> HookResult | None:
        """Build memory messages and optionally rebuild the prompt layout.

        Why: run_ai_node can now skip old inline memory injection when this hook
        is registered. How: call build_memory_messages, store the results in
        ctx.extra, and when apply_injection=True rebuild ctx.messages with the
        same shared layout helper used by SkillInjector. Purpose: make memory
        injection a real before_prompt_build hook without duplicate memory blocks.
        """
        if ctx.node is None or ctx.rctx is None or ctx.node.memory_access.mode == "none":
            return None

        from engine.inference.message_assembly import _conversational_history

        runtime_cfg = ctx.extra.get("runtime_cfg") or {}
        instruction_text = str(ctx.extra.get("instruction_text") or "")
        history = _conversational_history(ctx.extra.get("history") or [])
        static_msgs, dynamic_msgs = build_memory_messages(
            ctx.rctx.workspace_root,
            node_id=ctx.node.id,
            instruction_text=instruction_text,
            history=history,
            max_budget_chars=get_int(runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
            memory_mode=ctx.node.memory_access.mode,
            memory_allow=ctx.node.memory_access.allow,
        )
        ctx.extra["memory_static_messages"] = static_msgs
        ctx.extra["memory_dynamic_messages"] = dynamic_msgs

        if ctx.extra.get("apply_injection"):
            # Why: MemoryInjector runs after SkillInjector, so this second rebuild
            # folds both skill and memory blocks into their final legacy positions.
            # How: reuse SkillInjector's in-place prompt rebuild helper. Purpose:
            # preserve one canonical prompt layout path for both handlers.
            from .skill_inject import _rebuild_prompt_messages

            _rebuild_prompt_messages(ctx)
            return HookResult(modified=True)
        return HookResult(modified=bool(static_msgs or dynamic_msgs))
