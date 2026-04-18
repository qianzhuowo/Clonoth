from __future__ import annotations

import asyncio
import json
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
from ..tool_step import result_to_raw, summarize_result
from ..compact import should_compact, _format_messages_for_summary
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

if TYPE_CHECKING:
    from ..context import RunContext


# ---------------------------------------------------------------------------
#  异步工具跟踪表（Async Tool Tracking）
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
        )
        # Child Session 隔离（Phase B）：子节点写入自己的 child session JSONL，
        # 不再写入父 session。无 child_session_id 时仍写入 parent session（主节点路径）。
        target_session = getattr(ls.rctx, 'child_session_id', '') or ls.rctx.session_id
        store.append(target_session, msg)
        # Phase 3: 记录最后一次影子写入的 message id，供 snapshot 持久化使用
        ls.last_shadow_message_id = msg_id
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
    _inj_skill_s, _inj_skill_d = build_skill_messages(
        ls.rctx.workspace_root,
        node_id=ls.node.id,
        instruction_text=_new_instruction,
        history=ls.history,
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
            history=ls.history,
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
    if not should_compact(ls.messages, ls.compact_threshold, ls.last_prompt_tokens):
        return None

    ls.compacted = True
    try:
        await ls.rctx.emit_event("compact_start", {"node_id": ls.node.id, "step": step})
        conversation_text = _format_messages_for_summary(
            [m for m in ls.messages if m.get("role") != "system" and not m.get("_dynamic")]
        )
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
            real_tool_calls.append({
                "id": tc.id,
                "name": tc.name,
                "arguments": dict(tc.arguments or {}),
            })

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

    # 【修复：compactor 泄漏问题】
    # 原先此处有隐式兜底：tool_calls 同轮若伴随纯文本，会自动当作 intermediate_reply
    # 推给用户（Discord 可见）并注入 [Intermediate reply delivered to user: ...]。
    # 副作用：system.compactor 把 <analysis><summary> 整块写在对话正文里，被兜底泄漏给用户。
    # 根因是 finish 工具 description 误导模型把 text 当状态汇报、把真正 deliverable 放在正文。
    # 现改为：删除隐式兜底，自由正文的流向由 formatter 层（JsonToolFormatter.parse_tool_calls）
    # 统一处理——当 JSON 模式下模型输出自由正文+非 finish 工具调用时，反向包装为显式 reply 调用；
    # native 模式下 text 与 tool_calls 独立，text 视为正常 thinking 伴随文本，不再推用户。
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
        action = await _handle_pseudo_tool(ls, _finish_call, step)
        if action is not None:
            return action

    # 无终止型动作 → 继续下一轮推理
    if pseudo_calls or real_tool_calls:
        ls.use_stream = ls.streaming
    return None


# ---------------------------------------------------------------------------
#  异步工具后台执行器
# ---------------------------------------------------------------------------

async def _run_async_tool(
    registry: ToolRegistry,
    http: "httpx.AsyncClient",
    supervisor_url: str,
    task_id: str,
    tool_name: str,
    tool_args: dict,
    tool_ctx: ToolContext,
    async_tool_id: str,
) -> None:
    """后台执行异步工具，完成后通过 preempt 注入结果。

    异步工具支持（2026-04-18）：
    由 _execute_real_tools 中 asyncio.create_task 启动，不阻塞主推理循环。
    工具完成后 POST preempt 到 supervisor，supervisor 将结果注入到当前 task。
    信号 span 在此函数内发射，因为主循环已继续执行。

    async_tool_id 参数（2026-04-18 tracking 机制）：
    用于关联 tracking map 条目，完成/失败时更新状态并在 preempt 消息中携带 id，
    使 LLM 能将 preempt 回传与先前的占位消息对应起来。

    注意：此函数只接收必要的轻量参数，不持有 _LoopState 引用，
    避免后台协程运行期间将整个对话历史 pin 在内存中。
    """
    _started = time.monotonic()
    try:
        # Phase 2 Signal: 异步工具的 tool.call span 在后台函数内发射
        _args_summary = _short(json.dumps(tool_args, ensure_ascii=False, default=str), 200)
        with get_bus().span('tool.call', payload={'tool': tool_name, 'args_summary': _args_summary, 'async': True}):
            result = await registry.execute(name=tool_name, arguments=tool_args, ctx=tool_ctx)

        _elapsed = time.monotonic() - _started
        _summary = summarize_result(tool_name, result)
        _fmt, raw = result_to_raw(tool_name, result)

        # [2026-04-18 tracking] 更新 tracking map：标记完成，记录耗时
        _async_tool_tasks[async_tool_id] = {
            "tool_name": tool_name,
            "status": "done",
            "task_id": task_id,
            "started_at": _started,
            "finished_at": time.monotonic(),
            "elapsed": round(_elapsed, 1),
        }

        # 构建 preempt payload，将完整工具结果通过 preempt 回传给当前 task
        # [2026-04-18 tracking] preempt 消息头部携带 async_tool_id，便于 LLM 关联
        preempt_text = (
            f'\u2705 Async tool "{tool_name}" (id: {async_tool_id}) completed in {_elapsed:.1f}s.'
            f'\nSummary: {_summary}\nResult:\n{raw}'
        )

        # 收集附件（如生图工具产生的图片路径）
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

        await http.post(
            f"{supervisor_url}/v1/tasks/{task_id}/preempt",
            json=payload,
        )
    except Exception as e:
        # [2026-04-18 tracking] 更新 tracking map：标记失败
        _async_tool_tasks[async_tool_id] = {
            "tool_name": tool_name,
            "status": "failed",
            "task_id": task_id,
            "started_at": _started,
            "finished_at": time.monotonic(),
            "elapsed": round(time.monotonic() - _started, 1),
            "error": str(e),
        }
        # 失败也要通知，避免 LLM 永远等不到结果
        try:
            await http.post(
                f"{supervisor_url}/v1/tasks/{task_id}/preempt",
                json={"message": f'\u274c Async tool "{tool_name}" (id: {async_tool_id}) failed: {e}'},
            )
        except Exception:
            pass  # 双重失败时静默，避免未处理异常泄漏到事件循环


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

        # 异步工具分流（2026-04-18）：查询 spec 判断该工具是否为 async_mode。
        # async_mode 工具由后台协程执行，主循环立即得到占位结果继续推理；
        # 同步工具保持原有 await 行为。
        _spec = ls.registry.get_spec(_t_name)
        _is_async = _spec.get("async_mode", False) if _spec else False

        if _is_async:
            # ---- 异步工具：后台执行，不阻塞主推理循环 ----
            # [2026-04-18 tracking] 生成 8 位 hex id，写入 tracking map，传给后台协程。
            # 占位消息和 preempt 回传都携带此 id，LLM 可据此关联。
            # 每次新增前顺便清理过期条目，防止 map 无限增长。
            _cleanup_async_tracker()
            _async_id = uuid.uuid4().hex[:8]
            _async_tool_tasks[_async_id] = {
                "tool_name": _t_name,
                "status": "running",
                "started_at": time.monotonic(),
                "task_id": ls.rctx.task_id,
            }
            # 使用 asyncio.create_task 启动后台协程。该 task 运行在事件循环级别，
            # 不受 worker_loop 的 _active task set 管理，因此不会在主 task finish 时被取消。
            # 工具完成后通过 _run_async_tool 内的 preempt POST 回传结果。
            asyncio.create_task(
                _run_async_tool(
                    registry=ls.registry,
                    http=ls.rctx.http,
                    supervisor_url=ls.rctx.supervisor_url,
                    task_id=ls.rctx.task_id,
                    tool_name=_t_name,
                    tool_args=_t_args,
                    tool_ctx=_tool_ctx,
                    async_tool_id=_async_id,
                ),
                name=f"async_tool_{_t_name}_{_async_id}",
            )
            # 立即追加占位结果，告知 LLM 该工具已在后台运行，附带 tracking id
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
        if isinstance(_t_result, dict) and _t_result.get("cancelled"):
            break

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
            await rctx.emit_event("compact_done", {
                "node_id": node.id,
                "success": resume_data.get("success", True),
                "before": resume_data.get("before", 0),
                "after": resume_data.get("after", 0),
            })
            # Phase 3 Signal: compact.done — context compaction completed
            _c_before = resume_data.get("before", 0)
            _c_after = resume_data.get("after", 0)
            get_bus().emit(Signal(
                name="compact.done",
                payload={
                    "node_id": node.id,
                    "success": resume_data.get("success", True),
                    "before_tokens": _c_before,
                    "after_tokens": _c_after,
                    "ratio": round(_c_after / _c_before, 2) if _c_before > 0 else 0,
                },
            ))

    # ---- 构建工具列表 ----
    tool_specs = _filter_tool_specs(node, registry.list_specs())
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

        action = await _check_and_compact(ls, step)
        if action is not None:
            return action

        _update_dynamic_vars(ls)

        result = await _call_llm_with_retry(ls, step)
        if isinstance(result, TaskAction):
            return result
        resp = result

        if not resp.ok:
            return _build_failure_action(ls, resp, step)

        # ---- 从文本中解析工具调用（formatter 统一处理）----
        if not resp.tool_calls:
            _parsed = formatter.parse_tool_calls(resp)
            if _parsed:
                # [refactor 2026-04-18] thinking → reasoning，同步新增 inline_data / provider_meta
                resp = ProviderResponse(
                    ok=True,
                    text=formatter.get_plain_text(resp),
                    tool_calls=[
                        ToolCall(id=p.id, name=p.name, arguments=p.arguments)
                        for p in _parsed
                    ],
                    reasoning=resp.reasoning,
                    status_code=resp.status_code,
                    usage=resp.usage,
                    inline_data=resp.inline_data,
                    provider_meta=resp.provider_meta,
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
    # Phase 2 Signal: max_steps 超限时发射 task.error 信号。
    # 从消息历史中反向查找最后执行的工具名，附带到 payload 中，便于监控和告警定位。
    _last_tool = ""
    for _m in reversed(ls.messages):
        _c = _m.get("content", "")
        if isinstance(_c, str) and _c.startswith('Tool result for "'):
            _last_tool = _c.split('"')[1]
            break
    get_bus().emit(Signal(
        name="task.error",
        payload={
            "error_type": "MaxStepsExceeded",
            "steps": max_steps,
            "max_steps": max_steps,
            "last_tool": _last_tool,
            "node_id": ls.node.id,
            "task_id": ls.rctx.task_id,
            "severity": "error",
        },
    ))
    ctx_ref = _persist_ctx(ls, max_steps)
    return TaskAction(
        action=ACTION_FAIL, node_id=ls.node.id,
        error="达到最大步数限制。",
        context_ref=ctx_ref,
        summary="max_steps reached",
    )
