"""Context compaction -- engine core mechanism.

When an AI node's message list is too long, automatically compress old
messages into a structured summary, keeping the system prompt and the
most recent messages.

Fully transparent to the AI -- no tool invocation required.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from providers.base import BaseProvider, ProviderResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Summary prompts
# ---------------------------------------------------------------------------

_COMPACT_SYSTEM_PROMPT = (
    "你是一个对话摘要生成器。只输出摘要内容，不要调用任何工具。"
)

_COMPACT_USER_PROMPT = """\
你的任务是对以下对话历史创建详细的结构化摘要，用于在上下文压缩后恢复工作。

先在 <analysis> 标签中按时间顺序分析对话，确保覆盖所有要点：
1. 识别用户的每个请求和意图
2. 记录具体的文件名、代码片段、函数签名
3. 记录遇到的错误及修复方式
4. 特别注意用户的反馈（尤其是用户要求改变做法的地方）
5. 检查技术细节的准确性和完整性

然后在 <summary> 标签中输出最终摘要，包含以下段落：

1. 用户请求和意图：完整描述用户的所有请求
2. 关键技术概念：列出讨论过的技术、框架、模式
3. 文件和代码：列出查看、修改、创建过的文件，附重要代码片段和变更原因
4. 错误和修复：列出遇到的错误、修复方式、用户反馈
5. 所有用户消息：列出所有非工具结果的用户消息（理解意图变化的关键）
6. 待处理任务：列出尚未完成的任务
7. 当前工作：精确描述压缩前正在做的事，包含文件名和代码片段
8. 下一步：列出接下来要做的事（必须直接对应用户最近的请求）

示例格式：

<analysis>
[按时间顺序分析每条消息，确保覆盖所有要点]
</analysis>

<summary>
1. 用户请求和意图：
   [详细描述]

2. 关键技术概念：
   - [概念 1]
   - [概念 2]

3. 文件和代码：
   - [文件名]
     - [变更原因]
     - [重要代码片段]

4. 错误和修复：
   - [错误描述]：
     - [修复方式]

5. 所有用户消息：
   - [用户消息 1]
   - [用户消息 2]

6. 待处理任务：
   - [任务 1]

7. 当前工作：
   [精确描述]

8. 下一步：
   [下一步行动]
</summary>

需要压缩的对话历史：

{conversation}"""


# ---------------------------------------------------------------------------
#  Summary formatting
# ---------------------------------------------------------------------------

def _format_compact_summary(raw_summary: str) -> str:
    """从 LLM 输出中剥离 <analysis> 草稿区，提取 <summary> 内容。"""
    text = raw_summary

    # 剥离 <analysis> 块
    text = re.sub(r'<analysis>[\s\S]*?</analysis>', '', text)

    # 提取 <summary> 内容
    m = re.search(r'<summary>([\s\S]*?)</summary>', text)
    if m:
        text = m.group(1).strip()
    else:
        # 没有 <summary> 标签，用去掉 <analysis> 后的全文
        text = text.strip()

    # 清理多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
    # 收集开头所有 system 消息（可能有多条：静态段 + 动态段）
    system_msgs: list[dict[str, Any]] = []
    body = list(messages)
    while body and body[0].get("role") == "system":
        system_msgs.append(body.pop(0))

    if len(body) <= keep_recent:
        return messages

    to_compress = body[:-keep_recent]
    to_keep = body[-keep_recent:]

    conversation_text = _format_messages_for_summary(to_compress)
    if not conversation_text.strip():
        return messages

    raw_summary = await _call_summary_llm(provider, conversation_text)
    if raw_summary is None:
        return messages

    # 剥离 <analysis>，提取 <summary>
    summary = _format_compact_summary(raw_summary)
    if not summary:
        return messages

    summary_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            "[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n" + summary
        ),
    }

    result: list[dict[str, Any]] = []
    if system_msgs:
        result.extend(system_msgs)
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
