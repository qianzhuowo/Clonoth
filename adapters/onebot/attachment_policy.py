"""Pure attachment-selection policy for the OneBot adapter.

Kept free of NoneBot imports so the ambiguity rules can be regression-tested even
when optional platform dependencies are not installed in the core test environment.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

_HTTP_SOURCE_RE = re.compile(r"^https?://", re.IGNORECASE)
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_downloadable_file_source(value: Any) -> bool:
    """Return whether a OneBot file field is an actual URL or absolute path.

    NapCat commonly puts the display filename in ``data.file``. Treating that
    value as a URL made httpx report a missing protocol and was then surfaced as
    a misleading "temporary link expired" error.
    """
    source = str(value or "").strip()
    return bool(
        _HTTP_SOURCE_RE.match(source)
        or source.startswith("file://")
        or source.startswith("/")
        or _WINDOWS_ABS_RE.match(source)
    )


def normalise_file_item(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract file metadata without confusing a display filename for a source."""
    url = str(data.get("url") or data.get("path") or "").strip()
    raw_file = str(data.get("file") or "").strip()
    source = url if is_downloadable_file_source(url) else ""
    if not source and is_downloadable_file_source(raw_file):
        source = raw_file

    name = str(
        data.get("name")
        or data.get("file_name")
        or data.get("filename")
        or ("" if is_downloadable_file_source(raw_file) else raw_file)
    ).strip()
    if not name and source:
        clean_source = source.split("?", 1)[0].split("#", 1)[0]
        if _WINDOWS_ABS_RE.match(clean_source):
            name = PureWindowsPath(clean_source).name
        else:
            name = PurePosixPath(clean_source.replace("file://", "", 1)).name

    file_id = str(data.get("file_id") or data.get("id") or "").strip()
    if not source and not name and not file_id:
        return None

    size_raw = data.get("size") or data.get("file_size") or data.get("filesize")
    try:
        size = int(size_raw) if size_raw is not None and str(size_raw).strip() else 0
    except (TypeError, ValueError):
        size = 0

    busid = data["busid"] if "busid" in data else data.get("bus_id")
    return {
        "source": source,
        "name": name,
        "size": size,
        "file_id": file_id,
        "busid": busid,
        "file_hash": str(data.get("file_hash") or data.get("hash") or "").strip(),
    }


def is_outbound_or_self_event(*, post_type: Any, user_id: Any, self_ids: Iterable[Any]) -> bool:
    """Identify OneBot echoes/system completions that must not enter the Agent."""
    if str(post_type or "").strip().lower() == "message_sent":
        return True
    uid = str(user_id or "").strip()
    return bool(uid and uid in {str(item or "").strip() for item in self_ids})


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
