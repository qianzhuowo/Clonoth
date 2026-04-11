"""Attachment handling utilities for multimodal message support.

All image data in the system is stored as files under data/attachments/.
Messages reference images via file:// URLs (e.g. file://data/attachments/xxx/yyy.png).
Before sending to the LLM provider, file:// refs are resolved to base64 data URLs.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any


_logger = logging.getLogger(__name__)

_FILE_SCHEME = "file://"
_ALLOWED_PREFIX = "data/attachments/"

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# 单图最大 10 MB
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Discord CDN 等来源经常返回无意义的 MIME，需要清洗掉以便 fallback 到扩展名猜测
_USELESS_MIMES = frozenset({
    "application/octet-stream",
    "binary/octet-stream",
})


def _sanitize_mime(mime_type: str) -> str:
    """清洗无意义的 MIME 类型，返回空字符串以触发扩展名猜测。"""
    if not mime_type or mime_type.strip() in _USELESS_MIMES:
        return ""
    return mime_type.strip()


def _guess_mime(ext: str) -> str:
    """根据扩展名猜测 MIME。

    已知图片扩展名走 _MIME_MAP；其余走 stdlib mimetypes；
    都猜不出的兜底为 application/octet-stream。
    """
    lower = ext.lower()
    if lower in _MIME_MAP:
        return _MIME_MAP[lower]
    guessed = mimetypes.guess_type(f"file{lower}")[0]
    return guessed or "application/octet-stream"


def _guess_mime_from_path(path: str) -> str:
    """根据路径扩展名猜测 MIME。

    仅用于 LLM 图片 base64 编码场景，未知扩展名仍兜底为 image/png。
    """
    ext = Path(path).suffix.lower()
    return _MIME_MAP.get(ext, "image/png")


def _is_allowed_attachment_path(rel_path: str) -> bool:
    """Only allow paths under data/attachments/."""
    normalized = rel_path.replace("\\", "/").lstrip("/")
    return normalized.startswith(_ALLOWED_PREFIX)


def save_attachment(
    workspace_root: Path,
    session_id: str,
    data_bytes: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
) -> dict[str, Any]:
    """Save attachment bytes to data/attachments/{session_id}/. Returns attachment dict."""
    # 清洗无意义的 MIME（如 Discord CDN 返回的 application/octet-stream）
    mime_type = _sanitize_mime(mime_type)

    sid = (session_id or "unknown").strip() or "unknown"
    d = workspace_root / "data" / "attachments" / sid
    d.mkdir(parents=True, exist_ok=True)

    ext = ""
    if filename:
        ext = Path(filename).suffix.lower()
    if not ext and mime_type:
        ext = mimetypes.guess_extension(mime_type) or ""
    if not ext:
        ext = ".bin"

    name = f"{uuid.uuid4().hex}{ext}"
    p = d / name
    p.write_bytes(data_bytes)

    rel = p.relative_to(workspace_root).as_posix()
    detected_mime = mime_type or _guess_mime(ext)
    return {
        "type": "image" if detected_mime.startswith("image/") else "file",
        "path": rel,
        "mime_type": detected_mime,
        "name": filename or name,
    }


def attachments_to_content_parts(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert attachment dicts to OpenAI multimodal content parts (with file:// references).

    Only paths under data/attachments/ are accepted.
    """
    parts: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        path = str(att.get("path") or "").strip()
        if not path:
            continue
        if not _is_allowed_attachment_path(path):
            continue
        url = f"{_FILE_SCHEME}{path}"
        parts.append({
            "type": "image_url",
            "image_url": {"url": url},
        })
    return parts


def build_multimodal_content(
    text: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]] | str:
    """Build multimodal content for a message. Returns plain str if no image attachments."""
    image_parts = attachments_to_content_parts(attachments)
    if not image_parts:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts.extend(image_parts)
    return parts


def _strip_image_parts_for_assistant(content: list[dict[str, Any]]) -> list[dict[str, Any]] | str:
    """从 assistant 消息的 content 中剥掉所有 image_url 部分。

    Claude API 不允许 assistant turn 包含 image block，会报：
    'image' blocks are not permitted within assistant turns.
    """
    text_parts = [p for p in content if isinstance(p, dict) and p.get("type") != "image_url"]
    if not text_parts:
        # 全是图片，返回占位文本
        return "[image attachment]"
    if len(text_parts) == 1 and text_parts[0].get("type") == "text":
        # 只剩一个 text part，展平为纯字符串
        return text_parts[0].get("text", "")
    return text_parts


def prepare_messages_for_llm(
    messages: list[dict[str, Any]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    """Return a copy of messages with file:// image refs resolved to base64 data URLs.

    Also strips image blocks from assistant messages (Claude API restriction).

    Uses a per-call cache keyed by relative path to avoid re-encoding the same
    image file multiple times within a single invocation.
    """
    cache: dict[str, str | None] = {}
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        role = msg.get("role", "")

        if not isinstance(content, list):
            result.append(msg)
            continue

        # --- assistant 消息：剥掉所有 image_url 部分 ---
        if role == "assistant":
            has_image = any(
                isinstance(p, dict) and p.get("type") == "image_url"
                for p in content
            )
            if has_image:
                new_msg = dict(msg)
                new_msg["content"] = _strip_image_parts_for_assistant(content)
                result.append(new_msg)
                _logger.debug("stripped image blocks from assistant message")
                continue
            # assistant 消息没有 image_url，正常处理
            result.append(msg)
            continue

        # --- user / system 等消息：正常解析 file:// 引用 ---
        needs_resolve = any(
            isinstance(part, dict)
            and part.get("type") == "image_url"
            and isinstance(part.get("image_url"), dict)
            and str(part["image_url"].get("url", "")).startswith(_FILE_SCHEME)
            for part in content
        )

        if not needs_resolve:
            result.append(msg)
            continue

        new_msg = dict(msg)
        new_content: list[dict[str, Any]] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and isinstance(part.get("image_url"), dict)
            ):
                url = str(part["image_url"].get("url", ""))
                if url.startswith(_FILE_SCHEME):
                    rel_path = url[len(_FILE_SCHEME):]
                    if rel_path in cache:
                        resolved = cache[rel_path]
                    else:
                        resolved = _resolve_file_url(url, workspace_root)
                        cache[rel_path] = resolved
                    if resolved:
                        new_content.append({
                            "type": "image_url",
                            "image_url": {"url": resolved},
                        })
                    else:
                        # 解析失败（文件不存在等），跳过这个图片，不传给 API。
                        # 原样传递 file:// URL 会导致 API 报 octet-stream 错误。
                        _logger.warning("attachment resolve failed, skipping: %s", rel_path)
                    continue  # 无论成功失败都 continue，不 fallback 到 append(part)
            new_content.append(part)  # 非 file:// 的 part 原样保留
        new_msg["content"] = new_content
        result.append(new_msg)

    return result


def _resolve_file_url(url: str, workspace_root: Path) -> str | None:
    """Resolve a file:// URL to a data: base64 URL."""
    rel_path = url[len(_FILE_SCHEME):]

    # Only allow paths under data/attachments/
    if not _is_allowed_attachment_path(rel_path):
        return None

    p = (workspace_root / rel_path).resolve()

    # Security: must be under workspace_root
    try:
        p.relative_to(workspace_root.resolve())
    except ValueError:
        return None

    if not p.exists() or not p.is_file():
        return None

    try:
        size = p.stat().st_size
        if size > _MAX_IMAGE_BYTES:
            return None
        data = p.read_bytes()
    except Exception:
        return None

    mime = _guess_mime_from_path(rel_path)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"
