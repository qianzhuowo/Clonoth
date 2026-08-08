"""Pure attachment-selection policy for the OneBot adapter.

Kept free of NoneBot imports so the ambiguity rules can be regression-tested even
when optional platform dependencies are not installed in the core test environment.
"""
from __future__ import annotations

from typing import Any, Iterable


def looks_like_image_query(text: str) -> bool:
    """Detect explicit image references without matching generic text requests."""
    value = str(text or "").strip().lower()
    if not value:
        return False
    keywords = (
        "这张图", "那张图", "上图", "原图", "图里", "图中", "看图", "看看图",
        "看一下图", "识图", "读图", "图片", "截图", "照片", "ocr", "表情包",
        "image", "photo", "screenshot",
    )
    return any(keyword in value for keyword in keywords)


def should_fallback_to_recent_images(
    *,
    has_attachments: bool,
    image_input_enabled: bool,
    looks_like_image_query: bool,
    reply_message_id: Any,
) -> bool:
    """Allow implicit recent-image lookup only for non-reply image queries."""
    return bool(
        not has_attachments
        and image_input_enabled
        and looks_like_image_query
        and reply_message_id is None
    )


def source_attachments_from_merged(attachments: Iterable[Any]) -> list[dict[str, Any]]:
    """Copy and de-duplicate attachments used by a merged queued task."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        key = (str(attachment.get("type") or ""), str(attachment.get("path") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(dict(attachment))
    return result


def select_recent_image_entries(
    entries: Iterable[Any],
    *,
    sender_id: str,
    now: float,
    max_age_seconds: float,
    max_images: int,
) -> list[dict[str, Any]]:
    """Select the latest single-message image batch from the current sender.

    Never falls back across senders and never combines separate QQ messages. This
    prevents a follow-up such as “再仔细看看图” from silently receiving an older,
    unrelated image.
    """
    eligible = [
        item
        for item in entries
        if str(getattr(item, "sender_id", "") or "") == str(sender_id or "")
        and now - float(getattr(item, "created_at", 0.0) or 0.0) <= max_age_seconds
    ]
    if not eligible or max_images <= 0:
        return []

    latest_message_id = str(getattr(eligible[-1], "message_id", "") or "")
    if latest_message_id:
        eligible = [
            item
            for item in eligible
            if str(getattr(item, "message_id", "") or "") == latest_message_id
        ]
    else:
        eligible = eligible[-1:]

    selected = eligible[-max_images:]
    return [
        dict(getattr(item, "attachment", {}) or {})
        for item in selected
        if isinstance(getattr(item, "attachment", None), dict)
    ]
