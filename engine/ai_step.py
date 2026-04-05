from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry
from toolbox.skills_runtime import build_skill_messages
from providers.openai import OpenAIProvider

from .context_store import load_context_snapshot, save_context_snapshot, write_context_snapshot
from .attachments import build_multimodal_content, prepare_messages_for_llm
from .node import Node
from .prompt import assemble_prompt
from .protocol import (
    TaskAction,
    ACTION_DISPATCH,
    ACTION_FINISH,
    ACTION_ASK,
    ACTION_FAIL,
    ACTION_CANCELLED,
)
from .tool_step import format_tool_trace
from .tool_step import result_to_raw, summarize_result, write_artifact
from .compact import should_compact, compact_messages
from clonoth_runtime import get_int, get_float, load_runtime_config
from toolbox.context import ToolContext

if TYPE_CHECKING:
    from .context import RunContext


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

_PSEUDO_TOOL_NAMES = frozenset({"dispatch_node", "finish", "ask"})

# ---------------------------------------------------------------------------
#  LLM 调用重试：可重试状态码
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _is_retryable_error(resp) -> bool:
    """判定 ProviderResponse 是否属于可重试的临时性错误。"""
    if resp.ok:
        return False
    # 有明确的 HTTP 状态码 → 检查是否在可重试集合内
    if resp.status_code is not None:
        return resp.status_code in _RETRYABLE_STATUS_CODES
    # 无状态码 → 网络异常（连接超时等），视为可重试
    return True


# ---------------------------------------------------------------------------
#  v3 伪工具 spec 构建
# ---------------------------------------------------------------------------

