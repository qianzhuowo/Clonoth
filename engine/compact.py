"""Context compaction -- pure utility functions.

Provides helper functions for context compression:
- should_compact: check if messages exceed token threshold
- apply_compact_summary: apply a pre-generated summary to compress messages
- _format_messages_for_summary: format messages into text for summary LLM
- _format_compact_summary: extract <summary> from LLM output

No LLM calls in this module.  The actual summarization is done by the
system.compactor node, dispatched via supervisor task queue.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Summary formatting
# ---------------------------------------------------------------------------

def _format_compact_summary(raw_summary: str) -> str:
    """从 LLM 输出中剥离 <analysis> 草稿区，提取 <summary> 内容。

    返回空字符串表示摘要不合格（找不到标签、长度不足等），
    调用方应视为压缩失败，不采纳摘要，不重置上下文。
    """
    text = raw_summary

    # 剥离 <analysis> 块
    text = re.sub(r'<analysis>[\s\S]*?</analysis>', '', text)

    # 提取 <summary> 内容
    m = re.search(r'<summary>([\s\S]*?)</summary>', text)
    if m:
        text = m.group(1).strip()
    else:
        # 没有 <summary> 标签 → 视为压缩失败，不采纳
        logger.warning("compact summary rejected: no <summary> tag found")
        return ""

    # 清理多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    # 最低长度校验：摘要不足 200 字符视为垃圾摘要，不采纳
    if len(text) < 200:
        logger.warning(
            "compact summary rejected: too short (%d chars, minimum 200)",
            len(text),
        )
        return ""

    return text


# ---------------------------------------------------------------------------
#  Threshold check
# ---------------------------------------------------------------------------

def should_compact(
    messages: list[dict[str, Any]],
    threshold_tokens: int,
    last_prompt_tokens: int | None = None,
) -> bool:
    """Return True if estimated/actual token count exceeds *threshold_tokens*.

    If *last_prompt_tokens* (from the previous LLM response's usage) is
    available, use that directly.  Otherwise, estimate from character count
    (~3 chars per token for mixed CJK/English content).
    """
    if threshold_tokens <= 0:
        return False
    if last_prompt_tokens is not None:
        return last_prompt_tokens > threshold_tokens
    # Fallback: estimate from characters
    total_chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
        if total_chars // 3 > threshold_tokens:
            return True
    return total_chars // 3 > threshold_tokens


# ---------------------------------------------------------------------------
#  Apply summary (pure function, no LLM)
# ---------------------------------------------------------------------------

def apply_compact_summary(
    messages: list[dict[str, Any]],
    summary: str,
    *,
    keep_recent: int = 6,
) -> list[dict[str, Any]]:
    """Apply a pre-generated summary to compress messages.

    Same split logic as the old compact_messages, but takes
    the summary text directly instead of calling LLM.

    Returns compressed message list.  If summary is empty or
    messages too short, returns original list unchanged.
    """
    if not summary or len(messages) <= keep_recent + 2:
        return messages

    # --- split segments ---
    # 1. 收集开头的 system 消息（静态前缀：静态 prompt + 常驻 skills/memory）
    prefix_systems: list[dict[str, Any]] = []
    body = list(messages)
    while body and body[0].get("role") == "system":
        prefix_systems.append(body.pop(0))

    # 2. 分离 body 中嵌入的 system 消息（动态 prompt/skills/memory）与对话消息
    conversation: list[dict[str, Any]] = []
    inner_systems: list[dict[str, Any]] = []
    for msg in body:
        if msg.get("role") == "system":
            inner_systems.append(msg)
        else:
            conversation.append(msg)

    if len(conversation) <= keep_recent:
        return messages

    to_keep = conversation[-keep_recent:] if keep_recent > 0 else []

    summary_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            "[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n" + summary
        ),
    }

    result: list[dict[str, Any]] = []
    result.extend(prefix_systems)
    result.append(summary_msg)
    result.extend(inner_systems)
    result.extend(to_keep)

    logger.info(
        "apply_compact_summary: %d -> %d messages (compressed %d middle messages)",
        len(messages), len(result), len(conversation) - keep_recent,
    )
    return result


# ---------------------------------------------------------------------------
#  Message formatting (for summary LLM input)
# ---------------------------------------------------------------------------

def _format_messages_for_summary(
    messages: list[dict[str, Any]],
) -> str:
    """Format messages into plain text for the summary LLM."""
    parts: list[str] = []
    for m in messages:
        role = str(m.get("role", "unknown"))
        content = m.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str):
                        text_parts.append(t)
            text = "\n".join(text_parts)
        else:
            text = str(content)
        if len(text) > 20_000:
            text = text[:20_000] + "\n...[truncated]"
        parts.append(f"[{role}]\n{text}")
    return "\n\n---\n\n".join(parts)
