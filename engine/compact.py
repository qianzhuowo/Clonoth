"""Context compaction -- engine core mechanism.

When an AI node's message list is too long, automatically compress old
messages into a structured summary, keeping the system prompt and the
most recent messages.

Fully transparent to theAI -- no tool invocation required.
"""
from __future__ import annotations

import logging
from typing import Any

from providers.base import BaseProvider, ProviderResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Summary prompts
# ---------------------------------------------------------------------------

_COMPACT_SYSTEM_PROMPT = (
    "You are a conversation summarizer. "
    "Output only the summary. Do not use any tools."
)

_COMPACT_USER_PROMPT = (
    "Please compress the following conversation history into a structured "
    "summary.\n\n"
    "Preserve:\n"
    "1. The user's original request and intent\n"
    "2. Completed operations (file I/O, commands, etc.) and key results\n"
    "3. Errors encountered and how they were resolved\n"
    "4. Work currently in progress\n"
    "5. Pending tasks\n"
    "6. Important file paths, code snippets, command outputs\n\n"
    "Requirements:\n"
    "- Output summary only -- no analysis process\n"
    "- Keep concrete file paths and key data\n"
    "- Organize chronologically\n\n"
    "Conversation history to compress:\n\n"
    "{conversation}"
)


# ---------------------------------------------------------------------------
#  Public API
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


async def compact_messages(
    provider: BaseProvider,
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 6,
) -> list[dict[str, Any]]:
    """Compress *messages*.  Keep system prompt + last *keep_recent*;
    summarize everything in between.

    On summary failure the original list is returned unchanged.
    """
    if len(messages) <= keep_recent + 2:
        return messages

    # --- split three segments ---
    if messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        body = messages[1:]
    else:
        system_msg = None
        body = list(messages)

    if len(body) <= keep_recent:
        return messages

    to_compress = body[:-keep_recent]
    to_keep = body[-keep_recent:]

    conversation_text = _format_messages_for_summary(to_compress)
    if not conversation_text.strip():
        return messages

    summary = await _call_summary_llm(provider, conversation_text)
    if summary is None:
        return messages

    summary_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            "[System: below is a summary of the earlier conversation; "
            "original context has been compacted]\n\n" + summary
        ),
    }

    result: list[dict[str, Any]] = []
    if system_msg is not None:
        result.append(system_msg)
    result.append(summary_msg)
    result.extend(to_keep)

    logger.info(
        "compact: %d -> %d messages (compressed %d middle messages)",
        len(messages), len(result), len(to_compress),
    )
    return result


# ---------------------------------------------------------------------------
#  Internal helpers
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


async def _call_summary_llm(
    provider: BaseProvider,
    conversation_text: str,
) -> str | None:
    """Call LLM for summary.  Returns None on any failure."""
    user_content = _COMPACT_USER_PROMPT.format(conversation=conversation_text)
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        resp: ProviderResponse = await provider.chat(messages=msgs, tools=None)
    except Exception as exc:
        logger.warning("compact: LLM call exception: %s", exc)
        return None
    if not resp.ok:
        logger.warning("compact: LLM error: %s", resp.error)
        return None
    text = (resp.text or "").strip()
    if not text:
        logger.warning("compact: LLM returned empty summary")
        return None
    return text
