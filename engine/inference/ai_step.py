from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry
from toolbox.skills_runtime import build_skill_messages
from providers.openai import OpenAIProvider
from ..memory import build_memory_messages

from .pseudo_tools import (
    _PSEUDO_TOOL_NAMES,
    _dispatch_node_spec,
    _dispatch_nodes_spec,
    _finish_spec,
    _reply_spec,
    _compact_context_spec,
    _preempt_task_spec,
    _switch_node_spec,
    _to_openai_tools,
    _filter_tool_specs,
)
from .dynamic_context import _load_dynamic_context_vars, _format_context_vars_block
from .resume_builder import _build_resume_messages
from .loop_state import _LoopState, _persist_ctx, _short
from .llm_call import _call_llm_with_retry, _build_failure_action, _is_retryable_error, _RETRYABLE_STATUS_CODES
from .pseudo_handlers import _handle_pseudo_tool

from ..context_store import load_context_snapshot
from ..attachments import build_multimodal_content
# Phase 1 (Session Conversation Store): 导入 Message 模型用于影子写入。
# ai_step 在每次 append assistant/tool_result 消息后，best-effort 写入 ConversationStore。
from ..conversation_store import ConversationStore, Message, MessageType
from ..node import Node
from .message_assembly import assemble_initial_messages
from ..protocol import (
    TaskAction,
    ACTION_DISPATCH,
    ACTION_FINISH,
    ACTION_FAIL,
    ACTION_CANCELLED,
    ACTION_PREEMPTED,
)
# [2026-04-17] write_artifact 移除：截断机制已废弃，不再写 artifact 文件
from ..tool_step import result_to_raw, summarize_result
# [2026-04-24] P1.5 熔断器：新增 record_compact_failure, record_compact_success, is_compact_circuit_open
# 用于在连续压缩失败时跳过自动压缩，避免浪费 API 调用。
from ..compact import should_compact, _format_messages_for_summary, record_compact_failure, record_compact_success, is_compact_circuit_open
from clonoth_runtime import get_int, get_float, load_runtime_config
from toolbox.context import ToolContext
# build_llm_messages: 反序列化方向的格式转换，在 llm_call.py 中实际调用。
# 此处导入供外部通过 ai_step 模块访问（如测试、调试）。
from .tool_format import create_tool_formatter, build_llm_messages
from .message_model import MessageMeta, set_message_meta
from providers.base import ToolCall, ProviderResponse
# Phase 2 Signal System: 导入信号总线，用于发射 tool.call 和 task.error 信号。
# get_bus() 返回全局单例 SignalBus，Signal 是不可变事件数据类。
from ..signals import Signal, get_bus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..context import RunContext


# ---------------------------------------------------------------------------
#  异步工具跟踪表（Async Tool Tracking）
#  [2026-04-23] 从 commit 7d10197 恢复，在 864a333 大扫除中被误删。
#  key = async_tool_id (8 位 hex), value = 状态字典
#  用于关联异步工具的启动占位消息与 preempt 回传结果。
#  done/failed 条目保留 5 分钟后自动清理，防止无限增长。
# ---------------------------------------------------------------------------
_async_tool_tasks: dict[str, dict] = {}

# 清理阈值：done/failed 条目保留秒数
_ASYNC_TRACK_RETAIN_SEC = 300  # 5 minutes


def _cleanup_async_tracker() -> None:
    """清理已完成超过 _ASYNC_TRACK_RETAIN_SEC 的条目。

    在每次新增 tracking 条目时调用，避免 map 无限增长。
    只清理 status 为 done 或 failed 且 finished_at 已过期的条目。
    """
    now = time.monotonic()
    expired = [
        k for k, v in _async_tool_tasks.items()
        if v.get("status") in ("done", "failed")
        and now - v.get("finished_at", now) > _ASYNC_TRACK_RETAIN_SEC
    ]
    for k in expired:
        del _async_tool_tasks[k]


def get_async_tool_tasks() -> list[dict]:
    """导出当前所有异步工具跟踪条目，供外部查询。

    返回列表，每项包含 async_id, tool_name, status, elapsed 等字段。
    """
    result = []
    now = time.monotonic()
    for aid, info in _async_tool_tasks.items():
        entry = {"async_id": aid, **info}
        # 对 running 状态补算已经过的时间
        if info.get("status") == "running" and "started_at" in info:
            entry["elapsed"] = round(now - info["started_at"], 1)
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
#  Phase 1: 影子写入辅助函数
#  将 ls.messages.append() 产生的消息同步写入 ConversationStore（JSONL）。
#  best-effort：任何异常静默忽略，绝不影响主推理流程。
#  仅处理 assistant 和 tool_result 消息；_dynamic/_ephemeral 消息跳过。
# ---------------------------------------------------------------------------

def _shadow_write(ls: _LoopState, msg_dict: dict, message_type: str = "") -> None:
    """Best-effort shadow write to ConversationStore. Never breaks main flow.

    Phase 3: 写入成功后将 Message.id 记录到 ls.last_shadow_message_id，
    供 _persist_ctx 写入 snapshot 的 last_message_id 字段。

    Child Session 隔离（Phase B）：写入目标优先使用 rctx.child_session_id，
    使子节点的消息写入自己的 JSONL 而非父 session。
    """
    try:
        store = getattr(ls.rctx, 'conversation_store', None)
        if store is None:
            return
        # 跳过 dynamic context 和 ephemeral 消息（如 retry hint）
        if msg_dict.get('_dynamic') or msg_dict.get('_ephemeral'):
            return
        from uuid import uuid4
        from datetime import datetime, timezone
        msg_id = str(uuid4())
        msg = Message(
            id=msg_id,
            role=msg_dict.get('role', 'user'),
            content=msg_dict.get('content', '') if isinstance(msg_dict.get('content'), str) else str(msg_dict.get('content', '')),
            message_type=message_type,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=msg_dict.get('_meta', {}),
            source_node_id=getattr(ls.node, 'id', ''),
            source_task_id=getattr(ls.rctx, 'task_id', ''),
            tool_calls=msg_dict.get('tool_calls', []),
        )
        # Child Session 隔离（Phase B）：子节点写入自己的 child session JSONL，
        # 不再写入父 session。无 child_session_id 时仍写入 parent session（主节点路径）。
        target_session = getattr(ls.rctx, 'child_session_id', '') or ls.rctx.session_id
        store.append(target_session, msg)
        # Phase 3: 记录最后一次影子写入的 message id，供 snapshot 持久化使用
        ls.last_shadow_message_id = msg_id
        # P0 Task 内核化：追踪 first/last message ID 到 RunContext
        if not ls.rctx.first_shadow_message_id:
            ls.rctx.first_shadow_message_id = msg_id
        ls.rctx.last_shadow_message_id = msg_id
    except Exception:
        pass  # best-effort, never break main flow


