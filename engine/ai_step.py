from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from toolbox.registry import ToolRegistry
from toolbox.skills_runtime import build_skill_messages
from providers.openai import OpenAIProvider
from .memory import build_memory_messages

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
from .tool_step import result_to_raw, summarize_result, write_artifact
from .compact import should_compact, compact_messages
from clonoth_runtime import get_int, get_float, load_runtime_config
from toolbox.context import ToolContext

if TYPE_CHECKING:
    from .context import RunContext


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

_PSEUDO_TOOL_NAMES = frozenset({"dispatch_node", "dispatch_nodes", "finish", "ask", "reply", "switch_node"})

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

def _dispatch_node_spec(targets: list[str], downstream_info: list[dict[str, str]] | None = None) -> dict:
    """构建 dispatch_node 伪工具定义。downstream_info 包含各下游节点的 id/name/description。"""
    desc_parts = [
        "将任务委派给另一个节点。目标节点会独立执行，完成后结果返回给你。",
        "",
        "使用方式：",
        "- target：选择委派目标（见 enum）",
        "- instruction：给出清晰、具体、可执行的指令",
    ]
    if downstream_info:
        desc_parts.append("")
        desc_parts.append("各目标节点：")
        for info in downstream_info:
            desc_parts.append(f"- {info.get('name', info['id'])}（{info['id']}）：{info.get('description', '')}")
    desc_parts.extend([
        "",
        "何时使用：需要工具操作但你没有对应权限、需要多步执行、需要 shell 命令。",
        "何时不用：能直接回答的问题、你自己有工具可以直接调用。",
    ])
    return {
        "type": "function",
        "function": {
            "name": "dispatch_node",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": targets,
                        "description": "委派目标节点 ID。",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "给目标节点的清晰、具体、可执行的指令。像给一个刚进入房间的同事做简报——说明要做什么、为什么、你已经知道什么。",
                    },
                },
                "required": ["target", "instruction"],
            },
        },
    }

def _dispatch_nodes_spec(targets: list[str], downstream_info: list[dict[str, str]] | None = None) -> dict:
    """构建 dispatch_nodes 伪工具定义——并行委派多个节点实例。"""
    desc_parts = [
        "将任务并行委派给多个节点实例。所有子任务同时执行，全部完成后结果一起返回给你。",
        "",
        "使用场景：",
        "- 需要对大量数据分段处理（每段交给一个独立实例）",
        "- 需要多个独立子任务并行执行以提高效率",
        "- 同一个 target 可以出现多次，每次创建该角色的一个新实例",
    ]
    if downstream_info:
        desc_parts.append("")
        desc_parts.append("可用目标节点：")
        for info in downstream_info:
            desc_parts.append(f"- {info.get('name', info['id'])}（{info['id']}）：{info.get('description', '')}")
    return {
        "type": "function",
        "function": {
            "name": "dispatch_nodes",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {
                                    "type": "string",
                                    "enum": targets,
                                    "description": "目标节点 ID。",
                                },
                                "instruction": {
                                    "type": "string",
                                    "description": "给该实例的清晰、具体、可执行的指令。",
                                },
                            },
                            "required": ["target", "instruction"],
                        },
                        "description": "并行子任务列表。同一 target 可多次出现，每次创建独立实例。",
                    },
                },
                "required": ["tasks"],
            },
        },
    }

