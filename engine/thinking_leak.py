"""Split thinking that a model wrote into its visible reply.

Some OpenAI-compatible gateways in front of Gemini return the model's thought text
inside ``content`` instead of a reasoning field. Observed shapes:

    "ির" + "thinking\\n<thought...></body><reply>"     (current LL/gemini-4-argon)
    "<thinking>\\n<thought...></thinking><reply>"       (older history)

Detection only looks at the first line: optional non-ASCII garbage or ``<`` followed
by ``thinking`` and an optional ``>``, alone on that line. Ordinary answers (including
HTML answers that contain ``</body>``) never start that way, so they are untouched.

The thought ends at the first closing marker (``</body>`` or ``</thinking>``) that is
not inside a Markdown code span or fence. Without a closing marker the whole text is
thought: with tool calls the reply text simply becomes empty; without tool calls the
empty reply makes the inference loop retry instead of sending a half-thought.
"""
from __future__ import annotations

import re
from typing import Any

_OPEN_RE = re.compile(r"^\s*(?:[^\x00-\x7f]{0,6}|<)thinking>?[ \t]*\r?\n", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</body>|</thinking>", re.IGNORECASE)
_CODE_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`", re.DOTALL)


def _first_close_outside_code(text: str) -> re.Match[str] | None:
    code = [m.span() for m in _CODE_RE.finditer(text)]
    for m in _CLOSE_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in code):
            return m
    return None


def split_leaked_thinking(text: str | None) -> tuple[str, str] | None:
    """Return ``(thought, reply)`` when ``text`` starts with leaked thinking, else None."""
    if not text:
        return None
    opened = _OPEN_RE.match(text)
    if not opened:
        return None
    body = text[opened.end():]
    close = _first_close_outside_code(body)
    if close is None:
        return body.strip(), ""
    return body[:close.start()].strip(), body[close.end():].lstrip()


def move_leaked_thinking(resp: Any) -> bool:
    """Move leaked thinking from ``resp.text`` into ``resp.reasoning``. Returns True if moved."""
    split = split_leaked_thinking(getattr(resp, "text", None))
    if split is None:
        return False
    thought, reply = split
    resp.text = reply
    resp.reasoning = "\n\n".join(p for p in (getattr(resp, "reasoning", None) or "", thought) if p) or None
    resp.provider_meta["_leaked_thinking_moved"] = True
    return True
