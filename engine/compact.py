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
#  Auto-compact circuit breaker (session-level)
#  [2026-04-24] P1.5 熔断器：连续 N 次压缩失败后暂停自动压缩，
#  避免每个新 task 的 _LoopState 重置 compacted=False 后反复浪费 API 调用。
#  _compact_failures 是 module-level dict，进程重启自然清零，无需持久化。
# ---------------------------------------------------------------------------

_MAX_CONSECUTIVE_FAILURES = 3
_compact_failures: dict[str, int] = {}  # session_id → consecutive failure count


def record_compact_failure(session_id: str) -> None:
    """Record a compact failure. After MAX failures, is_compact_circuit_open returns True."""
    count = _compact_failures.get(session_id, 0) + 1
    _compact_failures[session_id] = count
    if count >= _MAX_CONSECUTIVE_FAILURES:
        logger.warning(
            "Compact circuit breaker tripped for session %s after %d consecutive failures",
            session_id, count,
        )


def record_compact_success(session_id: str) -> None:
    """Reset failure counter on success."""
    _compact_failures.pop(session_id, None)


def is_compact_circuit_open(session_id: str) -> bool:
    """Return True if compact should be skipped (too many consecutive failures)."""
    return _compact_failures.get(session_id, 0) >= _MAX_CONSECUTIVE_FAILURES


# ---------------------------------------------------------------------------
#  P1 Microcompact — time-based tool_result cleanup
#  When last assistant message is older than gap_minutes (cache expired),
#  clear old tool_result contents, keeping only the most recent ones.
# ---------------------------------------------------------------------------

_MICROCOMPACT_PLACEHOLDER = "[tool result cleared — cache expired]"


def microcompact_messages(
    messages: list[dict[str, Any]],
    *,
    gap_minutes: int = 60,
    keep_recent: int = 5,
    min_tool_results: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """Time-based microcompact: clear old tool_result contents.

    Triggered when the last assistant message is older than gap_minutes,
    indicating the provider's prompt cache has likely expired.
    Clears tool_result content for all but the most recent `keep_recent` results.

    Returns (messages, cleared_count). Messages are modified in-place.
    """
    from datetime import datetime, timezone

    # Find last assistant message timestamp
    last_assistant_ts = None
    for msg in reversed(messages):
        meta = msg.get("_meta", {})
        if isinstance(meta, dict):
            mt = meta.get("message_type", "")
            if mt == "assistant" or msg.get("role") == "assistant":
                ts_str = meta.get("timestamp", "")
                if ts_str:
                    try:
                        last_assistant_ts = datetime.fromisoformat(ts_str)
                        if last_assistant_ts.tzinfo is None:
                            last_assistant_ts = last_assistant_ts.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                break

    if last_assistant_ts is None:
        return messages, 0

    now = datetime.now(timezone.utc)
    gap = (now - last_assistant_ts).total_seconds() / 60.0
    if gap < gap_minutes:
        return messages, 0

    # Collect tool_result indices
    tr_indices: list[int] = []
    for i, msg in enumerate(messages):
        meta = msg.get("_meta", {})
        is_tr = False
        if isinstance(meta, dict) and meta.get("message_type") == "tool_result":
            is_tr = True
        elif isinstance(msg.get("content"), str) and msg["content"].startswith('Tool result for "'):
            is_tr = True
        if is_tr:
            tr_indices.append(i)

    if len(tr_indices) < min_tool_results:
        return messages, 0

    # Clear all but the last `keep_recent` tool results
    to_clear = tr_indices[:-keep_recent] if keep_recent > 0 else tr_indices
    cleared = 0
    for idx in to_clear:
        content = messages[idx].get("content", "")
        if isinstance(content, str) and content != _MICROCOMPACT_PLACEHOLDER:
            # Preserve the tool name header, clear the body
            first_line = content.split("\n", 1)[0]
            messages[idx]["content"] = first_line + "\n" + _MICROCOMPACT_PLACEHOLDER
            cleared += 1

    if cleared:
        logger.info(
            "microcompact: cleared %d/%d tool_results (gap=%.0fmin, kept=%d recent)",
            cleared, len(tr_indices), gap, keep_recent,
        )

    return messages, cleared


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

    # P6.5 Metadata Preservation: 收集被压缩掉的消息所属的 source_task_id，
    # 存入摘要消息的 _meta 中，防止 L2 snip 因 ID 丢失而重复触发 L3 LLM 压缩。
    _compressed = conversation[:-keep_recent] if keep_recent > 0 else conversation
    _compressed_tids = set()
    for _m in _compressed:
        _tid = _m.get("_meta", {}).get("source_task_id")
        if _tid:
            _compressed_tids.add(str(_tid))

    summary_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            "[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n" + summary
        ),
        "_meta": {
            "source_task_id": "compact_summary",
            "compressed_task_ids": list(_compressed_tids),
        }
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
