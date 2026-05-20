from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry

from .pseudo_tools import (
    _dispatch_delegate_specs,
    _is_pseudo_tool_name,
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
from ..compact import (
    _format_messages_for_summary,
    count_real_task_segments,
    is_compact_circuit_open,
    record_compact_failure,
    record_compact_success,
    should_compact,
)
from clonoth_runtime import get_int, get_float, load_runtime_config
from toolbox.context import ToolContext
# build_llm_messages: 反序列化方向的格式转换，在 llm_call.py 中实际调用。
# 此处导入供外部通过 ai_step 模块访问（如测试、调试）。
from .tool_format import (
    ParsedToolCall,
    create_tool_formatter,
    build_llm_messages,
)
from .message_model import MessageMeta, set_message_meta
from providers.base import BaseProvider, ToolCall, ProviderResponse
# Phase 2 Signal System: 导入信号总线，用于发射 tool.call 和 task.error 信号。
# get_bus() 返回全局单例 SignalBus，Signal 是不可变事件数据类。
from ..signals import Signal, get_bus
# Phase 3 Hook System：引入 hook registry 与上下文对象。
# 原因：before_tool_call 的业务检查要从 ai_step.py 的硬编码分支迁出。
# 做法：ai_step 只负责构造 HookContext 并触发 registry；具体规则由 handler 实现。
# 目的：后续内核逻辑可以插件化注册，同时保持当前推理循环行为不变。
from ..hooks import HookContext, hook_registry
from ..builtin.loader import auto_discover_and_register
from ..hooks.loader import load_external_plugins

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
        # [2026-05-07] 不再按 control_tool_name 跳过 finish。
        # 原因：finish 已恢复为真实 API 工具，正常结果必须像普通工具一样进入 ConversationStore。
        # 做法：这里只保留 dynamic/ephemeral 两类运行期消息过滤，普通 tool_result 全部写入。
        # 目的：长期历史能够保存 assistant.tool_call 与 tool_result 的完整配对。
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
            # [2026-05-01] 影子写入时保留原生 role=tool 的配对字段。
            # 原因：ConversationStore 是下一轮历史来源；丢失 tool_call_id 会破坏 true native。
            tool_call_id=str(msg_dict.get('tool_call_id') or ''),
            name=str(msg_dict.get('name') or ''),
        )
        # [Fork/Merge 2026-05-12] Child sessions still win, otherwise write to the runtime session.
        # Why: rctx.session_id may now be an entry branch, not the user-facing parent session. How:
        # keep child_session_id for delegated nodes and use rctx.session_id for main branch tasks.
        # Purpose: ConversationStore writes stay isolated until supervisor merges the branch.
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


def _compact_target_session_id(ls: _LoopState) -> str:
    """Return the durable session that automatic compact should rewrite."""
    # [AutoC 2026-05-13] Why: this legacy helper may still be called by tests or
    # fallback paths while entry tasks run on branch sessions. How: prefer
    # parent_session_id and fall back to session_id. Purpose: keep legacy L2/L3/LLM
    # compact target selection aligned with the active hook implementation.
    return str(getattr(ls.rctx, "parent_session_id", "") or ls.rctx.session_id or "").strip()


