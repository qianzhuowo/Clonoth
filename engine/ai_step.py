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
from .compact import should_compact, compact_messages
from clonoth_runtime import get_int, load_runtime_config

if TYPE_CHECKING:
    from .context import RunContext


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

_PSEUDO_TOOL_NAMES = frozenset({"dispatch_node", "finish", "ask"})


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


        text_buf: _StreamBuffer | None = None
        think_buf: _StreamBuffer | None = None
        tools_arg = openai_tools if openai_tools else None
        llm_messages = prepare_messages_for_llm(messages, rctx.workspace_root)

        resp = None

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

        # ---- LLM 调用失败 ----
        if not resp.ok:
            ctx_ref = _persist_node_context(
                rctx.workspace_root, rctx.session_id,
                rctx.task_id or run_id or node.id, node.id, messages,
                step_count=step + 1, context_ref=context_ref,
            )
            return TaskAction(
                action=ACTION_FAIL, node_id=node.id,
                error=resp.error or "LLM 调用失败",
                context_ref=ctx_ref,
                summary=_short(resp.error or "LLM 调用失败", 240),
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

            # 处理真实工具调用 → 委派给 Tool 节点
            if real_tool_calls:
                await rctx.emit_event("handoff_progress", {
                    "message": f"[{node.id}] 请求执行 {len(real_tool_calls)} 个工具",
                    "node_id": node.id,
                    "task_id": rctx.task_id,
                })
                ctx_ref = _persist_node_context(
                    rctx.workspace_root, rctx.session_id,
                    rctx.task_id or run_id or node.id, node.id, messages,
                    step_count=step + 1, context_ref=context_ref,
                )
                if len(real_tool_calls) == 1:
                    tc = real_tool_calls[0]
                    return TaskAction(
                        action=ACTION_DISPATCH, node_id=node.id,
                        target_node=tc["name"],
                        dispatch_input={
                            "tool_call_id": tc["id"],
                            "arguments": tc["arguments"],
                        },
                        context_ref=ctx_ref,
                        summary=f"dispatch tool → {tc['name']}",
                    )
                else:
                    # 多个工具调用：批量委派
                    return TaskAction(
                        action=ACTION_DISPATCH, node_id=node.id,
                        target_node="__tool_batch__",
                        dispatch_input={"tool_calls": real_tool_calls},
                        context_ref=ctx_ref,
                        summary=f"dispatch {len(real_tool_calls)} tools",
                    )

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
