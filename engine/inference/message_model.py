"""统一消息模型与元数据。

为消息和上下文快照提供结构化元数据，为后续跨 provider 思维链保留、
JSON tool mode 等功能做准备。

设计原则：
  - 不破坏现有代码：所有元数据字段可选，现有 dict 消息格式完全兼容
  - 渐进式采用：新代码可以开始写入 meta，旧代码忽略即可
  - 快照格式向后兼容：v1 快照无 meta 字段，加载时自动补空
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
#  消息级元数据
# ---------------------------------------------------------------------------

@dataclass
class MessageMeta:
    """单条消息的元数据，持久化到 context snapshot。

    这些信息不发送给 LLM，仅用于引擎内部追踪和跨 provider 兼容。

    分两层：
      - Raw 层 (raw_parts)：原始 provider 响应结构，仅 engine 使用
      - 提取层 (thinking_text, has_thinking, inline_data)：写入时从 raw_parts
        一次性提取的便捷字段，供 bot/前端/debug 使用
    """
    # ── 通用字段 ──
    provider: str = ""              # 生成该消息的 provider (claude/gemini/openai)
    tool_mode: str = "native"       # native / json — 该消息使用的工具调用格式
    message_type: str = ""          # user_input / tool_result / assistant / system
    timestamp: str = ""             # ISO 格式生成时间

    # ── Raw 层（仅 engine 使用）──
    raw_parts: list = field(default_factory=list)
    # 原始 provider 响应的 content blocks / parts 数组，原样保存
    # Claude: [{type: "thinking", text, signature}, {type: "text", text}, {type: "tool_use", ...}]
    # Gemini: [{text, thoughtSignature}, {functionCall, thoughtSignature}, {inlineData}]
    # OpenAI: [{...}] 或为空

    tool_call_ids: list[str] = field(default_factory=list)
    # 原始 tool_call id 列表（从 raw_parts 也能提取，但常用所以冗余一份）

    # ── 提取层（bot/前端可读，写入时从 raw_parts 一次性提取）──
    thinking_text: str = ""         # 思维链正文（给 debug/展示用）
    has_thinking: bool = False      # 快速判断是否含思维链
    inline_data: list = field(default_factory=list)
    # 附件引用列表（图片等 inlineData 的 mime_type + 引用信息，不含原始 base64）

    usage: dict = field(default_factory=dict)
    # LLM 响应的 token usage，仅 assistant 消息有值
    # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict。省略空值/默认值字段以节省空间。"""
        d: dict[str, Any] = {}
        if self.provider:
            d["provider"] = self.provider
        if self.tool_mode and self.tool_mode != "native":
            d["tool_mode"] = self.tool_mode
        if self.message_type:
            d["message_type"] = self.message_type
        if self.timestamp:
            d["timestamp"] = self.timestamp
        if self.raw_parts:
            d["raw_parts"] = self.raw_parts
        if self.tool_call_ids:
            d["tool_call_ids"] = list(self.tool_call_ids)
        if self.thinking_text:
            d["thinking_text"] = self.thinking_text
        if self.has_thinking:
            d["has_thinking"] = True
        if self.inline_data:
            d["inline_data"] = self.inline_data
        if self.usage:
            d["usage"] = self.usage
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageMeta:
        """从 dict 反序列化。向后兼容旧快照中的 thinking_signature 字段（忽略）。"""
        if not isinstance(data, dict):
            return cls()
        return cls(
            provider=str(data.get("provider") or ""),
            tool_mode=str(data.get("tool_mode") or "native"),
            message_type=str(data.get("message_type") or ""),
            timestamp=str(data.get("timestamp") or ""),
            raw_parts=list(data.get("raw_parts") or []),
            tool_call_ids=list(data.get("tool_call_ids") or []),
            thinking_text=str(data.get("thinking_text") or ""),
            has_thinking=bool(data.get("has_thinking")),
            inline_data=list(data.get("inline_data") or []),
            usage=dict(data.get("usage") or {}),
        )


# ---------------------------------------------------------------------------
#  快照级元数据
# ---------------------------------------------------------------------------

@dataclass
class SnapshotMeta:
    """上下文快照的全局元数据。

    存储于 snapshot["meta"] 字段中，与现有 v1 格式兼容（v1 无此字段）。
    """
    provider: str = ""           # 当前使用的 provider
    tool_mode: str = "native"    # 当前工具调用格式
    message_metas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # key = 消息索引（字符串），value = MessageMeta.to_dict()
    # 只记录有实际元数据的消息，空 meta 不占位

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict。"""
        d: dict[str, Any] = {}
        if self.provider:
            d["provider"] = self.provider
        if self.tool_mode and self.tool_mode != "native":
            d["tool_mode"] = self.tool_mode
        if self.message_metas:
            d["message_metas"] = dict(self.message_metas)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SnapshotMeta:
        """从 dict 反序列化。None 或非 dict 返回空实例。"""
        if not isinstance(data, dict):
            return cls()
        return cls(
            provider=str(data.get("provider") or ""),
            tool_mode=str(data.get("tool_mode") or "native"),
            message_metas=dict(data.get("message_metas") or {}),
        )


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------

def get_message_meta(message: dict[str, Any]) -> MessageMeta:
    """从消息 dict 中提取 _meta 字段，返回 MessageMeta 实例。

    如果消息没有 _meta 字段，返回空的 MessageMeta。
    """
    raw = message.get("_meta")
    if isinstance(raw, MessageMeta):
        return raw
    if isinstance(raw, dict):
        return MessageMeta.from_dict(raw)
    return MessageMeta()


def set_message_meta(message: dict[str, Any], meta: MessageMeta) -> None:
    """将 MessageMeta 写入消息 dict 的 _meta 字段。

    如果 meta 为空（所有字段都是默认值），不写入以保持消息干净。
    """
    d = meta.to_dict()
    if d:
        message["_meta"] = d
    elif "_meta" in message:
        del message["_meta"]


def strip_internal_fields(message: dict[str, Any]) -> dict[str, Any]:
    """剥离所有以 _ 开头的内部标记，返回可发送给 LLM 的干净消息。

    包括: _dynamic, _ephemeral, _meta 等。
    """
    return {k: v for k, v in message.items() if not k.startswith("_")}


def build_snapshot_dict(
    *,
    node_id: str,
    messages: list[dict[str, Any]],
    step_count: int,
    meta: SnapshotMeta | None = None,
) -> dict[str, Any]:
    """构建上下文快照 dict，兼容 v1 格式。

    如果提供了 meta 且非空，写入 snapshot["meta"] 字段（v2 扩展）。
    """
    snapshot: dict[str, Any] = {
        "version": 1,
        "node_id": node_id,
        "messages": messages,
        "step_count": int(step_count),
    }
    if meta is not None:
        meta_dict = meta.to_dict()
        if meta_dict:
            snapshot["meta"] = meta_dict
    return snapshot


def load_snapshot_meta(snapshot: dict[str, Any]) -> SnapshotMeta:
    """从快照 dict 中提取 SnapshotMeta。v1 快照无 meta 字段时返回空实例。"""
    return SnapshotMeta.from_dict(snapshot.get("meta"))