async def _check_and_compact(ls: _LoopState, step: int) -> TaskAction | None:
    """上下文压缩检查。如需压缩则返回 DISPATCH action。"""
    if ls.compacted or ls.compact_threshold <= 0:
        return None
    compact_sid = _compact_target_session_id(ls)
    # [2026-04-24] P1.5 熔断器：连续压缩失败达到阈值时跳过自动压缩
    if is_compact_circuit_open(compact_sid):
        return None
    if not should_compact(ls.messages, ls.compact_threshold, ls.last_prompt_tokens):
        return None

    # ---------------------------------------------------------------
    # Pre-check: count task segments in ConversationStore. If there
    # are not enough segments to compress (≤ keep_recent), skip the
    # LLM compactor call entirely to avoid wasting API calls.
    # ---------------------------------------------------------------
    try:
        _conv_store = getattr(ls.rctx, 'conversation_store', None)
        if _conv_store:
            _stored_msgs = _conv_store.load(compact_sid)
            # [2026-05-17] Why: compact_summary is the prior compressed prefix,
            # not a real task segment. How: use the shared segment counter that
            # skips compact_summary and counts only consecutive real task ids.
            # Purpose: this legacy ai_step path stays aligned with builtin
            # compact and stops dispatching when only keep_recent real tasks remain.
            _seg_count = count_real_task_segments(_stored_msgs)
            if _seg_count <= ls.compact_keep_recent:
                logger.info(
                    "skip compact: only %d task segments (keep_recent=%d), not enough to compress",
                    _seg_count, ls.compact_keep_recent,
                )
                ls.compacted = True  # prevent retrigger this step
                return None
    except Exception as _seg_err:
        logger.warning("segment pre-check failed, proceeding with compact: %s", _seg_err)

    # ---------------------------------------------------------------
    # P6 Snip Compact (Level 2): 用已有轮摘要替换旧 task 消息链
    # 在 dispatch LLM compactor 前先尝试，可能免去 LLM 调用
    # ---------------------------------------------------------------
    try:
        from engine.task_record import (
            load_task_records,
            snip_history,
            snip_store,
        )
        # [AutoC 2026-05-13] Why: branch sessions are forked copies, so snipping
        # them does not reduce the durable parent history. How: use the same
        # parent-first target as LLM compact. Purpose: L2 and L3 compaction both
        # persist to the session future branches will fork from.
        _snip_sid = compact_sid
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

    except Exception as _snip_err:
        logger.warning("snip compact failed, falling through to LLM compact: %s", _snip_err)

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
                    "_compact_threshold_tokens": ls.compact_threshold,
                    # [AutoC 2026-05-13] Why: supervisor applies the compactor
                    # result after dispatch returns, and branch session ids are
                    # temporary. How: carry the parent-first target session in the
                    # dispatch input. Purpose: LLM compact rewrites the durable
                    # parent ConversationStore.
                    "target_session_id": compact_sid,
                    "_system_task": True,
                    "use_context": False,
                },
            )
    except Exception as compact_err:
        # [2026-04-24] P1.5 熔断器：记录压缩失败，累计达阈值后自动跳过
        # [AutoC 2026-05-13] Why: the circuit breaker should follow the session
        # we tried to compact, not the branch currently executing. How: record the
        # failure against compact_sid. Purpose: retry suppression matches the
        # parent ConversationStore target.
        record_compact_failure(compact_sid)
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
    # 例如“委派工具 + 自由正文”会被吞掉委派，只留 reply。
    # 现改为按 LLM 输出顺序收集所有伪工具，后续按序处理，
    # 遇到返回 TaskAction 的（finish / switch_node / dispatch 等）立即退出循环，
    # 返回 None 的（reply / compact_context / preempt_task）继续执行下一个。
    pseudo_calls: list = []
    real_tool_calls: list[dict[str, Any]] = []
    for tc in resp.tool_calls:
        # [2026-05-04] Dynamic per-target dispatch tools are pseudo tools too.
        # Why: names like dispatch:ereuna_coder are generated from delegate_targets
        # and must bypass real-tool authorization. How: use the prefix-aware helper
        # instead of a fixed name-only set. Purpose: route fixed-target dispatches
        # to pseudo_handlers without accepting removed aggregate dispatch tools.
        if _is_pseudo_tool_name(tc.name):
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
                # [2026-05-01] 工具结果必须带当前 tool_mode。
                # 目的：真 native 的 role=tool 消息在下一轮仍由 NativeToolFormatter 透传。
                set_message_meta(_err_msg, MessageMeta(
                    tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                    message_type="tool_result",
                ))
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
        tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
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
    # [2026-05-07] Store reasoning_content at top level for API round-trip.
    # DeepSeek V4 and similar models require this field in the message dict.
    # _meta.reasoning is kept for internal use but top-level survives L2 stripping.
    if resp.reasoning:
        _assistant_msg["reasoning_content"] = resp.reasoning
    ls.messages.append(_assistant_msg)
    # Phase 1: 影子写入 assistant 消息到 ConversationStore
    # [2026-05-07] 含 finish 的 assistant 消息也直接持久化。
    # 原因：finish 是真实 API tool_call，删除或拆分它会使后续 tool_result 失去配对来源。
    # 做法：不再调用 sanitize_assistant_control_tools，而是保存原始 assistant.tool_calls。
    # 目的：ConversationStore、snapshot 与 provider replay 的工具轮结构一致。
    _shadow_write(ls, _assistant_msg, MessageType.ASSISTANT)

    # 正文处理策略（JSON / Fake Native / Native 模式统一）：
    # 工具调用伴随的自由正文不发送给用户，也不合成为 reply 工具调用。
    # 正文通过 build_assistant_message 保留在 assistant 消息的 content 字段中，
    # LLM 下一轮能看到自己说过的话，但用户看不到。
    # 用户可见的输出仅通过 finish / reply 伪工具产生。
    # 纯文本重试逻辑（_handle_plaintext_response）保留，仅覆盖「完全没有任何工具调用」的分支。

    # ---- before_tool_call hook：本轮工具调用级检查 ----
    # Phase 3 Hook System：先触发 round-level hook，再进入伪工具和真实工具处理。
    # 原因：finish 并列检测这类业务规则不应继续硬编码在 ai_step.py。
    # 做法：把本轮所有 tool_calls 以及 legacy 过滤后的 pseudo/real 列表放进 HookContext。
    # 目的：handler 能复刻旧判断，同时后续可以继续迁移其他 before_tool_call 规则。
    _before_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step,
        response=resp,
        tool_calls=list(resp.tool_calls or []),
        extra={"pseudo_calls": pseudo_calls, "real_tool_calls": real_tool_calls},
    )
    _before_result = await hook_registry.afire("before_tool_call", _before_ctx)
    if _before_result.action is not None:
        return _before_result.action
    if _before_result.block:
        _reject_msg = (
            _before_result.error_message
            or _before_result.reason
            or "Tool call blocked by before_tool_call hook."
        )
        for tc in resp.tool_calls:
            _err = ls.formatter.format_tool_result(tc, _reject_msg)
            # [2026-05-07] 拒绝路径也按普通工具结果持久化。
            # 原因：finish_guard 产生的是对模型可见的错误 tool_result，若标记为 ephemeral，
            # 下一轮 provider 会看到 assistant.tool_call 缺少对应结果。
            # 做法：不再给 finish 错误结果设置 control_tool_name 或 _ephemeral。
            # 目的：被拒绝的 finish 与同轮其他工具一样保持完整配对历史。
            set_message_meta(_err, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
            ))
            ls.messages.append(_err)
            # [2026-05-07] before_tool_call 拒绝结果也要写入 ConversationStore。
            # 原因：这些结果是 assistant.tool_call 的真实回复，不是运行期占位消息。
            # 做法：与普通工具错误结果共用 _shadow_write。
            # 目的：模型重试时能看到完整的拒绝原因和工具配对。
            _shadow_write(ls, _err, message_type="tool_result")
        return None  # 不执行任何工具，回到主循环让 AI 重试
    if _before_result.skip_step:
        return None

    # LEGACY: replaced by hook FinishGuardHandler in engine.builtin.finish_guard.
    # 原因：保留原始硬编码判断，便于下一轮清理前核对行为。
    # 做法：只注释旧逻辑，不再执行；hook 使用 pseudo_calls/real_tool_calls 复刻同一判断。
    # 目的：迁移期间可快速回溯，不破坏当前 finish 并列拒绝语义。
    # _has_finish = any(_pc.name == "finish" for _pc in pseudo_calls)
    # _has_non_reply_others = bool(real_tool_calls) or any(
    #     _pc.name not in ("finish", "reply") for _pc in pseudo_calls
    # )
    # if _has_finish and _has_non_reply_others:
    #     _reject_msg = (
    #         "\u274c REJECTED: finish() cannot be called alongside other tools "
    #         "(except reply). Execute your other tools first, wait for their "
    #         "results, then call finish() alone in a separate turn."
    #     )
    #     logger.warning(
    #         "Rejected finish + other tools in same turn (node=%s, step=%d, tools=%s)",
    #         ls.node.id, step, [tc.name for tc in resp.tool_calls],
    #     )
    #     for tc in resp.tool_calls:
    #         _err = ls.formatter.format_tool_result(tc, _reject_msg)
    #         set_message_meta(_err, MessageMeta(
    #             tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
    #             message_type="tool_result",
    #         ))
    #         ls.messages.append(_err)
    #         _shadow_write(ls, _err, message_type="tool_result")
    #     return None  # 不执行任何工具，回到主循环让 AI 重试

    # 处理伪工具（finish 延后到真实工具之后，确保同轮真实工具不被跳过）
    _finish_call = None
    if pseudo_calls:
        for _pc in pseudo_calls:
            if _pc.name == "finish":
                _finish_call = _pc
                continue  # finish 延后执行
            action = await _handle_pseudo_tool(ls, _pc, step)
            if action is not None:
                # 其他终止型伪工具（如 switch_node）仍然立刻退出。
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
        # 然后让主循环继续，下一轮由 PreemptChecker 注入新用户消息。
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
            # [2026-05-01] 写入当前 tool_mode，避免真 native 的拦截结果被当作旧 fake-native。
            # [2026-05-07] preempt 拦截 ACK 只服务当前运行期配对。
            # 原因：该 finish 未交付，不能让 fake-native/json 的文本结果在恢复后压制未来正常 finish。
            # 做法：补齐 ephemeral、tool_call_id 和 name，让清洗函数按调用 ID 精确移除。
            # 目的：任务继续时不会向下一轮 provider 回放被拦截的 finish。
            _intercept_msg["_ephemeral"] = True
            if _finish_parsed.id:
                _intercept_msg.setdefault("tool_call_id", _finish_parsed.id)
            _intercept_msg.setdefault("name", "finish")
            set_message_meta(_intercept_msg, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                control_tool_name="finish",
                control_tool_status="preempt_intercepted",
            ))
            ls.messages.append(_intercept_msg)
            # [2026-05-07] 被新用户输入拦截的 finish 未交付，不能写入长期历史。
            # 原因：该 finish 没有产生最终交付，持久化会把未完成控制流带入下一轮。
            # 做法：只保留运行期消息，且不调用 _shadow_write。
            # 目的：下一轮提示由真实用户新输入驱动，而不是回放被拦截的 finish。
            await ls.rctx.emit_event("preempt_finish_intercepted", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "step": step,
            })
            # 不 return TaskAction — 函数返回 None，主循环 continue 到下一轮
        elif ls.preempt_after_step:
            # 无消息的 preempt：与真工具后的 preempt_after_step 路径对齐，
            # 保存上下文后退出任务
            # [2026-05-01] 补写 finish 的 tool_result，确保 native 模式下
            # functionCall/functionResponse 严格 1:1 配对（Gemini 强校验）
            from .tool_format import ParsedToolCall as _FinishPTC2
            _finish_parsed2 = _FinishPTC2(
                id=getattr(_finish_call, "id", "") or "",
                name="finish",
                arguments=dict(_finish_call.arguments or {}),
            )
            _preempt_result = ls.formatter.format_tool_result(
                _finish_parsed2, "preempted",
            )
            # [2026-05-07] 无消息 preempt 的 finish ACK 同样只保留在运行期。
            # 原因：保存上下文后恢复时不应看到 finish tool_call/tool_result；但本轮内存仍需满足 provider 配对。
            # 做法：设置 ephemeral，并补齐 tool_call_id/name 供 snapshot 清洗精确匹配。
            # 目的：preempt 快照只恢复真实对话，不恢复控制流占位结果。
            _preempt_result["_ephemeral"] = True
            if _finish_parsed2.id:
                _preempt_result.setdefault("tool_call_id", _finish_parsed2.id)
            _preempt_result.setdefault("name", "finish")
            set_message_meta(_preempt_result, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
                control_tool_name="finish",
                control_tool_status="preempted",
            ))
            ls.messages.append(_preempt_result)
            # [2026-05-07] 无消息 preempt 也不能持久化未交付的 finish 结果。
            # 原因：任务会带上下文退出，恢复后不应看到已经终止的工具轮。
            # 做法：只把结果留在运行期消息中，并依赖 ephemeral 过滤快照。
            # 目的：恢复后的历史保持待继续执行的状态。
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
    """后台执行异步工具，完成后通过路由 session 的 API 注入结果。"""
    # [Fork/Merge 2026-05-12] session_id here is the event/user-facing route session.
    # Why: async tool results create a new inbound and must attach to the parent session when
    # the original task is running on a branch. How: callers pass parent_session_id when present.
    # Purpose: branch-local ConversationStore writes remain isolated while async callbacks still
    # reach the SDK conversation_key mapping.
    _started = time.monotonic()
    try:
        _args_summary = _short(json.dumps(tool_args, ensure_ascii=False, default=str), 200)
        with get_bus().span('tool.call', payload={'tool': tool_name, 'args_summary': _args_summary, 'async': True}):
            result = await registry.execute(name=tool_name, arguments=tool_args, ctx=tool_ctx)

        _elapsed = time.monotonic() - _started
        # [summary-args 2026-05-19] Why: async tool completion is reported through
        # preempt text and should identify the original call. How: pass the saved
        # tool arguments into summarize_result(). Purpose: keep async summaries as
        # informative as synchronous handoff_progress rows.
        _summary = summarize_result(tool_name, result, args=tool_args)
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

        # [Fork/Merge 2026-05-12] Use the route session, not necessarily the runtime session.
        # Why: branch sessions are internal and may not have SDK channel mappings. How: the caller
        # passes parent_session_id when available. Purpose: async results become normal inbound
        # messages on the user-facing parent session.
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
        # [Fork/Merge 2026-05-17] Why: real tools may call supervisor APIs while
        # their node is running on a branch session. How: pass RunContext's parent
        # route session into ToolContext. Purpose: tool events, approvals, and
        # session-scoped built-ins stay attached to the durable user session.
        parent_session_id=getattr(ls.rctx, "parent_session_id", "") or "",
    )

    _tool_entries: list[dict[str, Any]] = []
    _tool_atts: list[dict[str, Any]] = []
    # Phase 3 Hook System：预构造当前批次的真实工具调用对象。
    # 原因：before_tool_call 的审批类 handler 需要看到“当前工具”和“本轮工具集合”。
    # 做法：把 legacy dict 形状转换为 ParsedToolCall，避免 handler 直接依赖 ai_step 内部字典。
    # 目的：在不改变工具执行结果格式的前提下，为真实工具执行前检查提供统一输入。
    _hook_real_tool_calls = [
        ParsedToolCall(
            id=str(_call.get("id") or ""),
            name=str(_call.get("name") or ""),
            arguments=dict(_call.get("arguments") or {}),
        )
        for _call in real_tool_calls
    ]

    for _rtc in real_tool_calls:
        if await ls.rctx.check_cancelled():
            break
        _t_name = _rtc["name"]
        _t_args = _rtc["arguments"]

        # Phase 3 Hook System：触发单个真实工具的 before_tool_call hook。
        # 原因：审批类 handler 以当前 tool_call 为粒度，不能只看整轮工具列表。
        # 做法：在实际执行工具前构造 HookContext；block/skip 时写入一个 tool_result 保持
        # native 工具调用配对完整。目的：新增 hook 不破坏后续 LLM 消息格式。
        _current_tool_call = ParsedToolCall(
            id=str(_rtc.get("id") or ""),
            name=str(_t_name),
            arguments=dict(_t_args or {}),
        )
        _tool_hook_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_current_tool_call,
            tool_calls=_hook_real_tool_calls,
            extra={"real_tool_calls": real_tool_calls},
        )
        _tool_hook_result = await hook_registry.afire("before_tool_call", _tool_hook_ctx)
        if _tool_hook_result.action is not None:
            return _tool_hook_result.action
        if _tool_hook_result.block or _tool_hook_result.skip_step:
            _blocked_msg = (
                _tool_hook_result.error_message
                or _tool_hook_result.reason
                or "Tool call blocked by before_tool_call hook."
            )
            _tool_entries.append({
                "id": _rtc.get("id", ""),
                "name": _t_name,
                "args": _t_args,
                "format": "text",
                "raw_inline": _blocked_msg,
                "truncated": False,
                "ref": "",
                "summary": _blocked_msg[:200],
            })
            continue

        # [2026-04-23] 异步工具分流：查询 spec 判断该工具是否为 async_mode。
        # 若是，则在后台 asyncio.Task 中执行，不阻塞当前推理循环，
        # 结果通过 preempt API 异步回传。从 commit 7d10197 恢复。
        _spec = ls.registry.get_spec(_t_name)
        _is_async = _spec.get("async_mode", False) if _spec else False

        if _is_async:
            # [WS tool result fields 2026-05-19] Why: tool_call_end now exposes
            # elapsed_ms for both synchronous and async-started tools. How: capture
            # a monotonic timestamp before the lifecycle start event is emitted.
            # Purpose: downstream WebSocket consumers can show one consistent
            # duration field without depending on SignalBus internals.
            _tool_t0 = time.monotonic()
            # [WS tool events 2026-05-17] Why: WebSocket clients need structured
            # tool lifecycle events in the durable EventLog, not only localized
            # handoff_progress text. How: emit a non-transient start event before
            # the async tool is scheduled. Purpose: reconnecting clients can replay
            # the tool start through the existing EventLog catch-up path.
            await ls.rctx.emit_event("tool_call_start", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "tool_call_id": _rtc.get("id", ""),
                "tool_name": _t_name,
                "arguments": _t_args,
            })
            _cleanup_async_tracker()
            _async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[_async_id] = {
                "tool_name": _t_name,
                "status": "running",
                "started_at": _tool_t0,
                "task_id": ls.rctx.task_id,
            }
            asyncio.create_task(
                _run_async_tool(
                    registry=ls.registry,
                    http=ls.rctx.http,
                    supervisor_url=ls.rctx.supervisor_url,
                    task_id=ls.rctx.task_id,
                    # [Fork/Merge 2026-05-12] Route async callbacks through the parent session.
                    # Why: ls.rctx.session_id may be an entry branch used only for runtime history.
                    # How: prefer parent_session_id and fall back to session_id for old tasks.
                    # Purpose: async tool results create follow-up inbound messages in the SDK-visible session.
                    session_id=ls.rctx.parent_session_id or ls.rctx.session_id,
                    tool_name=_t_name,
                    tool_args=_t_args,
                    tool_ctx=_tool_ctx,
                    async_tool_id=_async_id,
                ),
                name=f"async_tool_{_t_name}_{_async_id}",
            )
            _async_summary = f"异步执行已启动 (id: {_async_id})，结果将通过 preempt 自动回传"
            # [WS tool result fields 2026-05-19] Why: async-started calls have no
            # final tool result yet, but clients still need the same result schema.
            # How: define the same local variables used by the synchronous branch,
            # with result=None and the immediate placeholder text as raw_inline.
            # Purpose: tool_call_end consumers can parse sync and async lifecycle
            # events without special-casing missing keys.
            _t_result = None
            _t_fmt = "text"
            _t_raw_inline = f'\u23f3 Async tool "{_t_name}" started (id: {_async_id}). Result will be delivered via preempt when ready.'
            _tool_entries.append({
                "id": _rtc.get("id", ""),
                "name": _t_name,
                "args": _t_args,
                "format": _t_fmt,
                "raw_inline": _t_raw_inline,
                "truncated": False,
                "ref": "",
                "summary": _async_summary,
            })
            # [WS tool events 2026-05-17] Why: async tools return control before
            # the real result exists, so clients still need a lifecycle closure for
            # this immediate call. How: emit tool_call_end with async_started rather
            # than success or error. Purpose: UIs can show that the background task
            # was accepted while waiting for the later preempt-delivered result.
            await ls.rctx.emit_event("tool_call_end", {
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
                "tool_call_id": _rtc.get("id", ""),
                "tool_name": _t_name,
                "status": "async_started",
                "summary": _async_summary,
                "result": _t_result,
                "raw_inline": _t_raw_inline,
                "format": _t_fmt,
                "elapsed_ms": round((time.monotonic() - _tool_t0) * 1000, 1) if "_tool_t0" in dir() else None,
            })
            await ls.rctx.emit_event("handoff_progress", {
                "message": f"[{ls.node.id}] {_t_name}: 异步执行已启动",
                "node_id": ls.node.id,
                "task_id": ls.rctx.task_id,
            })
            continue

        # ---- 同步工具：阻塞等待执行完成（原有逻辑）----
        # [WS tool result fields 2026-05-19] Why: tool_call_end should include the
        # tool execution duration. How: capture a monotonic start timestamp before
        # emitting the structured start event and running the registry call. Purpose:
        # expose elapsed_ms without changing SignalBus span behavior.
        _tool_t0 = time.monotonic()
        # [WS tool events 2026-05-17] Why: handoff_progress remains for legacy
        # consumers, but the web UI needs structured lifecycle data. How: emit a
        # durable tool_call_start immediately before the SignalBus span. Purpose:
        # the EventLog can replay the exact tool name, call id, and arguments.
        await ls.rctx.emit_event("tool_call_start", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "tool_call_id": _rtc.get("id", ""),
            "tool_name": _t_name,
            "arguments": _t_args,
        })
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

        # [summary-args 2026-05-19] Why: handoff_progress keeps the legacy
        # "[node] tool: summary" format, so argument detail must come from the
        # summary itself. How: pass the parsed tool arguments alongside the result.
        # Purpose: show commands, queries, and target paths without changing the
        # event payload shape.
        _t_summary = summarize_result(_t_name, _t_result, args=_t_args)
        _t_fmt, _t_raw = result_to_raw(_t_name, _t_result)
        # [2026-04-17] 移除工具结果截断机制：不再截断、不再写 artifact，直接传完整结果。
        _t_raw_inline = _t_raw

        _tool_entries.append({
            "id": _rtc.get("id", ""),
            "name": _t_name,
            "args": _t_args,
            "format": _t_fmt,
            "raw_inline": _t_raw_inline,
            "truncated": False,  # [2026-04-17] 截断机制已移除，保留字段兼容 format_tool_trace
            "ref": "",
            "summary": _t_summary,
        })

        # [WS tool events 2026-05-17] Why: clients should receive a structured
        # completion event even when a tool reports an error dict or cancellation
        # stops later progress messages. How: derive status from the tool result's
        # error field and emit before any legacy handoff_progress path. Purpose:
        # reconnecting clients can reconstruct completed tool calls from EventLog.
        await ls.rctx.emit_event("tool_call_end", {
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "tool_call_id": _rtc.get("id", ""),
            "tool_name": _t_name,
            "status": "cancelled" if _t_cancelled else ("error" if (isinstance(_t_result, dict) and _t_result.get("error")) else "success"),
            "summary": _t_summary,
            # [WS tool result fields 2026-05-19] Why: SDKs and adapters need the
            # complete returned object, not only a short summary. How: carry the
            # original result plus the same formatted inline representation that is
            # appended to the model transcript. Purpose: leave truncation decisions
            # to consuming adapters while preserving the raw engine result here.
            "result": _t_result,
            "raw_inline": _t_raw_inline,
            "format": _t_fmt,
            "elapsed_ms": round((time.monotonic() - _tool_t0) * 1000, 1) if "_tool_t0" in dir() else None,
        })

        # [硬取消-场景1] 已取消的工具结果已存入 entries（上方 append），不处理附件和进度事件，
        # 直接退出循环。未执行的后续工具被跳过（循环顶部 check_cancelled），不产生 tool_result。
        if _t_cancelled:
            break

        # Phase 3 Hook System：工具附件收集交给 AttachmentCollector。
        # 原因：附件收集是 after_tool_call 的副作用，不应散落在真实工具执行主体中。
        # 做法：传入原始工具结果、局部附件列表和 loop state，由 handler 统一扩展。
        # 目的：保持最终附件选择和多模态结果提示不变。
        _attachment_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            tool_call=_current_tool_call,
            extra={
                "loop_state": ls,
                "tool_result": _t_result,
                "tool_attachments": _tool_atts,
            },
        )
        _attachment_result = await hook_registry.afire("after_tool_call", _attachment_ctx)
        if _attachment_result.action is not None:
            return _attachment_result.action

        # LEGACY: replaced by hook AttachmentCollector.
        # if isinstance(_t_result, dict) and isinstance(_t_result.get("attachments"), list):
        #     _tool_atts.extend(_t_result["attachments"])
        #     ls.collected_attachments.extend(_t_result["attachments"])
        #     ls.tool_produced_attachments.extend(_t_result["attachments"])

        await ls.rctx.emit_event("handoff_progress", {
            "message": f"[{ls.node.id}] {_t_name}: {_t_summary}",
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
        })

    if _tool_entries:
        for _entry in _tool_entries:
            _result_body = _entry["raw_inline"]
            # [2026-04-17] 截断机制已移除，不再追加 truncated 提示
            # [2026-05-01] 真实工具结果统一走 formatter.format_tool_result。
            # 原因：真 native 需要 role=tool + tool_call_id，而旧代码在这里手写 user 文本，
            # 会绕过新 NativeToolFormatter。fake-native/json 仍由各自 formatter 生成旧文本。
            _tool_msg = ls.formatter.format_tool_result(
                ParsedToolCall(
                    id=str(_entry.get("id") or ""),
                    name=str(_entry["name"]),
                    arguments=dict(_entry.get("args") or {}),
                ),
                _result_body,
            )
            set_message_meta(_tool_msg, MessageMeta(
                tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
                message_type="tool_result",
            ))
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
            tool_mode=getattr(ls.node, 'tool_mode', 'fake-native'),
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