def _finish_spec() -> dict:
    """Build the finish pseudo-tool spec."""
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final result and terminate this node immediately.\n\n"
                "CRITICAL: Once you call finish, the node exits. No further tool calls "
                "will be executed — not in this turn, not after. If you still have tools "
                "to call, call them FIRST, then finish in a later turn.\n\n"
                "Parameters:\n"
                "- text: The final result text. Must describe what HAS BEEN done, "
                "not what is being done or will be done. Never use phrases like "
                "'working on it', 'please wait', 'in progress'.\n"
                "- summary: Brief summary (optional) for the upstream node.\n"
                "- attachment_paths: File paths to attach (optional).\n\n"
                "Do not include internal protocol markers or debug info in text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "最终结果文本。",
                    },
                    "summary": {
                        "type": "string",
                        "description": "简要摘要（可选），帮助上游快速了解结果。",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "附带的文件路径列表（可选，仅在有图片等附件时使用）。",
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
            "description": (
                "信息不足时，向调用方提问以获取更多信息。\n\n"
                "- 只在确实缺少完成任务所必需的信息时使用。\n"
                "- 不要用来确认你已经知道答案的问题。\n"
                "- 不要用来征求许可——如果你有权限做，直接做。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "向调用方提出的具体问题。",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _reply_spec() -> dict:
    """Build the reply pseudo-tool spec (non-terminating)."""
    return {
        "type": "function",
        "function": {
            "name": "reply",
            "description": (
                "Send an intermediate message to the user WITHOUT terminating this node.\n\n"
                "The node keeps running after this call — you can continue calling tools "
                "or do more work in subsequent turns.\n\n"
                "When to use:\n"
                "- You have a partial result or progress update to share, but more work remains.\n"
                "- The user asked for multiple things and you want to respond to one "
                "while continuing to work on the rest.\n\n"
                "When NOT to use:\n"
                "- You are done with all work → use finish instead.\n"
                "- You need more information from the user → use ask instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The intermediate message to send to the user.",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _switch_node_spec(targets: list[str], switch_info: list[dict[str, str]] | None = None, *, current_node_id: str = "", current_node_name: str = "") -> dict:
    """构建 switch_node 伪工具定义——切换 session 的对话节点。"""
    desc_parts = [
        "切换当前会话的对话节点。调用后当前节点 finish，用户的下一条消息将由目标节点处理。",
        "",
        "使用场景：",
        "- 用户明确要求切换到某个节点（如'切到编程节点'）",
        "- 当前节点能力不足，判断应该由其他节点持续对话",
        "",
        "注意：",
        "- 调用后当前节点立即终止，不会再执行后续工具调用",
        "- 目标节点能看到之前的对话历史",
        "- 传空字符串的 target 可恢复为默认入口节点",
    ]
    if switch_info:
        desc_parts.append("")
        desc_parts.append("可切换到的节点：")
        for info in switch_info:
            desc_parts.append(f"- {info.get('name', info['id'])}（{info['id']}）：{info.get('description', '')}")
    # target enum 允许空字符串（恢复默认）
    enum_vals = targets + [""] if targets else [""]
    return {
        "type": "function",
        "function": {
            "name": "switch_node",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": enum_vals,
                        "description": "目标节点 ID。传空字符串恢复为默认入口节点。",
                    },
                    "text": {
                        "type": "string",
                        "description": "给用户的回复文本（如'已切换到编程节点'）。",
                    },
                },
                "required": ["target", "text"],
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
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        result = resume_data.get("result") or {}
        summary = str(result.get("summary") or "")
        text = str(result.get("text") or "")
        child_atts = result.get("attachments")
        lines = [f"下游节点 {from_node} 已完成。" if from_node else "下游节点已完成。"]
        if summary:
            lines.append(f"摘要：{summary}")
        if text:
            lines.append("结果：")
            lines.append(text)
        content_text = "\n".join(lines).strip()
        if isinstance(child_atts, list) and child_atts:
            return [{"role": "user", "content": build_multimodal_content(content_text, child_atts)}]
        return [{"role": "user", "content": content_text}]

    # v2: child_ask
    if rtype == "child_ask":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        result = resume_data.get("result") or {}
        text = str(result.get("text") or "").strip()
        if from_node and text:
            content = f"下游节点 {from_node} 需要补充信息：{text}"
        elif text:
            content = f"下游节点需要补充信息：{text}"
        elif from_node:
            content = f"下游节点 {from_node} 需要补充信息。"
        else:
            content = "下游节点需要补充信息。"
        return [{"role": "user", "content": content}]

    # v2: child_failed
    if rtype == "child_failed":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        error = str(resume_data.get("error") or "未知错误")
        prefix = f"下游节点 {from_node} 执行失败：" if from_node else "下游节点执行失败："
        return [{"role": "user", "content": f"{prefix}{error}"}]

    # v2: child_cancelled
    if rtype == "child_cancelled":
        from_node = str(resume_data.get("from_node") or resume_data.get("child_node_id") or "")
        text = f"下游节点 {from_node} 已被取消。" if from_node else "下游节点已被取消。"
        return [{"role": "user", "content": text}]


    # v1: tool_results
    if rtype == "tool_results":
        entries = resume_data.get("tool_results")
        if not isinstance(entries, list):
            entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs: list[dict[str, Any]] = []
            all_atts: list[dict[str, Any]] = [] 
            for e in entries:
                _name = e.get("name", "unknown")
                _raw = e.get("raw_inline", "")
                msgs.append({"role": "user", "content": f'Tool result for "{_name}":\n{_raw}'})
                atts = e.get("attachments")
                if isinstance(atts, list):
                    all_atts.extend(atts)
            if all_atts:
                msgs.append({"role": "user", "content": build_multimodal_content("以上工具执行产生了以下图片结果：", all_atts)})
            return msgs
        return []

    # v3: batch_results（统一批量返回，node 和 tool 共用）
    if rtype == "batch_results":
        entries = resume_data.get("entries")
        if isinstance(entries, list) and entries:
            msgs: list[dict[str, Any]] = []
            all_atts: list[dict[str, Any]] = []
            for e in entries:
                _kind = str(e.get("kind") or "node")
                _status = str(e.get("status") or "")

                if _kind == "tool":
                    _name = e.get("name", "unknown")
                    _raw = e.get("raw_inline", "")
                    if _status == "fail":
                        msgs.append({"role": "user", "content": f'Tool "{_name}" 执行失败：{e.get("error", "")}'})    
                    else:
                        msgs.append({"role": "user", "content": f'Tool result for "{_name}":\n{_raw}'})
                else:
                    _node = str(e.get("node_id") or "unknown")
                    _instr = str(e.get("instruction") or "")
                    _text = str(e.get("text") or "")
                    _summary = str(e.get("summary") or "")
                    if _status == "fail":
                        msgs.append({"role": "user", "content": f"子节点 {_node} 执行失败：{e.get('error', '')}"})    
                    else:
                        lines = [f"子节点 {_node}（指令：{_instr[:100]}）已完成。"]
                        if _summary:
                            lines.append(f"摘要：{_summary}")
                        if _text:
                            lines.append(f"结果：\n{_text}")
                        msgs.append({"role": "user", "content": "\n".join(lines)})

                atts = e.get("attachments")
                if isinstance(atts, list):
                    all_atts.extend(atts)
            if all_atts:
                msgs.append({"role": "user", "content": build_multimodal_content("批量执行产生了以下图片结果：", all_atts)})
            return msgs
        return []

    return []


# ---------------------------------------------------------------------------
#  附件筛选
# ---------------------------------------------------------------------------

def _select_attachments(
    collected: list[dict[str, Any]],
    selected_paths: Any,
    workspace_root: "Path | None" = None,
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Select attachments by path from collected, or read from disk as fallback.

    Disk fallback is restricted to paths under workspace_root for security.
    """
    if not isinstance(selected_paths, list) or not selected_paths:
        return collected

    path_set = {str(p).strip() for p in selected_paths if isinstance(p, str) and str(p).strip()}
    selected = [a for a in collected if a.get("path") in path_set]
    found_paths = {a.get("path") for a in selected}

    if workspace_root:
        from .attachments import save_attachment
        for raw in sorted(path_set - found_paths):
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = workspace_root / p
            # Security: only allow paths within workspace
            try:
                p.resolve().relative_to(workspace_root.resolve())
            except ValueError:
                continue
            if not p.is_file():
                continue
            try:
                data_bytes = p.read_bytes()
            except Exception:
                continue
            att = save_attachment(workspace_root, session_id, data_bytes, filename=p.name)
            selected.append(att)

    return selected if selected else collected


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
    _tool_produced_attachments: list[dict[str, Any]] = []  # 仅工具产出的，不含用户输入的
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
        system_prompt = assemble_prompt(rctx.workspace_root, node, variables=prompt_vars)
        skill_budget = get_int(runtime_cfg, "skills.max_budget_chars", 0, min_value=0)
        skill_static, skill_dynamic = build_skill_messages(
            rctx.workspace_root,
            node_id=node.id,
            instruction_text=instruction,
            history=history,
            skill_mode=node.skill_access.mode,
            skill_allow=node.skill_access.allow,
            max_budget_chars=skill_budget,
        )
        memory_static, memory_dynamic = build_memory_messages(
            rctx.workspace_root,
            instruction_text=instruction,
            history=history,
            max_budget_chars=get_int(
                runtime_cfg, "memory.max_budget_chars", 0, min_value=0,
            ),
        )

        # ---- Prompt cache friendly layout ----
        # Stable prefix (system role, identical across turns → cache hit):
        #   static system prompt → constant skills → constant memory
        # History (appended, existing prefix unchanged across turns):
        #   user/assistant messages
        # Dynamic suffix (user role, may change per turn):
        #   dynamic prompt + active skills/memory → instruction
        #
        # Dynamic content uses role=user instead of role=system so that
        # Anthropic/Gemini (which merge all system messages into a single
        # system field) keep a stable system cache across turns.
        messages: list[dict[str, Any]] = []

        # --- stable prefix (system) ---
        if system_prompt:
            messages.append(system_prompt[0])  # static part
        messages.extend(skill_static)
        messages.extend(memory_static)

        # --- history ---
        messages.extend(history)

        # --- dynamic suffix (user role) ---
        _dynamic_parts: list[str] = []
        if len(system_prompt) >= 2 and system_prompt[1].get("content"):
            _dynamic_parts.append(system_prompt[1]["content"])
        for _dm in skill_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        for _dm in memory_dynamic:
            if _dm.get("content"):
                _dynamic_parts.append(_dm["content"])
        if _dynamic_parts:
            messages.append({
                "role": "user",
                "content": "This is the current turn's dynamic context information you can use. It may change between turns. Continue with the previous task if the information is not needed and ignore it.\n\n" + "\n\n".join(_dynamic_parts),
            })

        # 检查 history 末尾是否已包含当前 instruction（避免重复）
        _last = history[-1] if history else None
        _last_content = _last.get("content", "") if isinstance(_last, dict) else ""
        _already_in_history = (
            _last is not None
            and _last.get("role") == "user"
            and isinstance(_last_content, str)
            and _last_content.strip() == instruction.strip()
        )
        if not _already_in_history:
            ctx_text = instruction
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
        openai_tools.append(_dispatch_node_spec(delegate_targets, downstream_info))
        openai_tools.append(_dispatch_nodes_spec(delegate_targets, downstream_info))

    # 伪工具：节点切换
    _sw_targets = [info["id"] for info in (switch_info or [])]
    openai_tools.append(_switch_node_spec(_sw_targets, switch_info, current_node_id=node.id, current_node_name=node.name))

    # 伪工具：完成 + 提问（所有节点均可用）
    openai_tools.append(_finish_spec())
    openai_tools.append(_ask_spec())
    openai_tools.append(_reply_spec())

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
    _plaintext_retry_count = 0
    _plaintext_retry_max = get_int(runtime_cfg, "engine.plaintext_retry_max", 2, min_value=0, max_value=10)


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
                # 上报上下文窗口用量到 supervisor
                await rctx.emit_event("context_usage", {
                    "node_id": node.id,
                    "task_id": rctx.task_id,
                    "usage": resp.usage,
                })

            # ---- 重试判定（错误重试 / 空回复重试，统一路径） ----
            _retry_reason = ""
            if not resp.ok and _is_retryable_error(resp):
                _retry_reason = resp.error or "unknown"
            elif resp.ok and not resp.tool_calls and not (resp.text or "").strip():
                _retry_reason = "empty_response"

            if _retry_reason and _retry_attempt < _retry_max:
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
                    "error": _retry_reason,
                    "status_code": resp.status_code,
                })
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

            break  # 成功或不可重试，退出重试循环


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
                _tc_args = json.dumps(dict(_tc.arguments or {}), ensure_ascii=False)
                _tc_desc_parts.append(f"[Tool call history record: {_tc.name} was executed with args: {_tc_args}]")
            messages.append({"role": "user", "content": "\n".join(_tc_desc_parts) or "[tool_call]"})

            # 处理伪工具
            if pseudo_call is not None:
                args = pseudo_call.arguments or {}

                # reply: non-terminating, send intermediate message and continue
                if pseudo_call.name == "reply":
                    reply_text = str(args.get("text") or "").strip()
                    if reply_text:
                        await rctx.emit_event("intermediate_reply", {
                            "node_id": node.id,
                            "task_id": rctx.task_id,
                            "text": reply_text,
                        })
                        messages.append({
                            "role": "user",
                            "content": f"[Intermediate reply delivered to user: {reply_text}]",
                        })
                    if not real_tool_calls:
                        use_stream = streaming
                        continue
                    # Has real_tool_calls: fall through to execute them below

                # Terminating pseudo-tools: persist context and return
                else:
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

                    if pseudo_call.name == "dispatch_nodes":
                        tasks_list = args.get("tasks")
                        if isinstance(tasks_list, list) and tasks_list:
                            batch_items = []
                            for t in tasks_list:
                                batch_items.append({
                                    "kind": "node",
                                    "target": str(t.get("target") or "").strip(),
                                    "instruction": str(t.get("instruction") or "").strip(),
                                })
                            targets_str = ", ".join(c["target"] for c in batch_items)
                            return TaskAction(
                                action=ACTION_DISPATCH, node_id=node.id,
                                dispatch_batch=batch_items,
                                context_ref=ctx_ref,
                                summary=f"dispatch_nodes → [{targets_str}]",
                            )

                    if pseudo_call.name == "finish":
                        summary_text = str(args.get("summary") or "").strip()
                        result_text = str(args.get("text") or "").strip()
                        _selected_paths = args.get("attachment_paths")
                        if isinstance(_selected_paths, list) and _selected_paths:
                            final_atts = _select_attachments(
                                collected_attachments, _selected_paths,
                                workspace_root=rctx.workspace_root,
                                session_id=rctx.session_id,
                            )
                        else:
                            final_atts = []
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

                    if pseudo_call.name == "switch_node":
                        switch_target = str(args.get("target") or "").strip()
                        switch_text = str(args.get("text") or "").strip()
                        # 调用 supervisor API 设置 session 级节点覆盖
                        try:
                            await rctx.http.post(
                                f"{rctx.supervisor_url}/v1/sessions/{rctx.session_id}/switch_node",
                                json={"target_node_id": switch_target},
                            )
                        except Exception:
                            pass  # 设置失败不影响 finish
                        # 发出事件通知前端
                        await rctx.emit_event("node_switch", {
                            "target_node_id": switch_target,
                            "node_id": node.id,
                        })
                        return TaskAction(
                            action=ACTION_FINISH, node_id=node.id,
                            result={
                                "text": switch_text,
                                "attachments": list(_tool_produced_attachments),
                            },
                            context_ref=ctx_ref,
                            summary=f"switch → {switch_target or 'default'}",
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
                        _tool_produced_attachments.extend(_t_result["attachments"])

                    await rctx.emit_event("handoff_progress", {
                        "message": f"[{node.id}] {_t_name}: {_t_summary}",
                        "node_id": node.id,
                        "task_id": rctx.task_id,
                    })

                # 将工具结果追加到对话历史，继续推理循环
                if _tool_entries:
                    for _entry in _tool_entries:
                        _result_body = _entry["raw_inline"]
                        if _entry.get("truncated") and _entry.get("ref"):
                            _result_body += f"\n(Truncated. Full output: {_entry['ref']})"
                        messages.append({
                            "role": "user",
                            "content": f'Tool result for "{_entry["name"]}":\n{_result_body}',
                        })
                    if _tool_atts:
                        messages.append({"role": "user", "content": build_multimodal_content("以上工具执行产生了以下图片结果：", _tool_atts)})
                use_stream = streaming  # 工具执行完毕，恢复流式输出
                continue  # 工具结果已追加，回到循环顶部进行下一轮 LLM 调用


        # ---- 纯文本输出 → 要求使用 finish 提交 ----
        text = (resp.text or "").strip()
        if text:
            _plaintext_retry_count += 1
            if _plaintext_retry_count <= _plaintext_retry_max:
                # LLM 返回纯文本而非调用工具 → 不将纯文本塞回上下文（避免强化错误模式），
                # 直接提示使用正确的工具提交。
                messages.append({
                    "role": "user",
                    "content": (
                        "请使用 finish 工具提交你的最终回复，不要直接输出纯文本。"
                        "将你要回复的内容放在 finish 的 text 参数中。"
                        "如果你还有后续工作要做，使用 reply 工具发送中间进度。"
                    ),
                })
                use_stream = streaming
                continue  # 回到推理循环顶部
            else:
                # 纯文本重试次数耗尽，回退为直接以纯文本作为结果返回
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
