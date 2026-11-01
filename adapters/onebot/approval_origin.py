"""Approval message header: which QQ window triggered the approval.

Kept free of nonebot imports so it can be unit-tested and reused by the
adapter's approval flow. The adapter resolves display names (group-name
cache, friend/stranger nickname, anonymous map) and passes them in; this
module only decides the wording.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

_MAX_NAME = 40


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:_MAX_NAME]


def origin_target_type(origin: Mapping[str, Any] | None) -> str:
    """Return "group" / "private" / "" from Approval.origin."""
    origin = origin or {}
    target_type = str(origin.get("target_type") or "").strip()
    if target_type in {"group", "private"}:
        return target_type
    key = str(origin.get("conversation_key") or "")
    if key.startswith("qq_group:"):
        return "group"
    if key.startswith("qq_private:"):
        return "private"
    return ""


def format_approval_origin(
    origin: Mapping[str, Any] | None,
    *,
    group_name: Optional[str] = None,
    group_id: Optional[int | str] = None,
    user_name: Optional[str] = None,
    user_id: Optional[int | str] = None,
) -> str:
    """Build the "来源" line, e.g. "【群】xx群" or "【私聊】xx".

    Names win over IDs; when nothing is resolvable the conversation key is
    shown so the admin still knows where to look.
    """
    origin = origin or {}
    target_type = origin_target_type(origin)
    requester = _clean(user_name) or _clean(user_id) or _clean(origin.get("user_id"))
    if target_type == "group":
        label = _clean(group_name) or (f"群 {_clean(group_id)}" if _clean(group_id) else "")
        label = label or _clean(origin.get("conversation_key")) or "未知群"
        line = f"【群】{label}"
        if requester:
            line += f"（发起人：{requester}）"
        return line
    if target_type == "private":
        return f"【私聊】{requester or _clean(origin.get('conversation_key')) or '未知用户'}"
    key = _clean(origin.get("conversation_key"))
    return f"【其他】{key}" if key else "【未知窗口】"
