"""伪工具 spec 构建器和工具列表 helpers。

从 ai_step.py 中拆出，这些函数零外部依赖，纯数据结构构建。
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .node import Node


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

_PSEUDO_TOOL_NAMES = frozenset({"dispatch_node", "dispatch_nodes", "finish", "reply", "switch_node", "compact_context", "preempt_task"})


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

    【Fix 4】description 重写，与 engine/inference/pseudo_tools.py 同步。
    强调 text 是实际交付载荷而非状态汇报，并显式声明自由正文不会送达调用方。
    """
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final result and terminate this node immediately.\n\n"
                "CRITICAL: Once you call finish, the node exits. No further tool calls "
                "will be executed — not in this turn, not after. If you still have tools "
                "to call, call them FIRST, then finish in a later turn.\n\n"
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
                        "description": "The final result text, or a specific question when more information is needed.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief summary (optional) to help the upstream node quickly understand the result.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to attach (optional, used only when there are images or other attachments).",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _reply_spec() -> dict:
    """Build the reply pseudo-tool spec (non-terminating).

    【Fix 4】description 重写，与 engine/inference/pseudo_tools.py 同步。
    显式声明 text 是 user-facing 内容，自由正文不会送达用户。
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
                        "description": "Task ID to interrupt (full or prefix). Can be obtained from dispatch callbacks or active_tasks.",
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
                        "description": "Target node ID. Pass an empty string to restore the default entry node.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Reply text for the user (e.g. 'Switched to the coding node').",
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