async def _fire_task_end_hook_if_finish(ls: _LoopState, action: TaskAction, step_count: int) -> TaskAction:
    """Fire on_task_end for successful finish actions and keep the action updated.

    Why: most normal AI-node exits are produced inside finish or hybrid plaintext
    branches before run_ai_node reaches its outer max_steps fallback. How: route
    only ACTION_FINISH through the registered on_task_end handlers and copy the
    snapshot context_ref back when the handler reports that persistence ran.
    Purpose: connect ContextSnapshotSaver to the safe normal-end path without
    changing dispatch, fail, cancel, or preempt terminal semantics yet.
    """
    if action.action != ACTION_FINISH:
        return action

    # Phase 3 Hook System：普通完成路径也触发 on_task_end。
    # 原因：finish 可能从多个内部 helper 提前返回，外层没有统一的“成功结束”落点。
    # 做法：只在 ACTION_FINISH 返回前构造 HookContext，并传入 loop_state 与正确步数。
    # 目的：先覆盖低风险成功路径，后续再逐步迁移 fail/preempt/dispatch 的快照保存。
    _end_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=step_count,
        extra={"loop_state": ls, "step_count": step_count, "task_action": action},
    )
    _end_result = await hook_registry.afire("on_task_end", _end_ctx)
    if _end_result.action is not None:
        return _end_result.action
    if _end_ctx.extra.get("snapshot_saved"):
        action.context_ref = str(_end_ctx.extra.get("context_ref") or "")
    return action


