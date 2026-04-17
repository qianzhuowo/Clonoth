"""LLM 调用（含重试逻辑）。

从 ai_step.py 抽出。依赖 _LoopState、_StreamBuffer。
"""
from __future__ import annotations

import asyncio
from typing import Any

from .stream_buffer import _StreamBuffer
from ..attachments import prepare_messages_for_llm
from ..protocol import TaskAction, ACTION_CANCELLED, ACTION_FAIL
from .loop_state import _LoopState, _persist_ctx, _short
# message_to_llm 反序列化：在发送 LLM 前做格式转换（role 修正 + 内部字段剥离）
from .tool_format import build_llm_messages


# ---------------------------------------------------------------------------
#  可重试状态码
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _is_retryable_error(resp) -> bool:
    """判定 ProviderResponse 是否属于可重试的临时性错误。"""
    if resp.ok:
        return False
    if resp.status_code is not None:
        return resp.status_code in _RETRYABLE_STATUS_CODES
    return True


# ---------------------------------------------------------------------------
#  LLM 调用（含重试）
# ---------------------------------------------------------------------------

async def _call_llm_with_retry(ls: _LoopState, step: int):
    """LLM 调用（含重试）。返回 ProviderResponse 或 TaskAction(CANCELLED)。"""
    tools_arg = ls.openai_tools if ls.openai_tools else None
    # 反序列化方向：先用 build_llm_messages 做格式转换（修正跨模式 role、剥离 _meta 等内部字段），
    # 再用 prepare_messages_for_llm 处理图片 file:// → base64 解析。
    # build_llm_messages 会跳过 _ephemeral 消息（retry hint 等），但保留 _dynamic（动态上下文）。
    # 注意：不能修改 ls.messages 本身，它是运行时状态。
    _formatted = build_llm_messages(ls.messages, ls.formatter) if ls.formatter else ls.messages
    llm_messages = prepare_messages_for_llm(_formatted, ls.rctx.workspace_root)

    resp = None
    _retry_attempt = 0

    while True:
        text_buf: _StreamBuffer | None = None
        think_buf: _StreamBuffer | None = None

        # ---- 流式调用 ----
        if ls.use_stream:
            text_buf = _StreamBuffer(ls.rctx, ls.node.id, "text")
            think_buf = _StreamBuffer(ls.rctx, ls.node.id, "thinking")
            stream_task = asyncio.create_task(
                ls.provider.chat_stream(
                    messages=llm_messages,
                    tools=tools_arg,
                    on_text=text_buf.push,
                    on_thinking=think_buf.push,
                )
            )
            while True:
                done, _ = await asyncio.wait({stream_task}, timeout=0.3)
                if stream_task in done:
                    resp = stream_task.result()
                    break
                if await ls.rctx.check_cancelled():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    await text_buf.flush()
                    await think_buf.flush()
                    if text_buf.flushed_any or think_buf.flushed_any:
                        await ls.rctx.emit_event("stream_end", {"node_id": ls.node.id, "has_text": text_buf.flushed_any, "has_thinking": think_buf.flushed_any})
                    await ls.rctx.emit_event("cancel_acknowledged", {"node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step})
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")
                if not ls.preempt_after_step and ls.preempt_inject_info is None:
                    _pi_s = await ls.rctx.check_preempted()
                    if _pi_s.get("preempted"):
                        if _pi_s.get("message"):
                            ls.preempt_inject_info = _pi_s
                        else:
                            ls.preempt_after_step = True
            await text_buf.flush()
            await think_buf.flush()
            if text_buf.flushed_any or think_buf.flushed_any:
                await ls.rctx.emit_event("stream_end", {
                    "node_id": ls.node.id,
                    "has_text": text_buf.flushed_any,
                    "has_thinking": think_buf.flushed_any,
                })
            if resp.ok and resp.tool_calls:
                ls.use_stream = False
        else:
            # ---- 非流式调用（可取消） ----
            llm_task = asyncio.create_task(
                ls.provider.chat(messages=llm_messages, tools=tools_arg)
            )
            while True:
                done, _ = await asyncio.wait({llm_task}, timeout=0.3)
                if llm_task in done:
                    resp = llm_task.result()
                    break
                if await ls.rctx.check_cancelled():
                    llm_task.cancel()
                    try:
                        await llm_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    await ls.rctx.emit_event("cancel_acknowledged", {"node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step})
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")
                if not ls.preempt_after_step and ls.preempt_inject_info is None:
                    _pi_n = await ls.rctx.check_preempted()
                    if _pi_n.get("preempted"):
                        if _pi_n.get("message"):
                            ls.preempt_inject_info = _pi_n
                        else:
                            ls.preempt_after_step = True

        assert resp is not None

        # ---- 提取 token usage ----
        if resp.usage and isinstance(resp.usage.get("prompt_tokens"), int):
            ls.last_prompt_tokens = resp.usage["prompt_tokens"]
            ls.last_usage = dict(resp.usage)
            ls.compacted = False
            await ls.rctx.emit_event("context_usage", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "usage": resp.usage,
            })

        # ---- 重试判定 ----
        _retry_reason = ""
        if not resp.ok and _is_retryable_error(resp):
            _retry_reason = resp.error or "unknown"
        elif resp.ok and not resp.tool_calls and not (resp.text or "").strip():
            _retry_reason = "empty_response"

        if _retry_reason and _retry_attempt < ls.retry_max:
            _retry_attempt += 1
            _delay = min(
                ls.retry_initial_delay * (ls.retry_backoff ** (_retry_attempt - 1)),
                ls.retry_max_delay,
            )
            await ls.rctx.emit_event("llm_retry", {
                "node_id": ls.node.id,
                "step": step,
                "attempt": _retry_attempt,
                "max_retries": ls.retry_max,
                "delay_sec": round(_delay, 2),
                "error": _retry_reason,
                "status_code": resp.status_code,
            })
            _waited = 0.0
            while _waited < _delay:
                _sleep_step = min(0.5, _delay - _waited)
                await asyncio.sleep(_sleep_step)
                _waited += _sleep_step
                if await ls.rctx.check_cancelled():
                    await ls.rctx.emit_event("cancel_acknowledged", {
                        "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
                    })
                    return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")
                if not ls.preempt_after_step and ls.preempt_inject_info is None:
                    _pi_r = await ls.rctx.check_preempted()
                    if _pi_r.get("preempted"):
                        if _pi_r.get("message"):
                            ls.preempt_inject_info = _pi_r
                        else:
                            ls.preempt_after_step = True
            resp = None
            continue  # 重试

        break  # 成功或不可重试，退出重试循环

    return resp


def _build_failure_action(ls: _LoopState, resp, step: int, retry_attempt: int = 0) -> TaskAction:
    """LLM 调用失败（重试耗尽），构建 FAIL action。"""
    _fail_msg = resp.error or "LLM 调用失败"
    if retry_attempt > 0:
        _fail_msg = f"{_fail_msg} (已重试 {retry_attempt} 次)"
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error=_fail_msg,
        context_ref=ctx_ref,
        summary=_short(_fail_msg, 240),
    )
