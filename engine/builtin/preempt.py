from __future__ import annotations

from typing import Any

from engine.attachments import build_multimodal_content
# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result
from engine.inference.message_assembly import rebuild_dynamic_context
from engine.protocol import ACTION_CANCELLED, ACTION_PREEMPTED, TaskAction


# Why: the built-in loader discovers handlers from per-file metadata.
# How: declare the handler class, hook methods, and priority in one place.
# Purpose: remove central hard-coded registration while keeping this handler self-describing.
PLUGIN_META = {
    "handler_class": "PreemptChecker",
    "hook_points": [
        ("before_step", "handle"),
        # [AutoC 2026-08-20] Why: terminal tools need a final preempt check
        # before delivery, and that check belongs to the preempt domain. How:
        # register a second method on the same handler. Purpose: keep preempt
        # scheduling in one plugin while the loop stays unaware.
        ("terminal_tool", "handle_terminal"),
    ],
    "priority": 100,
    "description": "Preempt handling: loop-top injection/cancellation checks and terminal-tool interception.",
}


def is_user_entry_task(ls: Any) -> bool:
    """Return whether this run belongs to a top-level user entry task."""
    rctx = ls.rctx
    if rctx is None or str(rctx.child_session_id or "").strip():
        return False
    tctx = rctx.task_context or {}
    return not (
        tctx.get("is_system_task")
        or tctx.get("caller_task_id")
        or tctx.get("_output_chain")
        or tctx.get("dispatch_origin")
    )


def is_preempt_deferred_for_output_reject(ls: Any) -> bool:
    """Return whether this run is repairing an output_rejected draft."""
    return bool((ls.plugin_data.get("preempt") or {}).get("defer_for_output_reject"))


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
                )
            )

        if is_preempt_deferred_for_output_reject(ls):
            return None

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

    async def handle_terminal(self, ctx: Any) -> Any | None:
        """Intercept finish/ask when a preempt arrived before delivery.

        Why: a terminal tool must not deliver its result when the user sent new
        input or a soft-preempt landed during the same turn; that scheduling
        decision belongs to the preempt domain, not the inference loop. How:
        re-check preempt state, then either write an interception tool_result
        (message preempt) and report intercepted, or write a pairing result and
        return PREEMPTED (message-less preempt). Purpose: the loop no longer
        knows about preempt scheduling around terminal tools.
        """
        ls = ctx.extra.get("loop_state")
        terminal_call = ctx.extra.get("terminal_call") or ctx.tool_call
        if ls is None or terminal_call is None:
            return None

        if is_preempt_deferred_for_output_reject(ls):
            return None

        terminal_name = str(terminal_call.name or "finish").strip() or "finish"
        if (
            terminal_name == "finish"
            and is_user_entry_task(ls)
            and str((terminal_call.arguments or {}).get("text") or "").strip()
        ):
            return None

        # [Preempt V3 需求2] 终止工具执行前的最终 preempt 检查
        # （真实工具执行期间可能有新消息到达）。同时补全 V2 遗漏：无消息 preempt
        # 在只有 finish/ask 没有真工具的场景下也需被检查。
        if ls.preempt_inject_info is None and not ls.preempt_after_step:
            _pi = await ls.rctx.check_preempted()
            if _pi.get("preempted"):
                if _pi.get("message"):
                    ls.preempt_inject_info = _pi
                else:
                    ls.preempt_after_step = True

        from ..inference.loop_state import _persist_ctx
        from ..inference.message_model import MessageMeta, set_message_meta
        from ..inference.tool_format import ParsedToolCall

        if ls.preempt_inject_info is not None:
            # 有消息的 preempt：拦截终止工具，塞假 tool_result 维持 native 配对，
            # 任务继续，下一轮由 handle() 注入新用户消息。
            _terminal_name = str(terminal_call.name or "finish").strip() or "finish"
            _finish_parsed = ParsedToolCall(
                id=terminal_call.id or "",
                name=_terminal_name,
                arguments=terminal_call.arguments or {},
            )
            _intercept_msg = ls.formatter.format_tool_result(
                _finish_parsed,
                "\u26a0\ufe0f Preempted: new user input received. Task continues.",
            )
            # Persist the intercepted ACK so restored history retains the same
            # assistant/tool-result pairing and provider cache prefix.
            if _finish_parsed.id:
                _intercept_msg.setdefault("tool_call_id", _finish_parsed.id)
            _intercept_msg.setdefault("name", _terminal_name)
            set_message_meta(_intercept_msg, MessageMeta(
                tool_mode=ls.node.tool_mode,
                message_type="tool_result",
                control_tool_name=_terminal_name,
                control_tool_status="preempt_intercepted",
            ))
            ls.messages.append(_intercept_msg)
            if not ls._cache_warm_fork:
                from engine.conversation_store import MessageType
                from engine.inference.ai_step import _shadow_write
                _shadow_write(ls, _intercept_msg, MessageType.TOOL_RESULT)
            await ls.rctx.emit_event("preempt_finish_intercepted", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": ctx.step,
            })
            return hook_result(channels={"intercepted": True})

        if ls.preempt_after_step:
            # 无消息的 preempt：补配对结果后保存上下文退出。
            # [2026-05-01] 补写 finish/ask 的 tool_result，确保 native 模式下
            # functionCall/functionResponse 严格 1:1 配对（Gemini 强校验）。
            _terminal_name2 = str(terminal_call.name or "finish").strip() or "finish"
            _finish_parsed2 = ParsedToolCall(
                id=terminal_call.id or "",
                name=_terminal_name2,
                arguments=terminal_call.arguments or {},
            )
            _preempt_result = ls.formatter.format_tool_result(
                _finish_parsed2, "preempted",
            )
            # [2026-05-07] 无消息 preempt 的 ACK 同样只保留在运行期：保存上下文后
            # 恢复时不应看到 finish tool_call/tool_result。
            _preempt_result["_ephemeral"] = True
            if _finish_parsed2.id:
                _preempt_result.setdefault("tool_call_id", _finish_parsed2.id)
            _preempt_result.setdefault("name", _terminal_name2)
            set_message_meta(_preempt_result, MessageMeta(
                tool_mode=ls.node.tool_mode,
                message_type="tool_result",
                control_tool_name=_terminal_name2,
                control_tool_status="preempted",
            ))
            ls.messages.append(_preempt_result)
            ctx_ref = _persist_ctx(ls, int(ctx.step) + 1)
            return hook_result(action=TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref,
            ))

        return None


