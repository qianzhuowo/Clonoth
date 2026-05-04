"""伪工具 spec 构建器和工具列表 helpers。

从 ai_step.py 中拆出，这些函数零外部依赖，纯数据结构构建。
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..node import Node


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

_PSEUDO_TOOL_NAMES = frozenset({"finish", "reply", "switch_node", "compact_context", "preempt_task"})

# [2026-05-04] Dynamic delegate dispatch tools use a stable prefix plus target id.
# Why: the removed aggregate dispatch tools hid delegate choices behind one
# schema. How: keep only static pseudo-tool names in _PSEUDO_TOOL_NAMES and
# recognize dispatch:{target_id} through this prefix. Purpose: route dynamic tools
# through the existing pseudo-tool execution path without hard-coding node ids or
# reintroducing removed aggregate tools.
DISPATCH_TOOL_PREFIX = "dispatch:"


def _is_pseudo_tool_name(name: str) -> bool:
    """Return whether ``name`` is handled by the engine pseudo-tool layer."""
    tool_name = str(name or "").strip()
    # [2026-05-04] The dynamic dispatch pattern requires a concrete target id.
    # Why: dispatch: without a suffix cannot be executed by pseudo_handlers.
    # How: keep exact static-name matching and accept only dispatch:{target_id}
    # with a non-empty suffix. Purpose: avoid treating malformed dynamic names as
    # pseudo tools while preserving all valid per-target dispatch tools.
    return tool_name in _PSEUDO_TOOL_NAMES or (
        tool_name.startswith(DISPATCH_TOOL_PREFIX)
        and bool(tool_name[len(DISPATCH_TOOL_PREFIX):].strip())
    )


def _dispatch_tool_name(target_id: str) -> str:
    """Build the public per-target dispatch tool name."""
    return f"{DISPATCH_TOOL_PREFIX}{str(target_id or '').strip()}"


def _dispatch_target_from_tool_name(tool_name: str) -> str:
    """Extract the fixed target id from a dispatch:{target_id} tool name."""
    name = str(tool_name or "").strip()
    if not name.startswith(DISPATCH_TOOL_PREFIX):
        return ""
    return name[len(DISPATCH_TOOL_PREFIX):].strip()

# [RFC 2026-04-20] finish 升级为真实 API 工具：tool_result 固定内容。
# 仅用于满足 API 的 tool_use/tool_result 配对校验和下一轮对话历史格式合法性。
# finish 调用后循环即终止，模型不会看到此内容。
FINISH_TOOL_RESULT_CONTENT = "completed"


# ---------------------------------------------------------------------------
#  v3 伪工具 spec 构建
# ---------------------------------------------------------------------------

# [2026-05-04] Shared parameter schema for dynamic per-target dispatch tools.
# Why: dispatch:{target_id} fixes the target in the tool name, so exposing a
# target argument would let model output disagree with the registered tool.
# How: reuse the legacy dispatch parameters except for target, and document the
# default context behavior in schema. Purpose: keep execution compatible with the
# existing supervisor dispatch API while making each delegate target visible as a
# first-class tool.
def _dispatch_delegate_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "给目标节点的清晰、具体、可执行的指令。说明要做什么、为什么、你已经知道什么。",
            },
            "context_mode": {
                "type": "string",
                "enum": ["fresh", "fork", "accumulate"],
                "default": "accumulate",
                "description": "子节点上下文模式。fresh=每次从零无历史；fork=继承父节点对话历史；accumulate=首次从零后续恢复自己的上下文（默认 accumulate）。",
            },
            "context_key": {
                "type": "string",
                "description": "上下文继承标识（仅 accumulate 模式有效）。同一 context_key 的历次任务共享上下文链。不填则按目标节点 ID 查找。",
            },
            "attachment_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths (relative to workspace root) to attach to the child node's initial context. Images are injected as multimodal content; other files as references the child can read_file.",
            },
        },
        "required": ["instruction"],
    }


def _dispatch_delegate_spec(target_id: str, info: dict[str, str] | None = None) -> dict:
    """Build one dispatch:{target_id} pseudo-tool spec."""
    target = str(target_id or "").strip()
    node_name = str((info or {}).get("name") or target).strip() or target
    node_description = str((info or {}).get("description") or node_name or target).strip()
    desc_parts = [
        f"将任务委派给固定目标节点 {node_name}（{target}）。",
        node_description,
        "",
        "目标节点会独立执行，完成后结果返回给你。",
        "参数中不再提供 target；该工具名已经固定了目标节点。",
        "需要并行委派时，可以在同一轮并行调用多个 dispatch:{target_id} 工具。",
    ]
    return {
        "type": "function",
        "function": {
            "name": _dispatch_tool_name(target),
            "description": "\n".join(part for part in desc_parts if part is not None),
            "parameters": _dispatch_delegate_parameters(),
        },
    }


def _dispatch_delegate_specs(targets: list[str], downstream_info: list[dict[str, str]] | None = None) -> list[dict]:
    """Build dynamic dispatch pseudo-tools for all delegate targets."""
    info_by_id = {
        str(info.get("id") or "").strip(): info
        for info in (downstream_info or [])
        if isinstance(info, dict) and str(info.get("id") or "").strip()
    }
    result: list[dict] = []
    seen: set[str] = set()
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        result.append(_dispatch_delegate_spec(target, info_by_id.get(target)))
    return result

def _finish_spec() -> dict:
    """Build the finish pseudo-tool spec.

    【Fix 4】description 重写：
    - 原文 "describe what HAS BEEN done in text" 措辞误导模型把 text 写成「已完成 X」
      之类的状态汇报，把真正的交付载荷（如 compactor 的 <analysis><summary>）
      留在自由正文里，配合旧的隐式兜底导致泄漏。
    - 新版强调 text 是「实际交付内容」，不是状态描述；并明确自由正文不会送达用户/调用方，
      只有 reply()/finish() 的 text 才会到达。
    """
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final result and terminate this node immediately.\n\n"
                "CRITICAL: Once you call finish, the node exits. If you have other tools to call (like execute_command or save_memory), you MUST wait for them to complete (`ok`) before calling finish.\n\n"
                "⚠️ finish MUST be called ALONE — never in the same turn as other tools (except reply). "
                "If you call finish alongside execute_command, save_memory, a dispatch tool, or any other tool, ALL calls will be REJECTED and you must retry. "
                "Always: execute tools first → wait for results → then call finish separately.\n\n"
                "Do NOT send your final report via `reply` while waiting for tools. Just call the tools silently, wait for the next turn, and put your ENTIRE final report in the `finish` tool's text.\n\n"
                "The `text` parameter is the ACTUAL DELIVERABLE — the content the caller "
                "or user will receive. Put your real output here (full answer, full data, "
                "full summary), not a status report of what you did. If the caller expects "
                "a structured payload, put the entire structured payload in `text`.\n\n"
                "Free prose outside of tool calls is NOT delivered to the caller or user. "
                "Only `reply()` and `finish()` reach the recipient.\n\n"
                "Use cases:\n"
                "- Task completed: put the deliverable content in `text`.\n"
                "- Need more information: put your specific question in `text`. "
                "The caller or user will see it and respond.\n\n"
                "Parameters:\n"
                "- text: The actual deliverable content, or a question if you need more info.\n"
                "- summary: Brief summary (optional) for the upstream node.\n"
                "- attachment_paths: File paths to attach (optional).\n\n"
                "Do not include internal protocol markers or debug info in text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "最终结果文本，或需要补充信息时的具体问题。",
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


def _reply_spec() -> dict:
    """Build the reply pseudo-tool spec (non-terminating).

    【Fix 4】description 重写：
    - 显式声明 `text` 是 user-facing 的消息内容。
    - 显式声明自由正文不会送达用户，只有 reply()/finish() 才会到达，
      避免模型把想说的话写在 tool_call 之外的自由正文里。
    """
    return {
        "type": "function",
        "function": {
            "name": "reply",
            "description": (
                "Send an interim message to the user WITHOUT terminating this node.\n\n"
                "The node keeps running after this call — you can continue calling tools "
                "or do more work in subsequent turns.\n\n"
                "The `text` parameter is the message content the user will read. "
                "Anything you want the user to see mid-task must go here. "
                "Do NOT rely on free prose output — free prose is not delivered to the user; "
                "only reply() and finish() reach the user.\n\n"
                "When to use:\n"
                "- You have a partial result or progress update to share, but more work remains.\n"
                "- The user asked for multiple things and you want to respond to one "
                "while continuing to work on the rest.\n\n"
                "When NOT to use:\n"
                "- You are done with all work → use finish instead.\n"
                "- You need more information from the user → use finish instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message content the user will read.",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _compact_context_spec() -> dict:
    """伪工具：手动触发上下文压缩。"""
    return {
        "type": "function",
        "function": {
            "name": "compact_context",
            "description": (
                "Compress the current conversation context by summarizing older messages.\n\n"
                "Use when context is getting very long and you want to free up token budget. "
                "Recent messages are preserved; older messages are replaced with a structured summary.\n\n"
                "This is non-terminating — the node continues running after compression.\n"
                "The compression happens immediately and takes effect for subsequent LLM calls in this session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_recent": {
                        "description": "Number of recent conversation rounds to preserve (default uses system config, typically 6).",
                        "type": "integer",
                    },
                },
                "required": [],
            },
        },
    }


def _preempt_task_spec() -> dict:
    """伪工具：软打断一个正在运行的子任务。"""
    return {
        "type": "function",
        "function": {
            "name": "preempt_task",
            "description": (
                "Soft-interrupt a running child task. The target task will finish its current "
                "atomic operation (e.g. tool call), persist a context snapshot, and then exit gracefully.\n\n"
                "Use cases:\n"
                "- A dispatched child node is taking too long and you want to reclaim control.\n"
                "- The user's new message makes the child's current work unnecessary.\n"
                "- You want to 'continue' a finished node by preempting and re-dispatching with updated instructions.\n\n"
                "This is non-terminating — your node keeps running after the call. "
                "The preempted task will return its partial result (if any) via the normal dispatch callback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "要打断的任务 ID（完整或前缀均可）。可从 dispatch 回调或 active_tasks 中获取。",
                    },
                },
                "required": ["task_id"],
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

# [2026-04-23] 异步工具提示：当 spec 中 async_mode=True 时，在 description 末尾追加说明，
# 告知模型该工具为后台执行、结果通过 preempt 自动回传。从 commit 7d10197 恢复。
_ASYNC_TOOL_HINT = (
    "\n\n⚡ This is an async tool. Execution runs in background; "
    "result will be delivered automatically via preempt when ready. "
    "You can continue working or finish — no need to wait."
)


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    # [2026-04-23] 恢复 async_mode 支持：遍历 spec 列表，对 async_mode=True 的工具
    # 在 description 末尾追加 _ASYNC_TOOL_HINT，使模型知道该工具为异步执行。
    result = []
    for s in specs:
        desc = s.get("description", "")
        if s.get("async_mode"):
            desc += _ASYNC_TOOL_HINT
        result.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": desc,
                "parameters": s.get("input_schema", {"type": "object", "properties": {}, "required": []}),
            },
        })
    return result


def _filter_tool_specs(node: "Node", all_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
