from __future__ import annotations

import re
from typing import Any


def compile_keyword(kw: str) -> re.Pattern[str] | str:
    """Compile a skill or memory keyword entry.

    Why: skill and memory injection previously carried identical keyword
    compilers, which made future fixes easy to apply to only one path by
    accident. How: keep the exact legacy /pattern/flags parsing and substring
    fallback in one engine-level helper. Purpose: preserve existing activation
    behavior while removing duplicated matching code.
    """
    kw = (kw or "").strip()
    if not kw:
        return ""
    if kw.startswith("/"):
        last_slash = kw.rfind("/")
        if last_slash > 0:
            pattern = kw[1:last_slash]
            flags_str = kw[last_slash + 1:]
            flags = 0
            if "i" in flags_str:
                flags |= re.IGNORECASE
            if "s" in flags_str:
                flags |= re.DOTALL
            if "m" in flags_str:
                flags |= re.MULTILINE
            try:
                return re.compile(pattern, flags)
            except re.error:
                pass
    return kw.lower()


def match_keywords(compiled: list[re.Pattern[str] | str], text: str) -> bool:
    """Return True when any compiled keyword matches text.

    Why: skill and memory activation must continue to share identical matching
    semantics. How: run regex entries against the original text and literal
    entries against the lowercased text, matching the old duplicated code.
    Purpose: keep prompt injection decisions unchanged after the refactor.
    """
    if not compiled or not text:
        return False
    text_lower = text.lower()
    for kw in compiled:
        if not kw:
            continue
        if isinstance(kw, re.Pattern):
            if kw.search(text):
                return True
        elif kw in text_lower:
            return True
    return False


def build_scan_text(
    instruction_text: str,
    history: list[dict[str, Any]] | None,
    scan_depth: int,
) -> str:
    """Build the text scanned for keyword activation.

    Why: scan-depth history handling was duplicated between skill and memory
    injection. How: always include the current instruction and append the last
    scan_depth conversation rounds using the legacy user-message boundary rule.
    Purpose: keep activation byte-for-byte compatible while centralizing the
    shared matcher.
    """
    parts: list[str] = [instruction_text or ""]
    if history and scan_depth > 0:
        # Why: the old implementation defined a round as a user message that
        # starts after a non-user message. How: walk backward until enough round
        # starts are found. Purpose: preserve keyword activation scope exactly.
        round_starts: list[int] = []
        for i in range(len(history) - 1, -1, -1):
            role = history[i].get("role", "")
            if role != "user":
                continue
            if i == 0 or history[i - 1].get("role", "") != "user":
                round_starts.append(i)
                if len(round_starts) >= scan_depth:
                    break

        if round_starts:
            start_idx = round_starts[-1]
            for msg in history[start_idx:]:
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "\n".join(parts)