# ---------------------------------------------------------------------------
#  推理循环子函数
# ---------------------------------------------------------------------------

async def _check_preempt_and_cancel(ls: _LoopState, step: int) -> TaskAction | None:
    """循环顶部：取消检查 + preempt 检查。返回 TaskAction 则退出循环。"""
    if await ls.rctx.check_cancelled():
        await ls.rctx.emit_event("cancel_acknowledged", {
            "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
        })
        return TaskAction(action=ACTION_CANCELLED, node_id=ls.node.id, summary="任务已被用户取消。")

    if not ls.preempt_after_step and ls.preempt_inject_info is None:
        _pi = await ls.rctx.check_preempted()
        if _pi.get("preempted"):
            if _pi.get("message"):
                ls.preempt_inject_info = _pi
            else:
                ls.preempt_after_step = True
                await ls.rctx.emit_event("preempt_acknowledged", {
                    "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
                })
    return None


async def _inject_preempt_message(ls: _LoopState, step: int) -> None:
    """Preempt V2：如果有待注入的 preempt 消息，原地注入并重置状态。"""
    if ls.preempt_inject_info is None:
        return

    _new_instruction = ls.preempt_inject_info.get("message", "")
    _new_atts = ls.preempt_inject_info.get("attachments", [])

    # 1. 剥离旧 dynamic context 消息
    ls.messages = [m for m in ls.messages if not m.get("_dynamic")]

    # 2. 重建 dynamic context（skill + memory 的 dynamic 部分）
    from .message_assembly import _conversational_history
    _scan_history = _conversational_history(ls.history)
    _inj_skill_s, _inj_skill_d = build_skill_messages(
        ls.rctx.workspace_root,
        node_id=ls.node.id,
        instruction_text=_new_instruction,
        history=_scan_history,
        skill_mode=ls.node.skill_access.mode,
        skill_allow=ls.node.skill_access.allow,
        max_budget_chars=get_int(ls.runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
    )
    if ls.node.memory_access.mode == "none":
        _inj_mem_d = []
    else:
        _inj_mem_s, _inj_mem_d = build_memory_messages(
            ls.rctx.workspace_root,
            node_id=ls.node.id,
            instruction_text=_new_instruction,
            history=_scan_history,
            max_budget_chars=get_int(ls.runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
            memory_mode=ls.node.memory_access.mode,
            memory_allow=ls.node.memory_access.allow,
        )

    _inj_parts: list[str] = []
    if not ls.is_block_mode and len(ls.system_prompt) >= 2 and ls.system_prompt[1].get("content"):
        _inj_parts.append(ls.system_prompt[1]["content"])
    for _dm in _inj_skill_d:
        if _dm.get("content"):
            _inj_parts.append(_dm["content"])
    for _dm in _inj_mem_d:
        if _dm.get("content"):
            _inj_parts.append(_dm["content"])

    if _inj_parts:
        _dyn_prefix = (
            "以下是本轮动态上下文，每轮可能变化。\n\n"
            if ls.is_block_mode
            else "以下是本轮动态上下文信息，每轮可能变化。如与当前任务无关可忽略，继续之前的工作即可。\n\n"
        )
        ls.messages.append({
            "role": "user",
            "content": _dyn_prefix + "\n\n".join(_inj_parts),
            "_dynamic": True,
        })

    # 3. 注入新 user message
    if _new_atts:
        ls.messages.append({"role": "user", "content": build_multimodal_content(
            _new_instruction, _new_atts, workspace_root=ls.rctx.workspace_root,
        )})
    else:
        ls.messages.append({"role": "user", "content": _new_instruction})

    # 4. 通知 supervisor 已消费
    await ls.rctx.consume_preempt()
    await ls.rctx.emit_event("preempt_injected", {
        "node_id": ls.node.id, "task_id": ls.rctx.task_id, "step": step,
    })

    # 5. 重置状态
    ls.preempt_inject_info = None
    ls.plaintext_retry_count = 0
    ls.compacted = False


async def _check_and_compact(ls: _LoopState, step: int) -> TaskAction | None:
    """上下文压缩检查。如需压缩则返回 DISPATCH action。"""
    if ls.compacted or ls.compact_threshold <= 0:
        return None
    # [2026-04-24] P1.5 熔断器：连续压缩失败达到阈值时跳过自动压缩
    if is_compact_circuit_open(ls.rctx.session_id):
        return None
    if not should_compact(ls.messages, ls.compact_threshold, ls.last_prompt_tokens):
        return None

    # ---------------------------------------------------------------
    # P6 Snip Compact (Level 2): 用已有轮摘要替换旧 task 消息链
    # 在 dispatch LLM compactor 前先尝试，可能免去 LLM 调用
    # ---------------------------------------------------------------
    try:
        from engine.task_record import snip_history, snip_store, load_task_records
        _snip_sid = ls.rctx.child_session_id or ls.rctx.session_id
        _snip_records = load_task_records(ls.rctx.workspace_root, _snip_sid)
        if _snip_records:
            # Incremental: snip a few oldest tasks per trigger
            _snipped, _snip_count, _snipped_ids = snip_history(
                ls.messages, _snip_records,
            )
            if _snip_count > 0:
                ls.messages = _snipped
                # Persist to ConversationStore so next load sees snipped version
                _store = getattr(ls.rctx, 'conversation_store', None)
                if _store:
                    try:
                        _stored = _store.load(_snip_sid)
                        _persisted = snip_store(_stored, _snip_records, _snipped_ids)
                        _store.replace_all(_snip_sid, _persisted)
                    except Exception as _pe:
                        logger.warning("failed to persist snipped history: %s", _pe)
                await ls.rctx.emit_event("snip_compact", {
                    "node_id": ls.node.id, "step": step,
                    "snipped_tasks": _snip_count,
                })
                # Snipped something → done for this round, continue task
                logger.info("snip_compact: replaced %d tasks, skipping LLM compact", _snip_count)
                ls.compacted = True
                return None
            # snip_count == 0 → all eligible already snipped, fall through to LLM
    except Exception as _snip_err:
        logger.warning("snip_compact failed, falling through to LLM compact: %s", _snip_err)

    ls.compacted = True
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": step})
        conversation_text = _format_messages_for_summary(
            [m for m in ls.messages if m.get("role") != "system" and not m.get("_dynamic")]
        )
        # ---------------------------------------------------------------
        # P5b PTL Retry: 压缩请求本身过长时截断
        # compactor 节点也有模型上下文上限，如果待压缩的对话文本超过
        # 这个上限，压缩请求自身就会 413。这里在发送前做预截断：
        # 保留尾部（最近的对话），丢弃头部（最旧的部分），并对齐到
        # 消息分隔符边界，避免截断产生不完整消息。
        # ~100K tokens ≈ 300K chars（按 3 字符/token 估算）
        # ---------------------------------------------------------------
        _ptl_max_chars = 300000
        if len(conversation_text) > _ptl_max_chars:
            _ptl_original_len = len(conversation_text)
            conversation_text = conversation_text[-_ptl_max_chars:]
            # 找到第一个完整消息边界（--- 分隔符），丢弃截断的不完整消息
            _first_sep = conversation_text.find("\n\n---\n\n")
            if _first_sep > 0:
                conversation_text = conversation_text[_first_sep + len("\n\n---\n\n"):]
            await ls.rctx.emit_event("ptl_truncated", {
                "node_id": ls.node.id, "step": step,
                "original_chars": _ptl_original_len,
            })
        if conversation_text.strip():
            ctx_ref = _persist_ctx(ls, step)
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
    except Exception as compact_err:
        # [2026-04-24] P1.5 熔断器：记录压缩失败，累计达阈值后自动跳过
        record_compact_failure(ls.rctx.session_id)
        await ls.rctx.emit_event("compact_failed", {
            "node_id": ls.node.id, "step": step, "error": str(compact_err),
        })
    return None


def _estimate_context_tokens(ls: _LoopState) -> int:
    """估算当前上下文的真实 token 数。

    方案 B：优先使用 last_prompt_tokens + last_completion_tokens（LLM 真实报告值）。
    如果没有（如 compact 恢复后），遍历 messages 累加每条消息的 token 数：
    - assistant 消息优先读 _meta.usage.completion_tokens
    - 其他消息用 char-based 估算（len / 3）
    """
    # 优先用 LLM 真实报告的 usage（最准确）
    if ls.last_usage:
        pt = ls.last_usage.get("prompt_tokens", 0) or 0
        ct = ls.last_usage.get("completion_tokens", 0) or 0
        if pt > 0:
            return pt + ct

    # Fallback: 逐条消息估算
    total = 0
    for m in ls.messages:
        if m.get("_dynamic") or m.get("_ephemeral"):
            continue
        meta = m.get("_meta", {})
        if isinstance(meta, dict):
            usage = meta.get("usage", {})
            if isinstance(usage, dict):
                ct = usage.get("completion_tokens", 0)
                if ct and isinstance(ct, int) and ct > 0:
                    total += ct
                    continue
        # char-based fallback
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content) // 3
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"]) // 3
    return total


def _update_dynamic_vars(ls: _LoopState) -> None:
    """每步更新 dynamic context 变量（beijing_time, context_utilization 等）。"""
    _estimated_tokens = _estimate_context_tokens(ls)
    _dyn_vars = _load_dynamic_context_vars(
        ls.rctx.workspace_root,
        task_context=ls.rctx.task_context,
        session_id=ls.rctx.session_id,
        node_id=ls.node.id,
        prompt_tokens=_estimated_tokens,
        compact_threshold=ls.compact_threshold,
    )
    if not _dyn_vars:
        return
    _vars_text = _format_context_vars_block(_dyn_vars)
    if not _vars_text:
        return
    for _msg in ls.messages:
        if _msg.get("_dynamic"):
            _old = _msg.get("content", "")
            _s = _old.find("\n\n[CONTEXT_VARS]\n")
            if _s >= 0:
                _e = _old.find("\n[/CONTEXT_VARS]", _s)
                if _e >= 0:
                    _msg["content"] = _old[:_s] + _vars_text + _old[_e + len("\n[/CONTEXT_VARS]"):]
                else:
                    _msg["content"] = _old[:_s] + _vars_text
            else:
                _msg["content"] = _old + _vars_text
            break


# ---------------------------------------------------------------------------
#  工具调用处理
# ---------------------------------------------------------------------------

async def _handle_tool_calls(ls: _LoopState, resp, step: int) -> TaskAction | None:
    """处理 tool_calls（伪工具 + 真工具）。

    返回 TaskAction 则退出循环；返回 None 则 continue 到下一轮。
    """
    # 【方案 A 重构】伪工具改为列表按序执行，不再是 last-wins
    # 原本单标量 `pseudo_call = tc` 会导致同轮多伪工具只有最后一个生效，
    # 在 Fix 2（JSON 自由正文反向包装为 reply）后尤其危险——
    # 例如 `dispatch_node + 自由正文` 会被吞掉 dispatch 只留 reply。
    # 现改为按 LLM 输出顺序收集所有伪工具，后续按序处理，
    # 遇到返回 TaskAction 的（finish / switch_node / dispatch 等）立即退出循环，
    # 返回 None 的（reply / compact_context / preempt_task）继续执行下一个。
    pseudo_calls: list = []
    real_tool_calls: list[dict[str, Any]] = []
    for tc in resp.tool_calls:
        if tc.name in _PSEUDO_TOOL_NAMES:
            pseudo_calls.append(tc)
        else:
            # 【Fix】真工具权限校验：工具必须在节点的授权列表内才能执行
            if tc.name not in ls.allowed_real_tools:
                logger.warning("node %s attempted unauthorized tool call: %s (allowed: %s)",
                               ls.node.id, tc.name, ls.allowed_real_tools)
                _err_msg = ls.formatter.format_tool_result(
                    tc,
                    f"Error: Tool '{tc.name}' is not in this node's allowed tool list. "
                    f"Use finish() to provide your output directly.",
                )
                ls.messages.append(_err_msg)
                _shadow_write(ls, _err_msg, message_type="tool_result")
                continue

            real_tool_calls.append({
                "id": tc.id,
                "name": tc.name,
                "arguments": dict(tc.arguments or {}),
            })
            # P0 Task 内核化：记录工具调用摘要
            _args_str = str(tc.arguments or {})[:200]
            ls.rctx.tool_call_log.append({"name": tc.name, "args_summary": _args_str})

    # 将 LLM 的工具调用决策追加到对话历史
    _assistant_msg = ls.formatter.build_assistant_message(resp, resp.text or "", resp.tool_calls)
    # [refactor 2026-04-18] raw_parts → metadata, thinking_text → reasoning, has_thinking → has_reasoning
    # provider_meta 由 ProviderResponse 透传；engine 只搬运不解读
    # [fix 2026-04-18] provider 名称改为动态获取，不再硬编码 "openai"。
    # ls.provider.name 由 BaseProvider.name 提供，各 provider 子类在初始化时传入。
    _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
    _tc_meta = MessageMeta(
        provider=_provider_name,
        tool_mode=getattr(ls.node, 'tool_mode', 'native'),
        message_type="assistant",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={_provider_name: resp.provider_meta} if resp.provider_meta else {},
        tool_call_ids=[tc.id for tc in (resp.tool_calls or [])],
        reasoning=resp.reasoning or "",
        has_reasoning=bool(resp.reasoning),
        inline_data=resp.inline_data or [],
        usage=dict(ls.last_usage) if ls.last_usage else {},
    )
    set_message_meta(_assistant_msg, _tc_meta)
    ls.messages.append(_assistant_msg)
    # Phase 1: 影子写入 assistant 消息到 ConversationStore
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

    # 正文处理策略（JSON / Fake Native / Native 模式统一）：
    # 工具调用伴随的自由正文不发送给用户，也不合成为 reply 工具调用。
    # 正文通过 build_assistant_message 保留在 assistant 消息的 content 字段中，
    # LLM 下一轮能看到自己说过的话，但用户看不到。
    # 用户可见的输出仅通过 finish / reply 伪工具产生。
    # 纯文本重试逻辑（_handle_plaintext_response）保留，仅覆盖「完全没有任何工具调用」的分支。

    # 处理伪工具（finish 延后到真实工具之后，确保同轮真实工具不被跳过）
    _finish_call = None
    if pseudo_calls:
        for _pc in pseudo_calls:
            if _pc.name == "finish":
                _finish_call = _pc
                continue  # finish 延后执行
            action = await _handle_pseudo_tool(ls, _pc, step)
            if action is not None:
                # 其他终止型伪工具（switch_node / dispatch_node 等）仍然立刻退出
                return action
            # 非终止型（reply / compact_context / preempt_task）继续

    # 处理真实工具
    if real_tool_calls:
        action = await _execute_real_tools(ls, real_tool_calls, step)
        if action is not None:
            return action
        ls.use_stream = ls.streaming

        if ls.preempt_after_step:
            ctx_ref = _persist_ctx(ls, step + 1)
            return TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
            )

    # finish 最后执行（同轮真实工具已完成）
    if _finish_call:
        # ---------------------------------------------------------------
        # Preempt V3 需求2: finish 拦截
        # 在执行 finish 之前再次检查 preempt 状态。如果有待注入的 preempt
        # 消息（用户在 LLM 推理/工具执行期间发了新消息），拦截 finish：
        # 不产生 TaskAction(FINISH)，改为塞一个假 tool_result 维持 native
        # 模式下 tool_use/tool_result 的配对完整性（Claude API 强校验），
        # 然后让主循环继续——下一轮 _inject_preempt_message 注入新用户消息。
        #
        # 同时补全 V2 遗漏：preempt_after_step（无消息 preempt）在只有 finish
        # 没有真工具的场景下也需要被检查，此前会跳过导致 finish 照常执行。
        # ---------------------------------------------------------------
        if ls.preempt_inject_info is None and not ls.preempt_after_step:
            _pi_finish = await ls.rctx.check_preempted()
            if _pi_finish.get("preempted"):
                if _pi_finish.get("message"):
                    ls.preempt_inject_info = _pi_finish
                else:
                    ls.preempt_after_step = True

        if ls.preempt_inject_info is not None:
            # 有消息的 preempt：拦截 finish，塞假 tool_result，任务继续
            from .tool_format import ParsedToolCall as _FinishPTC
            _finish_parsed = _FinishPTC(
                id=getattr(_finish_call, "id", "") or "",
                name="finish",
                arguments=dict(_finish_call.arguments or {}),
            )
            _intercept_msg = ls.formatter.format_tool_result(
                _finish_parsed,
                "\u26a0\ufe0f Preempted: new user input received. Task continues.",
            )
            set_message_meta(_intercept_msg, MessageMeta(message_type="tool_result"))
            ls.messages.append(_intercept_msg)
            _shadow_write(ls, _intercept_msg, MessageType.TOOL_RESULT)
            await ls.rctx.emit_event("preempt_finish_intercepted", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": step,
            })
            # 不 return TaskAction — 函数返回 None，主循环 continue 到下一轮
        elif ls.preempt_after_step:
            # 无消息的 preempt：与真工具后的 preempt_after_step 路径对齐，
            # 保存上下文后退出任务
            ctx_ref = _persist_ctx(ls, step + 1)
            return TaskAction(
                action=ACTION_PREEMPTED, node_id=ls.node.id,
                context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
            )
        else:
            action = await _handle_pseudo_tool(ls, _finish_call, step)
            if action is not None:
                return action

    # 无终止型动作 → 继续下一轮推理
    if pseudo_calls or real_tool_calls:
        ls.use_stream = ls.streaming
    return None