def _dispatch_node_spec(targets: list[str]) -> dict:
    """构建 dispatch_node 伪工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": "dispatch_node",
            "description": (
                "将任务委派给另一个节点。目标节点执行完成后，结果会返回给你。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": targets,
                        "description": "委派目标节点。",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "给目标节点的清晰、具体、可执行的指令。",
                    },
                },
                "required": ["target", "instruction"],
            },
        },
    }


def _finish_spec() -> dict:
    """构建 finish 伪工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "完成当前任务，提交结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "结果文本。",
                    },
                    "summary": {
                        "type": "string",
                        "description": "简要摘要（可选）。",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "附带的文件路径列表（可选）。",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _ask_spec() -> dict:
    """构建 ask 伪工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": "ask",
            "description": "信息不足，向调用方提问以获取更多信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "需要补充的问题。",
                    },
                },
                "required": ["text"],
            },
        },
    }




# ---------------------------------------------------------------------------
#  工具 spec 相关
# ---------------------------------------------------------------------------

def _to_openai_tools(specs: list[dict]) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": s["name"],
            "description": s.get("description", ""),
            "parameters": s.get("input_schema", {"type": "object", "properties": {}, "required": []}),
        },
    } for s in specs]


def _filter_tool_specs(node: Node, all_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mode = (node.tool_access.mode or "none").lower()
    if mode == "all":
        denied = set(node.tool_access.deny)
        if denied:
            return [s for s in all_specs if s.get("name") not in denied]
        return list(all_specs)
    if mode == "allowlist":
        allowed = set(node.tool_access.allow)
        return [s for s in all_specs if s.get("name") in allowed]
    return []


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "...<truncated>"


# ---------------------------------------------------------------------------
#  流式输出缓冲
# ---------------------------------------------------------------------------

class _StreamBuffer:
    def __init__(self, rctx: "RunContext", node_id: str, kind: str) -> None:
        self._rctx = rctx
        self._node_id = node_id
        self._kind = kind
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self.flushed_any = False

    async def push(self, chunk: str) -> None:
        self._buf.append(chunk)
        now = time.monotonic()
        buf_len = sum(len(s) for s in self._buf)
        if now - self._last_flush >= 0.15 or buf_len >= 60:
            await self.flush()

    async def flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._last_flush = time.monotonic()
        self.flushed_any = True
        await self._rctx.emit_event("stream_delta", {
            "node_id": self._node_id,
            "type": self._kind,
            "content": text,
        })


# ---------------------------------------------------------------------------
#  上下文持久化
# ---------------------------------------------------------------------------

def _sanitize_context_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "ctx").strip() or "ctx")[:80]


def _persist_node_context(
    workspace_root: Path,
    session_id: str,
    task_id: str,
    node_id: str,
    messages: list[dict[str, Any]],
    *,
    step_count: int,
    context_ref: str = "",
) -> str:
    snapshot = {
        "version": 1,
        "node_id": node_id,
        "messages": messages,
        "step_count": int(step_count),
    }
    if context_ref:
        return write_context_snapshot(workspace_root, context_ref, snapshot)
    context_id = _sanitize_context_id(f"{task_id}_{node_id}")
    return save_context_snapshot(workspace_root, session_id, snapshot, context_id=context_id)


# ---------------------------------------------------------------------------
#  恢复消息构建（同时兼容 v1 和 v2 格式）
# ---------------------------------------------------------------------------

def _build_resume_messages(resume_data: dict[str, Any]) -> list[dict[str, Any]]:
    """从 resume_data / resume_event 构建恢复消息。

    v2 格式:
      - child_result:    下级节点完成
      - child_failed:    下级节点失败
      - child_cancelled: 下级节点被取消
    v1 兼容:
      - tool_results:    工具调用结果
      - handoff_result:  子链返回结果
    """
    rtype = str(resume_data.get("type") or "").strip()

    # v2: child_result
    if rtype == "child_result":
        from_node = str(resume_data.get("from_node") or "")
        result = resume_data.get("result") or {}
        summary = str(result.get("summary") or "")
        text = str(result.get("text") or "")
        child_atts = result.get("attachments")
        lines = [f"下游节点 {from_node} 已完成。"]
        if summary:
            lines.append(f"摘要：{summary}")
        if text:
            lines.append("结果：")
            lines.append(text)
        content_text = "\n".join(lines).strip()
        if isinstance(child_atts, list) and child_atts:
            return [{"role": "user", "content": build_multimodal_content(content_text, child_atts)}]
        return [{"role": "user", "content": content_text}]

    # v2: child_failed
    if rtype == "child_failed":
        from_node = str(resume_data.get("from_node") or "")
        error = str(resume_data.get("error") or "未知错误")
        return [{"role": "user", "content": f"下游节点 {from_node} 执行失败：{error}"}]

    # v2: child_cancelled
    if rtype == "child_cancelled":
        from_node = str(resume_data.get("from_node") or "")
        return [{"role": "user", "content": f"下游节点 {from_node} 已被取消。"}]

    # v1: tool_results
    if rtype == "tool_results":
        entries = resume_data.get("tool_results")
        if not isinstance(entries, list):
            entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs: list[dict[str, Any]] = [{"role": "assistant", "content": format_tool_trace(entries)}]
            all_atts: list[dict[str, Any]] = []
            for e in entries:
                atts = e.get("attachments")
                if isinstance(atts, list):
                    all_atts.extend(atts)
            if all_atts:
                msgs.append({"role": "user", "content": build_multimodal_content("以上工具执行产生了以下图片结果：", all_atts)})
            return msgs
        return []

    return []


# ---------------------------------------------------------------------------
#  附件筛选
# ---------------------------------------------------------------------------

def _select_attachments(
    collected: list[dict[str, Any]],
    selected_paths: Any,
) -> list[dict[str, Any]]:
    if isinstance(selected_paths, list) and selected_paths:
        path_set = {str(p).strip() for p in selected_paths if isinstance(p, str)}
        selected = [a for a in collected if a.get("path") in path_set]
        return selected if selected else collected
    return collected


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
    downstream_capabilities: str = "",
    own_tools_text: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> TaskAction:
    runtime_cfg = load_runtime_config(rctx.workspace_root)
    max_steps = get_int(runtime_cfg, "engine.max_steps", 32, min_value=1, max_value=200)

    # ---- 收集附件 ----
    collected_attachments: list[dict[str, Any]] = []
    if attachments:
        collected_attachments.extend(attachments)
    if resume_data and isinstance(resume_data, dict):
        for e in (resume_data.get("tool_results") or resume_data.get("entries") or []):
            if isinstance(e, dict) and isinstance(e.get("attachments"), list):
                collected_attachments.extend(e["attachments"])
        if isinstance(resume_data.get("attachments"), list):
            collected_attachments.extend(resume_data["attachments"])
        # v2: child_result 中的附件
        rd = resume_data.get("result")
        if isinstance(rd, dict) and isinstance(rd.get("attachments"), list):
            collected_attachments.extend(rd["attachments"])

    # ---- 恢复或新建消息历史 ----
    step_count = 0
    snapshot = load_context_snapshot(rctx.workspace_root, context_ref) if context_ref else None
    if snapshot and isinstance(snapshot.get("messages"), list):
        messages = list(snapshot.get("messages") or [])
        try:
            step_count = int(snapshot.get("step_count") or 0)
        except Exception:
            step_count = 0
    else:
        prompt_vars: dict[str, str] = {
            "node_id": node.id,
            "node_name": node.name,
            "instruction": instruction,
        }
        if own_tools_text:
            prompt_vars["own_tools"] = own_tools_text
        if downstream_capabilities:
            prompt_vars["downstream"] = downstream_capabilities
        system_prompt = assemble_prompt(rctx.workspace_root, node, variables=prompt_vars)
        skill_budget = get_int(runtime_cfg, "skills.max_budget_chars", 0, min_value=0)
        skill_msgs = build_skill_messages(
            rctx.workspace_root,
            instruction_text=instruction,
            history=history,
            skill_mode=node.skill_access.mode,
            skill_allow=node.skill_access.allow,
            max_budget_chars=skill_budget,
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(skill_msgs)
        messages.extend(history)
        ctx_text = "\n".join([f"当前节点={node.id}", f"请完成指令：{instruction}"])
        if attachments:
            messages.append({"role": "user", "content": build_multimodal_content(ctx_text, attachments)})
        else:
            messages.append({"role": "user", "content": ctx_text})

    # ---- 追加恢复消息 ----
    if resume_data:
        messages.extend(_build_resume_messages(resume_data))

    # ---- 构建工具列表 ----
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    # 伪工具：委派
    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        openai_tools.append(_dispatch_node_spec(delegate_targets))

    # 伪工具：完成 + 提问（所有节点均可用）
    openai_tools.append(_finish_spec())
    openai_tools.append(_ask_spec())

    use_stream = streaming

    # ---- 上下文压缩配置 ----
    _compact_threshold = get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0)
    _compact_keep_recent = get_int(runtime_cfg, "engine.compact.keep_recent", 6, min_value=2, max_value=50)
    _compacted = False  # 防止同一轮推理循环中重复压缩
    _last_prompt_tokens: int | None = None  # 上一次 LLM 返回的 prompt_tokens

    # ---- LLM 重试配置 ----
    _retry_max = get_int(runtime_cfg, "engine.retry.max_retries", 3, min_value=0, max_value=10)
    _retry_initial_delay = get_float(runtime_cfg, "engine.retry.initial_delay_sec", 1.0, min_value=0.1, max_value=60.0)
    _retry_max_delay = get_float(runtime_cfg, "engine.retry.max_delay_sec", 30.0, min_value=1.0, max_value=300.0)
    _retry_backoff = get_float(runtime_cfg, "engine.retry.backoff_multiplier", 2.0, min_value=1.0, max_value=10.0)

    # ---- 推理循环 ----
    for step in range(step_count, max_steps):
        if await rctx.check_cancelled():
            await rctx.emit_event("cancel_acknowledged", {"node_id": node.id, "task_id": rctx.task_id, "step": step})
            return TaskAction(action=ACTION_CANCELLED, node_id=node.id, summary="任务已被用户取消。")

        # ---- 上下文压缩检查 ----
        if not _compacted and _compact_threshold > 0 and should_compact(messages, _compact_threshold, _last_prompt_tokens):
            _compacted = True
            try:
                await rctx.emit_event("compact_start", {"node_id": node.id, "step": step})
                compacted_messages = await compact_messages(provider, messages, keep_recent=_compact_keep_recent)
                if len(compacted_messages) < len(messages):
                    next_context_ref = _persist_node_context(
                        rctx.workspace_root, rctx.session_id,
                        rctx.task_id or run_id or node.id, node.id, compacted_messages,
                        step_count=step, context_ref=context_ref,
                    )
                    messages = compacted_messages
                    context_ref = next_context_ref
                await rctx.emit_event("compact_done", {"node_id": node.id, "step": step})
            except Exception as compact_err:
                await rctx.emit_event("compact_failed", {"node_id": node.id, "step": step, "error": str(compact_err)})


        tools_arg = openai_tools if openai_tools else None
        llm_messages = prepare_messages_for_llm(messages, rctx.workspace_root)

        resp = None
        _retry_attempt = 0

        # ---- LLM 调用（含重试） ----
        while True:
            text_buf: _StreamBuffer | None = None
            think_buf: _StreamBuffer | None = None

            # ---- 流式调用 ----
            if use_stream:
                text_buf = _StreamBuffer(rctx, node.id, "text")
                think_buf = _StreamBuffer(rctx, node.id, "thinking")
                stream_task = asyncio.create_task(
                    provider.chat_stream(
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
                    if await rctx.check_cancelled():
                        stream_task.cancel()
                        try:
                            await stream_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        await text_buf.flush()
                        await think_buf.flush()
                        if text_buf.flushed_any or think_buf.flushed_any:
                            await rctx.emit_event("stream_end", {"node_id": node.id, "has_text": text_buf.flushed_any, "has_thinking": think_buf.flushed_any})
                        await rctx.emit_event("cancel_acknowledged", {"node_id": node.id, "task_id": rctx.task_id, "step": step})
                        return TaskAction(action=ACTION_CANCELLED, node_id=node.id, summary="任务已被用户取消。")
                await text_buf.flush()
                await think_buf.flush()
                if text_buf.flushed_any or think_buf.flushed_any:
                    await rctx.emit_event("stream_end", {
                        "node_id": node.id,
                        "has_text": text_buf.flushed_any,
                        "has_thinking": think_buf.flushed_any,
                    })
                if resp.ok and resp.tool_calls:
                    use_stream = False
            else:
                # ---- 非流式调用（可取消） ----
                llm_task = asyncio.create_task(
                    provider.chat(messages=llm_messages, tools=tools_arg)
                )
                while True:
                    done, _ = await asyncio.wait({llm_task}, timeout=0.3)
                    if llm_task in done:
                        resp = llm_task.result()
                        break
                    if await rctx.check_cancelled():
                        llm_task.cancel()
                        try:
                            await llm_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        await rctx.emit_event("cancel_acknowledged", {"node_id": node.id, "task_id": rctx.task_id, "step": step})
                        return TaskAction(action=ACTION_CANCELLED, node_id=node.id, summary="任务已被用户取消。")

            assert resp is not None

            # ---- 提取 token usage ----
            if resp.usage and isinstance(resp.usage.get("prompt_tokens"), int):
                _last_prompt_tokens = resp.usage["prompt_tokens"]
                _compacted = False  # 有了新的 token 数据，允许重新判定是否需要压缩

            # ---- 重试判定 ----
            if not resp.ok and _is_retryable_error(resp) and _retry_attempt < _retry_max:
                _retry_attempt += 1
                _delay = min(
                    _retry_initial_delay * (_retry_backoff ** (_retry_attempt - 1)),
                    _retry_max_delay,
                )
                await rctx.emit_event("llm_retry", {
                    "node_id": node.id,
                    "step": step,
                    "attempt": _retry_attempt,
                    "max_retries": _retry_max,
                    "delay_sec": round(_delay, 2),
                    "error": resp.error or "unknown",
                    "status_code": resp.status_code,
                })
                # 退避等待，期间检查取消
                _waited = 0.0
                while _waited < _delay:
                    _sleep_step = min(0.5, _delay - _waited)
                    await asyncio.sleep(_sleep_step)
                    _waited += _sleep_step
                    if await rctx.check_cancelled():
                        await rctx.emit_event("cancel_acknowledged", {
                            "node_id": node.id, "task_id": rctx.task_id, "step": step,
                        })
                        return TaskAction(action=ACTION_CANCELLED, node_id=node.id, summary="任务已被用户取消。")
                resp = None
                continue  # 重试

            break  # 成功或不可重试的错误，退出重试循环

        # ---- LLM 调用失败（重试耗尽） ----
        if not resp.ok:
            _fail_msg = resp.error or "LLM 调用失败"
            if _retry_attempt > 0:
                _fail_msg = f"{_fail_msg} (已重试 {_retry_attempt} 次)"
            ctx_ref = _persist_node_context(
                rctx.workspace_root, rctx.session_id,
                rctx.task_id or run_id or node.id, node.id, messages,
                step_count=step + 1, context_ref=context_ref,
            )
            return TaskAction(
                action=ACTION_FAIL, node_id=node.id,
                error=_fail_msg,
                context_ref=ctx_ref,
                summary=_short(_fail_msg, 240),
            )

        # ---- 处理 tool_calls ----
        if resp.tool_calls:
            pseudo_call = None
            real_tool_calls: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                if tc.name in _PSEUDO_TOOL_NAMES:
                    pseudo_call = tc
                else:
                    real_tool_calls.append({
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": dict(tc.arguments or {}),
                    })

            # 将 LLM 的工具调用决策追加到对话历史，
            # 确保 dispatch 后恢复时上下文完整
            _tc_desc_parts: list[str] = []
            if resp.text:
                _tc_desc_parts.append(resp.text)
            for _tc in resp.tool_calls:
                _tc_desc_parts.append(f"[Calling tool: {_tc.name}]")
            messages.append({"role": "assistant", "content": "\n".join(_tc_desc_parts) or "[tool_call]"})

            # 处理伪工具
            if pseudo_call is not None:
                args = pseudo_call.arguments or {}
                ctx_ref = _persist_node_context(
                    rctx.workspace_root, rctx.session_id,
                    rctx.task_id or run_id or node.id, node.id, messages,
                    step_count=step + 1, context_ref=context_ref,
                )

                if pseudo_call.name == "dispatch_node":
                    target = str(args.get("target") or "").strip()
                    instr = str(args.get("instruction") or "").strip()
                    return TaskAction(
                        action=ACTION_DISPATCH, node_id=node.id,
                        target_node=target,
                        dispatch_input={"instruction": instr},
                        context_ref=ctx_ref,
                        summary=f"dispatch → {target}",
                    )

                if pseudo_call.name == "finish":
                    summary_text = str(args.get("summary") or "").strip()
                    result_text = str(args.get("text") or "").strip()
                    final_atts = _select_attachments(collected_attachments, args.get("attachment_paths"))
                    return TaskAction(
                        action=ACTION_FINISH, node_id=node.id,
                        result={
                            "summary": summary_text,
                            "text": result_text,
                            "attachments": final_atts,
                        },
                        context_ref=ctx_ref,
                        summary=_short(summary_text or result_text, 240),
                    )

                if pseudo_call.name == "ask":
                    ask_text = str(args.get("text") or "").strip()
                    return TaskAction(
                        action=ACTION_ASK, node_id=node.id,
                        result={"text": ask_text},
                        context_ref=ctx_ref,
                        summary=_short(ask_text, 240),
                    )

            # 处理真实工具调用 → 循环内直接执行
            if real_tool_calls:
                _rt_cfg = load_runtime_config(rctx.workspace_root)
                _max_inline = get_int(_rt_cfg, "engine.tool_trace.max_inline_chars", 8000, min_value=1000, max_value=200_000)

                await rctx.emit_event("handoff_progress", {
                    "message": f"[{node.id}] 执行 {len(real_tool_calls)} 个工具",
                    "node_id": node.id,
                    "task_id": rctx.task_id,
                })

                _tool_ctx = ToolContext(
                    supervisor_url=rctx.supervisor_url,
                    session_id=rctx.session_id,
                    run_id=rctx.task_id or run_id or node.id,
                    worker_id=rctx.worker_id,
                    workspace_root=rctx.workspace_root,
                    http=rctx.http,
                    registry=registry,
                    task_id=rctx.task_id,
                    session_generation=rctx.session_generation,
                )

                _tool_entries: list[dict[str, Any]] = []
                _tool_atts: list[dict[str, Any]] = []

                for _rtc in real_tool_calls:
                    if await rctx.check_cancelled():
                        break
                    _t_name = _rtc["name"]
                    _t_args = _rtc["arguments"]
                    _t_result = await registry.execute(name=_t_name, arguments=_t_args, ctx=_tool_ctx)
                    if isinstance(_t_result, dict) and _t_result.get("cancelled"):
                        break

                    _t_summary = summarize_result(_t_name, _t_result)
                    _t_fmt, _t_raw = result_to_raw(_t_name, _t_result)
                    _t_truncated = len(_t_raw) > _max_inline
                    _t_ref = ""
                    if _t_truncated:
                        _t_ref = await write_artifact(
                            rctx.workspace_root, rctx.task_id or run_id,
                            _rtc["id"], _t_name, _t_fmt, _t_raw,
                        )
                    _t_raw_inline = _t_raw if not _t_truncated else _t_raw[:_max_inline] + "\n...<truncated>"

                    _tool_entries.append({
                        "name": _t_name,
                        "args": _t_args,
                        "format": _t_fmt,
                        "raw_inline": _t_raw_inline,
                        "truncated": _t_truncated,
                        "ref": _t_ref,
                        "summary": _t_summary,
                    })

                    if isinstance(_t_result, dict) and isinstance(_t_result.get("attachments"), list):
                        _tool_atts.extend(_t_result["attachments"])
                        collected_attachments.extend(_t_result["attachments"])

                    await rctx.emit_event("handoff_progress", {
                        "message": f"[{node.id}] {_t_name}: {_t_summary}",
                        "node_id": node.id,
                        "task_id": rctx.task_id,
                    })

                # 将工具结果追加到对话历史，继续推理循环
                if _tool_entries:
                    messages.append({"role": "assistant", "content": format_tool_trace(_tool_entries)})
                    if _tool_atts:
                        messages.append({"role": "user", "content": build_multimodal_content("以上工具执行产生了以下图片结果：", _tool_atts)})
                use_stream = streaming  # 工具执行完毕，恢复流式输出
                continue  # 工具结果已追加，回到循环顶部进行下一轮 LLM 调用


        # ---- 纯文本输出 → 返回结果 ----
        text = (resp.text or "").strip()
        if text:
            ctx_ref = _persist_node_context(
                rctx.workspace_root, rctx.session_id,
                rctx.task_id or run_id or node.id, node.id, messages,
                step_count=step + 1, context_ref=context_ref,
            )
            return TaskAction(
                action=ACTION_FINISH, node_id=node.id,
                result={"text": text, "attachments": collected_attachments},
                context_ref=ctx_ref,
                summary=_short(text, 240),
            )

    # ---- 达到最大步数 ----
    ctx_ref = _persist_node_context(
        rctx.workspace_root, rctx.session_id,
        rctx.task_id or run_id or node.id, node.id, messages,
        step_count=max_steps, context_ref=context_ref,
    )
    return TaskAction(
        action=ACTION_FAIL, node_id=node.id,
        error="达到最大步数限制。",
        context_ref=ctx_ref,
        summary="max_steps reached",
    )
