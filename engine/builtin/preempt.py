from __future__ import annotations

from typing import Any

from clonoth_runtime import get_int
from engine.attachments import build_multimodal_content
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.protocol import ACTION_CANCELLED, TaskAction
from engine.memory import build_memory_messages
from toolbox.skills_runtime import build_skill_messages


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "PreemptChecker",
    "hook_points": [
        ("before_step", "handle"),
    ],
    "priority": 100,
}


class PreemptChecker:
    """Handle cancellation and preempt message injection before each LLM step."""

    name = "preempt_checker"
    priority = 100

    async def handle(self, ctx: Any) -> Any | None:
        """Run the legacy loop-top cancellation and preempt checks.

        Why: cancellation and preempt state were hard-coded in ai_step.py, which
        made the inference loop harder to extend. How: read the current loop
        state from HookContext.extra, perform the same checks, and mutate the
        message list when a preempt message must be injected. Purpose: preserve
        the old control flow while moving the behavior behind before_step.
        """
        ls = ctx.extra.get("loop_state")
        if ls is None:
            return None

        if await ls.rctx.check_cancelled():
            await ls.rctx.emit_event("cancel_acknowledged", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": ctx.step,
            })
            return hook_result(
                action=TaskAction(
                    action=ACTION_CANCELLED,
                    node_id=ls.node.id,
                    summary="任务已被用户取消。",
                )
            )

        if not ls.preempt_after_step and ls.preempt_inject_info is None:
            preempt_info = await ls.rctx.check_preempted()
            if preempt_info.get("preempted"):
                if preempt_info.get("message"):
                    ls.preempt_inject_info = preempt_info
                else:
                    ls.preempt_after_step = True
                    await ls.rctx.emit_event("preempt_acknowledged", {
                        "node_id": ls.node.id,
                        "task_id": ls.rctx.task_id,
                        "step": ctx.step,
                    })

        if ls.preempt_inject_info is None:
            return None

        await _inject_preempt_message(ctx, ls)
        return hook_result(modified=True)


async def _inject_preempt_message(ctx: Any, ls: Any) -> None:
    """Inject a pending preempt message into the loop state.

    Why: the next LLM prompt must include the user's new message instead of
    finishing stale work. How: remove old dynamic messages, rebuild dynamic
    skill and memory context, append the preempt user input, and acknowledge the
    runtime. Purpose: keep Preempt V2 behavior identical after extraction.
    """
    new_instruction = ls.preempt_inject_info.get("message", "")
    new_attachments = ls.preempt_inject_info.get("attachments", [])

    ls.messages = [m for m in ls.messages if not m.get("_dynamic")]
    ctx.messages = ls.messages

    from engine.inference.message_assembly import _conversational_history

    scan_history = _conversational_history(ls.history)
    skill_static, skill_dynamic = build_skill_messages(
        ls.rctx.workspace_root,
        node_id=ls.node.id,
        instruction_text=new_instruction,
        history=scan_history,
        skill_mode=ls.node.skill_access.mode,
        skill_allow=ls.node.skill_access.allow,
        max_budget_chars=get_int(ls.runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
    )
    # Why: build_skill_messages returns both static and dynamic blocks, but the
    # preempt path historically reinjected only dynamic blocks. How: keep the
    # static value assigned for parity with the old call shape while using only
    # dynamic messages below. Purpose: avoid changing prompt-cache layout.
    _ = skill_static

    if ls.node.memory_access.mode == "none":
        memory_dynamic = []
    else:
        memory_static, memory_dynamic = build_memory_messages(
            ls.rctx.workspace_root,
            node_id=ls.node.id,
            instruction_text=new_instruction,
            history=scan_history,
            max_budget_chars=get_int(ls.runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
            memory_mode=ls.node.memory_access.mode,
            memory_allow=ls.node.memory_access.allow,
        )
        # Why: same as skills, preempt reinjection only used dynamic memory.
        # How: discard the static return explicitly. Purpose: preserve legacy
        # message placement during this extraction.
        _ = memory_static

    dynamic_parts: list[str] = []
    if not ls.is_block_mode and len(ls.system_prompt) >= 2 and ls.system_prompt[1].get("content"):
        dynamic_parts.append(ls.system_prompt[1]["content"])
    for dynamic_msg in skill_dynamic:
        if dynamic_msg.get("content"):
            dynamic_parts.append(dynamic_msg["content"])
    for dynamic_msg in memory_dynamic:
        if dynamic_msg.get("content"):
            dynamic_parts.append(dynamic_msg["content"])

    if dynamic_parts:
        dynamic_prefix = (
            "以下是本轮动态上下文，每轮可能变化。\n\n"
            if ls.is_block_mode
            else "以下是本轮动态上下文信息，每轮可能变化。如与当前任务无关可忽略，继续之前的工作即可。\n\n"
        )
        ls.messages.append({
            "role": "user",
            "content": dynamic_prefix + "\n\n".join(dynamic_parts),
            "_dynamic": True,
        })

    if new_attachments:
        ls.messages.append({
            "role": "user",
            "content": build_multimodal_content(
                new_instruction,
                new_attachments,
                workspace_root=ls.rctx.workspace_root,
            ),
        })
    else:
        ls.messages.append({"role": "user", "content": new_instruction})

    await ls.rctx.consume_preempt()
    await ls.rctx.emit_event("preempt_injected", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "step": ctx.step,
    })

    ls.preempt_inject_info = None
    ls.plaintext_retry_count = 0
    ls.compacted = False