async def _inject_preempt_message(ctx: Any, ls: Any) -> None:
    """Inject a pending preempt message into the loop state.

    Why: the next LLM prompt must include the user's new message instead of
    finishing stale work. Append and persist the user input before rebuilding
    incremental dynamic context, preserving the existing provider cache prefix.
    """
    new_instruction = ls.preempt_inject_info.get("message", "")
    new_attachments = ls.preempt_inject_info.get("attachments", [])

    _in_mem_meta: dict[str, Any] = {
        "preempt": True,
        "message_type": "user_input",
    }
    _cur_tid = str(ls.rctx.task_id or "").strip()
    if _cur_tid:
        _in_mem_meta["source_task_id"] = _cur_tid
    if new_attachments:
        _in_mem_meta["attachments"] = list(new_attachments)
        ls.messages.append({
            "role": "user",
            "content": build_multimodal_content(
                new_instruction,
                new_attachments,
                workspace_root=ls.rctx.workspace_root,
            ),
            "message_type": "user_input",
            "_meta": _in_mem_meta,
        })
    else:
        ls.messages.append({
            "role": "user",
            "content": new_instruction,
            "message_type": "user_input",
            "_meta": _in_mem_meta,
        })

    # [Fork/Merge 2026-05-12] Persist the injected preempt user message to ConversationStore.
    # Why: before this hook was extracted, preempt injection only mutated the in-memory prompt,
    # so a branch task could lose the new user message when it resumed or later merged. How:
    # append a USER_INPUT record to the active runtime session, preferring child_session_id for
    # delegated child nodes and otherwise using rctx.session_id, which may be an entry branch.
    # Purpose: preempted branch histories remain complete and merge back with the injected input.
    try:
        store = ls.rctx.conversation_store
        if store is not None and (new_instruction or new_attachments):
            from datetime import datetime, timezone
            from uuid import uuid4

            from engine.conversation_store import Message, MessageType

            target_session = ls.rctx.child_session_id or ls.rctx.session_id
            # [AutoC 2026-06-01] Why: preempt messages with attachments were
            # appended to the live prompt as multimodal content but persisted as
            # plain text, so a resume inside the same task lost the image. How:
            # build the same multimodal content for ConversationStore when
            # attachments are present. Purpose: injected user input follows the
            # same task-local image retention rule as initial task input.
            if new_attachments:
                persisted_content = build_multimodal_content(
                    new_instruction,
                    new_attachments,
                    workspace_root=ls.rctx.workspace_root,
                )
            else:
                persisted_content = new_instruction
            # [2026-06-06] Why: preempt messages injected here are always user-
            # initiated追加指令 (child-node callbacks use inject_async_result which
            # goes through the inbound path instead). How: unconditionally mark
            # meta.preempt=True. Purpose: frontend history hydration renders these
            # as inline preempt cards instead of plain user messages.
            _meta: dict[str, Any] = {"preempt": True}
            if new_attachments:
                _meta["attachments"] = list(new_attachments)
            store.append(
                target_session,
                Message(
                    id=str(uuid4()),
                    role="user",
                    content=persisted_content,
                    message_type=MessageType.USER_INPUT,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    meta=_meta,
                    source_node_id=ls.node.id,
                    source_task_id=ls.rctx.task_id,
                ),
            )
    except Exception:
        pass

    rebuild_dynamic_context(
        ls.messages,
        workspace_root=ls.rctx.workspace_root,
        node=ls.node,
        runtime_cfg=ls.runtime_cfg,
        instruction=new_instruction,
        is_block_mode=ls.is_block_mode,
        system_prompt=ls.system_prompt,
        session_id=ls.rctx.session_id or "",
        workspace_name=ls.rctx.workspace_name or "",
        task_context=ls.rctx.task_context,
    )

    # [2026-09-13] 携带实际消费内容：注入到清空之间并发追加的消息不会被误删。
    await ls.rctx.consume_preempt(
        str(new_instruction or ""),
        len(new_attachments) if isinstance(new_attachments, list) else 0,
    )
    await ls.rctx.emit_event("preempt_injected", {
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
        "step": ctx.step,
        "message": new_instruction,
    })

    ls.preempt_inject_info = None
    ls.plaintext_retry_count = 0
    ls.compacted = False