# ---------------------------------------------------------------------------
#  异步工具后台执行器
#  [2026-04-23] 从 commit 7d10197 恢复，在 864a333 大扫除中被误删。
#  当工具 spec 标记 async_mode=True 时，工具在后台 asyncio.Task 中执行，
#  完成后通过 preempt API 将结果注入回当前任务的对话流。
# ---------------------------------------------------------------------------

async def _run_async_tool(
    registry: ToolRegistry,
    http: "httpx.AsyncClient",
    supervisor_url: str,
    task_id: str,
    session_id: str,
    tool_name: str,
    tool_args: dict,
    tool_ctx: ToolContext,
    async_tool_id: str,
) -> None:
    """后台执行异步工具，完成后通过 session 级 API 注入结果。"""
    _started = time.monotonic()
    try:
        _args_summary = _short(json.dumps(tool_args, ensure_ascii=False, default=str), 200)
        with get_bus().span('tool.call', payload={'tool': tool_name, 'args_summary': _args_summary, 'async': True}):
            result = await registry.execute(name=tool_name, arguments=tool_args, ctx=tool_ctx)

        _elapsed = time.monotonic() - _started
        _summary = summarize_result(tool_name, result)
        _fmt, raw = result_to_raw(tool_name, result)

        _async_tool_tasks[async_tool_id] = {
            "tool_name": tool_name,
            "status": "done",
            "task_id": task_id,
            "started_at": _started,
            "finished_at": time.monotonic(),
            "elapsed": round(_elapsed, 1),
        }

        preempt_text = (
            f'\u2705 Async tool "{tool_name}" (id: {async_tool_id}) completed in {_elapsed:.1f}s.'
            f'\nSummary: {_summary}\nResult:\n{raw}'
        )

        attachments: list[str] = []
        if isinstance(result, dict) and isinstance(result.get("attachments"), list):
            for a in result["attachments"]:
                if isinstance(a, dict) and a.get("path"):
                    attachments.append(str(a["path"]))
                elif isinstance(a, str):
                    attachments.append(a)

        payload: dict = {"message": preempt_text}
        if attachments:
            payload["attachment_paths"] = attachments

        # 使用 session 级 API，复用三级回退逻辑
        await http.post(
            f"{supervisor_url}/v1/sessions/{session_id}/async_tool_result",
            json=payload,
        )
    except Exception as e:
        _async_tool_tasks[async_tool_id] = {
            "tool_name": tool_name,
            "status": "failed",
            "task_id": task_id,
            "started_at": _started,
            "finished_at": time.monotonic(),
            "elapsed": round(time.monotonic() - _started, 1),
            "error": str(e),
        }
        try:
            await http.post(
                f"{supervisor_url}/v1/sessions/{session_id}/async_tool_result",
                json={"message": f'\u274c Async tool "{tool_name}" (id: {async_tool_id}) failed: {e}'},
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  真实工具执行
# ---------------------------------------------------------------------------

async def _execute_real_tools(
    ls: _LoopState, real_tool_calls: list[dict[str, Any]], step: int,
) -> TaskAction | None:
    """批量执行真实工具调用，将结果追加到 messages。"""
    # [2026-04-17] 移除 _max_inline / _rt_cfg：截断机制已废弃，不再需要读取 runtime config 中的
    # engine.tool_trace.max_inline_chars 配置项。

    await ls.rctx.emit_event("handoff_progress", {
        "message": f"[{ls.node.id}] 执行 {len(real_tool_calls)} 个工具",
        "node_id": ls.node.id,
        "task_id": ls.rctx.task_id,
    })

    _tool_ctx = ToolContext(
        supervisor_url=ls.rctx.supervisor_url,
        session_id=ls.rctx.session_id,
        run_id=ls.rctx.task_id or ls.run_id or ls.node.id,
        worker_id=ls.rctx.worker_id,
        workspace_root=ls.rctx.workspace_root,
        http=ls.rctx.http,
        registry=ls.registry,
        task_id=ls.rctx.task_id,
        session_generation=ls.rctx.session_generation,
    )

    _tool_entries: list[dict[str, Any]] = []
    _tool_atts: list[dict[str, Any]] = []

    for _rtc in real_tool_calls:
        if await ls.rctx.check_cancelled():
            break
        _t_name = _rtc["name"]
        _t_args = _rtc["arguments"]

        # [2026-04-23] 异步工具分流：查询 spec 判断该工具是否为 async_mode。
        # 若是，则在后台 asyncio.Task 中执行，不阻塞当前推理循环，
        # 结果通过 preempt API 异步回传。从 commit 7d10197 恢复。
        _spec = ls.registry.get_spec(_t_name)
        _is_async = _spec.get("async_mode", False) if _spec else False

        if _is_async:
            _cleanup_async_tracker()
            _async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[_async_id] = {
                "tool_name": _t_name,
                "status": "running",
                "started_at": time.monotonic(),
                "task_id": ls.rctx.task_id,
            }
            asyncio.create_task(
                _run_async_tool(
                    registry=ls.registry,
                    http=ls.rctx.http,
                    supervisor_url=ls.rctx.supervisor_url,
                    task_id=ls.rctx.task_id,
                    session_id=ls.rctx.session_id,
                    tool_name=_t_name,
                    tool_args=_t_args,
                    tool_ctx=_tool_ctx,
                    async_tool_id=_async_id,
                ),
                name=f"async_tool_{_t_name}_{_async_id}",
            )
            _tool_entries.append({
                "name": _t_name,
                "args": _t_args,
                "format": "text",
                "raw_inline": f'\u23f3 Async tool "{_t_name}" started (id: {_async_id}). Result will be delivered via preempt when ready.',
                "truncated": False,
                "ref": "",
                "summary": f"异步执行已启动 (id: {_async_id})，结果将通过 preempt 自动回传",
            })
            await ls.rctx.emit_event("handoff_progress", {
                "message": f"[{ls.node.id}] {_t_name}: 异步执行已启动",
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
            })
            continue

        # ---- 同步工具：阻塞等待执行完成（原有逻辑）----
        # Phase 2 Signal: tool.call span 包裹每个工具的执行过程。
        # 自动发射 tool.call.start（含工具名和参数摘要）和 tool.call.end（含 elapsed_ms 和 error）。
        # span 是同步 contextmanager，在 async 函数中直接 with 即可。
        _args_summary = _short(json.dumps(_t_args, ensure_ascii=False, default=str), 200)
        with get_bus().span('tool.call', payload={'tool': _t_name, 'args_summary': _args_summary}):
            _t_result = await ls.registry.execute(name=_t_name, arguments=_t_args, ctx=_tool_ctx)
        # [硬取消-场景1] 工具返回 cancelled 时，仍将结果存入 _tool_entries 再 break。
        # 确保 assistant 的 tool_use 有对应 tool_result 配对，
        # 模型下次看到的是「我调了工具但被用户取消了」而非 tool_use 悬空无响应。
        _t_cancelled = isinstance(_t_result, dict) and _t_result.get("cancelled")

        _t_summary = summarize_result(_t_name, _t_result)
        _t_fmt, _t_raw = result_to_raw(_t_name, _t_result)
        # [2026-04-17] 移除工具结果截断机制：不再截断、不再写 artifact，直接传完整结果。
        _t_raw_inline = _t_raw

        _tool_entries.append({
            "name": _t_name,
            "args": _t_args,
            "format": _t_fmt,
            "raw_inline": _t_raw_inline,
            "truncated": False,  # [2026-04-17] 截断机制已移除，保留字段兼容 format_tool_trace
            "ref": "",
            "summary": _t_summary,
        })

        # [硬取消-场景1] 已取消的工具结果已存入 entries（上方 append），不处理附件和进度事件，
        # 直接退出循环。未执行的后续工具被跳过（循环顶部 check_cancelled），不产生 tool_result。
        if _t_cancelled:
            break

        if isinstance(_t_result, dict) and isinstance(_t_result.get("attachments"), list):
            _tool_atts.extend(_t_result["attachments"])
            ls.collected_attachments.extend(_t_result["attachments"])
            ls.tool_produced_attachments.extend(_t_result["attachments"])

        await ls.rctx.emit_event("handoff_progress", {
            "message": f"[{ls.node.id}] {_t_name}: {_t_summary}",
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        })

    if _tool_entries:
        for _entry in _tool_entries:
            _result_body = _entry["raw_inline"]
            # [2026-04-17] 截断机制已移除，不再追加 truncated 提示
            _tool_msg = {
                "role": "user",
                "content": f'Tool result for "{_entry["name"]}":\n{_result_body}',
            }
            set_message_meta(_tool_msg, MessageMeta(message_type="tool_result"))
            ls.messages.append(_tool_msg)
            # Phase 1: 影子写入 tool_result 消息到 ConversationStore
            _shadow_write(ls, _tool_msg, MessageType.TOOL_RESULT)
        if _tool_atts:
            ls.messages.append({"role": "user", "content": build_multimodal_content(
                "以上工具执行产生了以下图片结果：", _tool_atts, workspace_root=ls.rctx.workspace_root,
            )})

    return None


# ---------------------------------------------------------------------------
#  纯文本响应处理
# ---------------------------------------------------------------------------

def _handle_plaintext_response(ls: _LoopState, resp, step: int) -> TaskAction | None:
    """处理纯文本响应（无 tool_calls）。"""
    text = (resp.text or "").strip()
    if not text:
        return None

    if ls.preempt_after_step:
        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_PREEMPTED, node_id=ls.node.id,
            context_ref=ctx_ref, summary="任务被软打断，上下文已保存。",
        )

    # ---- hybrid 模式：纯文本视为隐式 finish，直接投递给用户 ----
    # 不 reject、不重试，将裸文本包装为 ACTION_FINISH 返回。
    # result 中标记 implicit_finish=True，供事件日志/管理界面区分显式与隐式 finish。
    # 参见 RFC: data/rfc_hybrid_output_mode.md
    if getattr(ls.node, 'output_mode', 'tool_only') == 'hybrid':
        # 写入 assistant 消息到对话历史 + ConversationStore，与 _handle_tool_calls 对齐
        _assistant_msg = ls.formatter.build_assistant_message(resp, text, [])
        # [refactor 2026-04-18] 与 _handle_tool_calls 对齐：动态 provider 名、metadata/reasoning 新字段
        _provider_name = getattr(ls.provider, 'name', '') or 'unknown'
        _implicit_meta = MessageMeta(
            provider=_provider_name,
            tool_mode=getattr(ls.node, 'tool_mode', 'native'),
            message_type="assistant",
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={},
            tool_call_ids=[],
            reasoning="",
            has_reasoning=False,
            usage=dict(ls.last_usage) if ls.last_usage else {},
        )
        set_message_meta(_assistant_msg, _implicit_meta)
        ls.messages.append(_assistant_msg)
        _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

        ctx_ref = _persist_ctx(ls, step + 1)
        return TaskAction(
            action=ACTION_FINISH, node_id=ls.node.id,
            result={
                "text": text,
                "attachments": list(ls.tool_produced_attachments),
                "implicit_finish": True,
            },
            context_ref=ctx_ref,
            summary=_short(text, 240),
        )

    # ---- tool_only 模式：现有行为，reject 纯文本并重试 ----
    ls.plaintext_retry_count += 1
    if ls.plaintext_retry_count <= ls.plaintext_retry_max:
        _retry_hint = ls.formatter.build_retry_hint()
        ls.messages.append({
            "role": "user",
            "content": _retry_hint,
            "_retry_hint": True,
        })
        ls.use_stream = ls.streaming
        return None

    # 重试耗尽后：返回 FAIL 而非 FINISH
    # 引擎内核不认可裸正文作为合法结束，只有 finish 工具才能产生 ACTION_FINISH。
    # 将原先的 ACTION_FINISH 改为 ACTION_FAIL，error 中附带截断原始文本用于调试。
    ctx_ref = _persist_ctx(ls, step + 1)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error=f"模型未使用 finish 工具，裸文本不被内核认可为合法结束。原始文本: {_short(text, 200)}",
        context_ref=ctx_ref,
        summary="plaintext_without_finish",
    )