async def run_ai_node(
    *,
    rctx: "RunContext",
    streaming: bool = False,
    # [provider-registry 2026-05-03] 推理循环只依赖 BaseProvider 接口。
    # 原因：provider 由 registry 创建后不一定是 OpenAI；做法：类型标注改为 BaseProvider；
    # 目的：删除不必要的具体 OpenAI 类型引用。
    provider: BaseProvider,
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
    # Phase 3 Hook System：每次进入 AI 节点都注册内置 handler。
    # 原因：内置 handler 已统一迁入 engine.builtin，并通过 PLUGIN_META 声明
    # 自己的 hook point。做法：自动扫描内置目录并注册到共享 hook_registry；
    # HookRegistry 会按名称替换旧实例。目的：删除集中硬编码注册，同时保持
    # finish_guard、approval、prompt 注入等内置规则始终可用。
    auto_discover_and_register(hook_registry)
    # Phase 3 External Hook Plugins：每次进入 AI 节点时扫描工作区 plugins/。
    # 原因：用户需要在不修改 engine 源码的情况下添加自定义 handler。
    # 做法：调用幂等的外部插件加载器；HookRegistry 会按 handler.name 替换旧实例。
    # 目的：启动时自动发现插件，同时避免重复注册和单个插件失败影响引擎启动。
    load_external_plugins(hook_registry, rctx.workspace_root / "plugins")

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
    # Phase 3 Hook System：初始组装只生成不含知识注入的 prompt 骨架。
    # Why: inference core should know only the hook point, not concrete knowledge
    # injection handlers. How: always fire before_prompt_build after fresh assembly
    # and let registered handlers rebuild messages in place. Purpose: keep prompt
    # ownership behind hooks while preserving the final prompt layout.
    _assembled_fresh = False
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
        _assembled_fresh = True

    if _assembled_fresh:
        # Phase 3 Hook System：初始 messages 完成后始终触发 before_prompt_build。
        # Why: the skeleton returned above intentionally contains no skill or
        # memory blocks, so the hook is the single place that may add them. How:
        # pass the rendered system prompt, history, instruction, and attachments
        # through HookContext.extra and request an in-place rebuild. Purpose: keep
        # zero behavior drift while removing knowledge injection from inference.
        _prompt_ctx = HookContext(
            messages=messages,
            tools=[],
            node=node,
            provider=provider,
            rctx=rctx,
            step=step_count,
            extra={
                "runtime_cfg": runtime_cfg,
                "instruction_text": instruction,
                "history": history,
                "attachments": attachments,
                "system_prompt": system_prompt,
                # Why: the initial message list is now only a prompt skeleton.
                # How: always request hook-side rebuild when before_prompt_build
                # runs. Purpose: ensure knowledge injection is handled solely by
                # hook handlers, not by the inference loop.
                "apply_injection": True,
            },
        )
        _prompt_result = await hook_registry.afire("before_prompt_build", _prompt_ctx)
        if _prompt_result.action is not None:
            return _prompt_result.action
        _is_block_mode = bool(_prompt_ctx.extra.get("is_block_mode", _is_block_mode))

    # ---- 追加恢复消息 ----
    if resume_data:
        messages.extend(_build_resume_messages(resume_data))
        if str(resume_data.get("type") or "") == "compact_done":
            # [2026-04-24] P1.5 熔断器：压缩成功时重置失败计数
            # [AutoC 2026-05-13] Why: compaction may have targeted the parent
            # session while the task resumed on a branch. How: reset the breaker
            # on parent_session_id when present. Purpose: success accounting stays
            # consistent with parent-first compact targeting.
            record_compact_success(rctx.parent_session_id or rctx.session_id)
            # Phase 2 Signal: compact.done 信号，通过 SignalBus 发射供监控使用
            _cd_payload = {
                "node_id": node.id,
                "success": resume_data.get("success", True),
                "before": resume_data.get("before", 0),
                "after": resume_data.get("after", 0),
            }
            # task 粒度信息（ConvStore 路径产生）
            for _k in ("total_segments", "kept_segments", "compressed_segments"):
                if _k in resume_data:
                    _cd_payload[_k] = resume_data[_k]
            get_bus().emit(Signal(name="compact.done", payload=_cd_payload))
            await rctx.emit_event("compact_done", _cd_payload)

    # ---- 构建工具列表 ----
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    _allowed_real_tools = {s.get("name") for s in tool_specs if s.get("name")}
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        # [2026-05-04] Register one dynamic dispatch tool per delegate target.
        # Why: target selection should happen through tool choice, not through an
        # aggregate dispatch schema. How: expand node.delegate_targets into only
        # dispatch:{target_id} specs. Purpose: keep dynamic dispatch intact while
        # removing the old aggregate dispatch tools from the model-visible list.
        openai_tools.extend(_dispatch_delegate_specs(delegate_targets, downstream_info))

    # switch_node 仅对非系统节点注入（系统节点如 memory_extractor 不应切换入口）
    _is_system_task = bool((rctx.task_context or {}).get("is_system_task"))
    if not _is_system_task:
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
        # Phase 3 Hook System：循环顶部统一触发 before_step。
        # 原因：取消、preempt 注入、microcompact、proactive snip 和自动压缩都属于
        # prompt 生成前的可插拔检查。做法：把完整 loop state 放入 HookContext.extra，
        # 由 PreemptChecker 与 CompactChecker 按优先级执行。目的：减少 ai_step.py
        # 中的硬编码控制流，同时保持旧行为顺序不变。
        _step_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            extra={"loop_state": ls, "step_count": step_count},
        )
        _step_result = await hook_registry.afire("before_step", _step_ctx)
        if _step_result.action is not None:
            return _step_result.action
        if _step_result.skip_step:
            continue

        # LEGACY: replaced by hook PreemptChecker and CompactChecker.
        # Why: inference core should only keep the before_step trigger point for
        # preempt and compact behavior. How: the old direct preempt injection path
        # was removed, while compact fallback helpers remain unused for comparison.
        # Purpose: avoid knowledge injection imports in this loop while keeping the
        # migrated execution order visible.

        # _update_dynamic_vars(ls)  # Disabled to prevent intra-task prompt cache invalidation

        result = await _call_llm_with_retry(ls, step)
        if isinstance(result, TaskAction):
            return result
        # ---------------------------------------------------------------
        # Preempt V3 需求1: _call_llm_with_retry 返回 None 表示流式输出
        # 在思考阶段被 preempt 截断。partial assistant message 已丢弃（不存
        # 历史），preempt 消息已存储在 ls.preempt_inject_info 中。
        # 跳到下一轮循环顶部，由 PreemptChecker 注入新用户消息后
        # 重新推理。与 cancel 的区别：不终止 task，继续循环。
        # ---------------------------------------------------------------
        if result is None:
            continue
        resp = result

        # P0 Task 内核化：记录实际完成的步数
        ls.rctx.completed_steps = step + 1

        # Phase 3 Hook System：LLM 调用后的 usage 统计交给 UsageTracker。
        # 原因：token 累加是 after_llm_call 的典型横切逻辑。做法：传入响应和
        # loop state，由 handler 更新 rctx.total_usage。目的：保持 TaskRecord
        # 用量统计不变，同时从 ai_step.py 中抽出 bookkeeping。
        _usage_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls},
        )
        _usage_result = await hook_registry.afire("after_llm_call", _usage_ctx)
        if _usage_result.action is not None:
            return _usage_result.action

        # LEGACY: replaced by hook UsageTracker.
        # if resp.usage and isinstance(resp.usage, dict):
        #     for _uk in ("prompt_tokens", "completion_tokens", "total_tokens"):
        #         if _uk in resp.usage:
        #             ls.rctx.total_usage[_uk] = ls.rctx.total_usage.get(_uk, 0) + resp.usage[_uk]

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
                return await _fire_task_end_hook_if_finish(ls, action, step + 1)
            continue

        # Phase 3 Hook System：纯文本响应交给 PlaintextRetryHandler。
        # 原因：hybrid 隐式 finish 与 tool_only 重试是 before_response 决策。
        # 做法：handler 根据 output_mode 返回 TaskAction 或追加 retry hint。
        # 目的：保留原行为，同时让响应策略可注册。
        _plaintext_ctx = HookContext(
            messages=ls.messages,
            tools=ls.openai_tools,
            node=ls.node,
            provider=ls.provider,
            rctx=ls.rctx,
            step=step,
            response=resp,
            extra={"loop_state": ls},
        )
        _plaintext_result = await hook_registry.afire("before_response", _plaintext_ctx)
        if _plaintext_result.action is not None:
            return await _fire_task_end_hook_if_finish(ls, _plaintext_result.action, step + 1)

        # LEGACY: replaced by hook PlaintextRetryHandler.
        # action = _handle_plaintext_response(ls, resp, step)
        # if action is not None:
        #     return action

    # ---- 达到最大步数 ----
    # Phase 3 Hook System：max_steps 是任务错误结束路径，先交给 on_task_error
    # 保存上下文。原因：ContextSnapshotSaver 应成为后续终止路径的统一入口；
    # 做法：传入正确 step_count=max_steps，并从 ctx.extra 读取 context_ref。
    # 目的：先安全覆盖此处单一错误路径，其他复杂终止路径保留旧逻辑。
    _error_ctx = HookContext(
        messages=ls.messages,
        tools=ls.openai_tools,
        node=ls.node,
        provider=ls.provider,
        rctx=ls.rctx,
        step=max_steps,
        extra={"loop_state": ls, "step_count": max_steps},
    )
    _error_result = await hook_registry.afire("on_task_error", _error_ctx)
    if _error_result.action is not None:
        return _error_result.action
    ctx_ref = str(_error_ctx.extra.get("context_ref") or "")
    if not _error_ctx.extra.get("snapshot_saved"):
        # LEGACY fallback: replaced by hook ContextSnapshotSaver for max_steps.
        ctx_ref = _persist_ctx(ls, max_steps)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error="达到最大步数限制。",
        context_ref=ctx_ref,
        summary="max_steps reached",
    )
