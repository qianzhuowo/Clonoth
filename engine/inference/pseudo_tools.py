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

_PSEUDO_TOOL_NAMES = frozenset({"dispatch_node", "dispatch_nodes", "finish", "reply", "switch_node", "compact_context", "preempt_task"})

# [RFC 2026-04-20] finish 升级为真实 API 工具：tool_result 固定内容。
# 仅用于满足 API 的 tool_use/tool_result 配对校验和下一轮对话历史格式合法性。
# finish 调用后循环即终止，模型不会看到此内容。
FINISH_TOOL_RESULT_CONTENT = "completed"


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
                    "context_mode": {
                        "type": "string",
                        "enum": ["fresh", "fork", "accumulate"],
                        "description": "子节点上下文模式。fresh=每次从零无历史；fork=继承父节点对话历史；accumulate=首次从零后续恢复自己的上下文（默认accumulate）",
                    },
                    "context_key": {
                        "type": "string",
                        "description": "上下文继承标识（仅 accumulate 模式有效）。用于精确指定继承哪个实例的上下文历史。同一 context_key 的历次任务共享上下文链。不填则按 target 节点 ID 查找（默认行为）。并发 dispatch 同一 target 多个实例时，应为每个实例指定不同的 context_key。",
                    },
                    # [2026-04-22] 新增 attachment_paths：允许父节点将文件附件传递给子节点。
                    # 路径为 workspace-relative，图片注入为多模态内容，其他文件作为引用。
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths (relative to workspace root) to attach to the child node's initial context. Images are injected as multimodal content; other files as references the child can read_file.",
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
                                "context_mode": {
                                    "type": "string",
                                    "enum": ["fresh", "fork", "accumulate"],
                                    "description": "子节点上下文模式。fresh=每次从零；fork=继承父节点历史；accumulate=首次从零后续恢复（默认accumulate）",
                                },
                                "context_key": {
                                    "type": "string",
                                    "description": "上下文继承标识（仅 accumulate 模式有效）。同一 context_key 的历次任务共享上下文链。并发 dispatch 同一 target 多个实例时，应为每个实例指定不同的 context_key。",
                                },
                                # [2026-04-22] 新增 attachment_paths：与 dispatch_node 同理。
                                "attachment_paths": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "File paths (relative to workspace root) to attach to this child node instance.",
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
                "If you call finish alongside execute_command, save_memory, dispatch_node, or any other tool, ALL calls will be REJECTED and you must retry. "
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