# ---------------------------------------------------------------------------
#  AI 节点主执行函数
# ---------------------------------------------------------------------------

async def run_ai_node(
    *,
    rctx: "RunContext",
    streaming: bool = False,
    provider: OpenAIProvider,
    registry: ToolRegistry,
    node: Node,
    instruction: str,
    history: list[dict[str, Any]],
    run_id: str = "",
    context_ref: str = "",
    resume_data: dict[str, Any] | None = None,
    downstream_info: list[dict[str, str]] | None = None,
    switch_info: list[dict[str, str]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> TaskAction:
    runtime_cfg = load_runtime_config(rctx.workspace_root)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 32, min_value=1, max_value=200)

    # ---- 收集附件 ----
    collected_attachments: list[dict[str, Any]] = []
    _tool_produced_attachments: list[dict[str, Any]] = []
    if attachments:
        collected_attachments.extend(attachments)
    if resume_data and isinstance(resume_data, dict):
        for e in (resume_data.get("tool_results") or resume_data.get("entries") or []):
            if isinstance(e, dict) and isinstance(e.get("attachments"), list):
                collected_attachments.extend(e["attachments"])
        if isinstance(resume_data.get("attachments"), list):
            collected_attachments.extend(resume_data["attachments"])
        rd = resume_data.get("result")
        if isinstance(rd, dict) and isinstance(rd.get("attachments"), list):
            collected_attachments.extend(rd["attachments"])

    # ---- 恢复或新建消息历史 ----
    step_count = 0
    _is_block_mode = False
    system_prompt: list[dict[str, Any]] = []
    snapshot = load_context_snapshot(rctx.workspace_root, context_ref) if context_ref else None
    if snapshot and isinstance(snapshot.get("messages"), list):
        messages = list(snapshot.get("messages") or [])
        try:
            step_count = int(snapshot.get("step_count") or 0)
        except Exception:
            step_count = 0
    else:
        messages, _is_block_mode, system_prompt = assemble_initial_messages(
            workspace_root=rctx.workspace_root,
            runtime_cfg=runtime_cfg,
            node=node,
            instruction=instruction,
            history=history,
            task_context=rctx.task_context,
            session_id=rctx.session_id,
            attachments=attachments,
        )

    # ---- 追加恢复消息 ----
    if resume_data:
        messages.extend(_build_resume_messages(resume_data))
        if str(resume_data.get("type") or "") == "compact_done":
            # [2026-04-24] P1.5 熔断器：压缩成功时重置失败计数
            record_compact_success(rctx.session_id)
            # Phase 2 Signal: compact.done 信号，通过 SignalBus 发射供监控使用
            get_bus().emit(Signal(name="compact.done", payload={
                "node_id": node.id,
                "success": resume_data.get("success", True),
                "before": resume_data.get("before", 0),
                "after": resume_data.get("after", 0),
            }))
            await rctx.emit_event("compact_done", {
                "node_id": node.id,
                "success": resume_data.get("success", True),
                "before": resume_data.get("before", 0),
                "after": resume_data.get("after", 0),
            })

    # ---- 构建工具列表 ----
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    _allowed_real_tools = {s.get("name") for s in tool_specs if s.get("name")}
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        openai_tools.append(_dispatch_node_spec(delegate_targets, downstream_info))
        openai_tools.append(_dispatch_nodes_spec(delegate_targets, downstream_info))

    _sw_targets = [info["id"] for info in (switch_info or [])]
    openai_tools.append(_switch_node_spec(_sw_targets, switch_info, current_node_id=node.id, current_node_name=node.name))

    openai_tools.append(_finish_spec())
    openai_tools.append(_reply_spec())
    openai_tools.append(_compact_context_spec())
    openai_tools.append(_preempt_task_spec())

    # ---- 工具定义注入（formatter 统一处理 native/json 差异）----
    formatter = create_tool_formatter(node.tool_mode)
    if openai_tools:
        for msg in messages:
            if msg.get("role") == "system":
                msg["content"], _api_tools = formatter.inject_tool_definitions(
                    openai_tools, msg.get("content", ""),
                )
                openai_tools = _api_tools or []
                break

    # ---- 构造循环状态 ----
    ls = _LoopState(
        rctx=rctx,
        node=node,
        provider=provider,
        registry=registry,
        run_id=run_id,
        context_ref=context_ref,
        runtime_cfg=runtime_cfg,
        streaming=streaming,
        messages=messages,
        system_prompt=system_prompt,
        is_block_mode=_is_block_mode,
        openai_tools=openai_tools,
        history=history,
        collected_attachments=collected_attachments,
        tool_produced_attachments=_tool_produced_attachments,
        formatter=formatter,
        allowed_real_tools=_allowed_real_tools,
        compact_threshold=get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0),
        compact_keep_recent=get_int(runtime_cfg, "engine.compact.keep_recent", 6, min_value=2, max_value=50),
        compacted=False,
        last_prompt_tokens=None,
        retry_max=get_int(runtime_cfg, "engine.retry.max_retries", 3, min_value=0, max_value=10),
        retry_initial_delay=get_float(runtime_cfg, "engine.retry.initial_delay_sec", 1.0, min_value=0.1, max_value=60.0),
        retry_max_delay=get_float(runtime_cfg, "engine.retry.max_delay_sec", 30.0, min_value=1.0, max_value=300.0),
        retry_backoff=get_float(runtime_cfg, "engine.retry.backoff_multiplier", 2.0, min_value=1.0, max_value=10.0),
        plaintext_retry_count=0,
        # 改动：plaintext retry 默认值从 2 → 3，与 retry_max（LLM 报错重试）对齐，
        # 给模型更多机会自行修正未调 finish 的问题。
        plaintext_retry_max=get_int(runtime_cfg, "engine.plaintext_retry_max", 3, min_value=0, max_value=10),
        preempt_after_step=False,
        preempt_inject_info=None,
        use_stream=streaming,
    )

    # ---- 推理循环 ----
    for step in range(step_count, max_steps):
        action = await _check_preempt_and_cancel(ls, step)
        if action is not None:
            return action

        await _inject_preempt_message(ls, step)

        # P1 Microcompact + Proactive Snip: 闲置时主动减压
        if step == step_count:  # 只在本轮第一步检查（避免循环内重复）
            from engine.compact import microcompact_messages
            _, _mc_cleared = microcompact_messages(ls.messages)
            if _mc_cleared:
                logger.info("microcompact: cleared %d tool_results", _mc_cleared)

            # Proactive Snip: 按闲置时长渐进式替换旧 task 为摘要
            # 每超过 1 小时替换 2 个，保留最近 3 个 task + 活跃 task
            try:
                from datetime import datetime as _dt, timezone as _tz
                from engine.task_record import snip_history, snip_store, load_task_records
                _last_ts = None
                for _m in reversed(ls.messages):
                    _mm = _m.get("_meta", {})
                    if isinstance(_mm, dict) and (_mm.get("message_type") == "assistant" or _m.get("role") == "assistant"):
                        _ts_str = _mm.get("timestamp", "")
                        if _ts_str:
                            try:
                                _last_ts = _dt.fromisoformat(_ts_str)
                                if _last_ts.tzinfo is None:
                                    _last_ts = _last_ts.replace(tzinfo=_tz.utc)
                            except Exception:
                                pass
                        break
                if _last_ts is not None:
                    _gap_hours = (_dt.now(_tz.utc) - _last_ts).total_seconds() / 3600.0
                    if _gap_hours >= 1.0:
                        _proactive_max = max(int(_gap_hours) * 2, 2)  # 2 per hour, no cap — keep_recent_tasks=3 is the floor
                        _snip_sid = ls.rctx.child_session_id or ls.rctx.session_id
                        _snip_records = load_task_records(ls.rctx.workspace_root, _snip_sid)
                        if _snip_records:
                            _snipped, _snip_count, _snipped_ids = snip_history(
                                ls.messages, _snip_records,
                                keep_recent_tasks=3, max_snip=_proactive_max,
                            )
                            if _snip_count > 0:
                                ls.messages = _snipped
                                _store = getattr(ls.rctx, 'conversation_store', None)
                                if _store:
                                    try:
                                        _persisted = snip_store(_store.load(_snip_sid), _snip_records, _snipped_ids)
                                        _store.replace_all(_snip_sid, _persisted)
                                    except Exception as _pe:
                                        logger.warning("proactive snip persist failed: %s", _pe)
                                logger.info("proactive snip: replaced %d tasks (gap=%.1fh, max=%d)", _snip_count, _gap_hours, _proactive_max)
            except Exception as _ps_err:
                logger.warning("proactive snip failed: %s", _ps_err)

        action = await _check_and_compact(ls, step)
        if action is not None:
            return action

        # _update_dynamic_vars(ls)  # Disabled to prevent intra-task prompt cache invalidation

        result = await _call_llm_with_retry(ls, step)
        if isinstance(result, TaskAction):
            return result
        # ---------------------------------------------------------------
        # Preempt V3 需求1: _call_llm_with_retry 返回 None 表示流式输出
        # 在思考阶段被 preempt 截断。partial assistant message 已丢弃（不存
        # 历史），preempt 消息已存储在 ls.preempt_inject_info 中。
        # 跳到下一轮循环顶部，由 _inject_preempt_message 注入新用户消息后
        # 重新推理。与 cancel 的区别：不终止 task，继续循环。
        # ---------------------------------------------------------------
        if result is None:
            continue
        resp = result

        # P0 Task 内核化：记录实际完成的步数
        ls.rctx.completed_steps = step + 1

        # P0 Task 内核化：累加 token 用量
        if resp.usage and isinstance(resp.usage, dict):
            for _uk in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if _uk in resp.usage:
                    ls.rctx.total_usage[_uk] = ls.rctx.total_usage.get(_uk, 0) + resp.usage[_uk]

        if not resp.ok:
            return _build_failure_action(ls, resp, step)

        # ---- 从文本中解析工具调用（formatter 统一处理）----
        if not resp.tool_calls:
            _parsed = formatter.parse_tool_calls(resp)
            if _parsed:
                resp = ProviderResponse(
                    ok=True,
                    text=formatter.get_plain_text(resp),
                    tool_calls=[
                        ToolCall(id=p.id, name=p.name, arguments=p.arguments)
                        for p in _parsed
                    ],
                    # [refactor 2026-04-18] thinking → reasoning
                    reasoning=resp.reasoning,
                    status_code=resp.status_code,
                    usage=resp.usage,
                )

        if resp.tool_calls:
            action = await _handle_tool_calls(ls, resp, step)
            if action is not None:
                return action
            continue

        action = _handle_plaintext_response(ls, resp, step)
        if action is not None:
            return action

    # ---- 达到最大步数 ----
    ctx_ref = _persist_ctx(ls, max_steps)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error="达到最大步数限制。",
        context_ref=ctx_ref,
        summary="max_steps reached",
    )
