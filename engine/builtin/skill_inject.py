from __future__ import annotations

from typing import Any
from clonoth_runtime import get_int
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from toolbox.skills_runtime import build_skill_messages


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "SkillInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 50,
}


class SkillInjector:
    """Glue handler for build_skill_messages."""

    name = "skill_inject"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """Build skill messages and optionally rebuild the prompt layout.

        Why: run_ai_node can now ask assemble_initial_messages to skip old inline
        skill injection. How: this handler calls build_skill_messages, stores the
        static and dynamic results in ctx.extra, and when apply_injection=True
        rebuilds ctx.messages through message_assembly's shared layout helper.
        Purpose: make skill injection a real before_prompt_build hook without
        duplicate messages or prompt layout drift.
        """
        if ctx.node is None or ctx.rctx is None:
            return None

        from engine.inference.message_assembly import _conversational_history

        runtime_cfg = ctx.extra.get("runtime_cfg") or {}
        instruction_text = str(ctx.extra.get("instruction_text") or "")
        history = _conversational_history(ctx.extra.get("history") or [])
        static_msgs, dynamic_msgs = build_skill_messages(
            ctx.rctx.workspace_root,
            node_id=ctx.node.id,
            instruction_text=instruction_text,
            history=history,
            skill_mode=ctx.node.skill_access.mode,
            skill_allow=ctx.node.skill_access.allow,
            max_budget_chars=get_int(runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
        )
        ctx.extra["skill_static_messages"] = static_msgs
        ctx.extra["skill_dynamic_messages"] = dynamic_msgs

        if ctx.extra.get("apply_injection"):
            _rebuild_prompt_messages(ctx)
            return hook_result(modified=True)
        return hook_result(modified=bool(static_msgs or dynamic_msgs))


def _rebuild_prompt_messages(ctx: Any) -> None:
    """Rebuild ctx.messages with all prompt injections currently in ctx.extra.

    Why: SkillInjector runs before MemoryInjector, so either handler may be the
    last one to change available injection blocks. How: call the shared
    assemble_messages_with_injections helper with skill and memory lists from
    ctx.extra, then replace the existing list in-place. Purpose: keep the
    original messages list object that run_ai_node continues using.
    """
    from engine.inference.message_assembly import assemble_messages_with_injections

    rebuilt, is_block_mode = assemble_messages_with_injections(
        workspace_root=ctx.rctx.workspace_root,
        system_prompt=list(ctx.extra.get("system_prompt") or []),
        history=list(ctx.extra.get("history") or []),
        instruction=str(ctx.extra.get("instruction_text") or ""),
        attachments=ctx.extra.get("attachments"),
        skill_static=list(ctx.extra.get("skill_static_messages") or []),
        skill_dynamic=list(ctx.extra.get("skill_dynamic_messages") or []),
        memory_static=list(ctx.extra.get("memory_static_messages") or []),
        memory_dynamic=list(ctx.extra.get("memory_dynamic_messages") or []),
    )
    ctx.messages[:] = rebuilt
    ctx.extra["is_block_mode"] = is_block_mode
