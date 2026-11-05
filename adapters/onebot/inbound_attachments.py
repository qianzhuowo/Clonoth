"""Inbound QQ images/files -> Clonoth attachments.

Downloads land in ``data/attachments/<stable_conversation_key>/`` (the stable
key is a hash, never a real QQ/group id). Expiry of these files is handled
centrally by ``engine/data_cleanup.py``; this module does not delete files.

Also keeps two small indexes:
- ``RecentImages``: per conversation, for follow-ups like "再看看图";
- ``ReplyAttachmentCache``: message_id -> stored images, persisted, used when
  NapCat ``get_msg`` cannot resolve a quoted message.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Deque, Dict, List, Optional

import httpx

from . import config
from .attachment_policy import (
    is_downloadable_file_source,
    looks_like_image_query,
    normalise_file_item,
    select_recent_image_entries,
    should_fallback_to_recent_images,
)
from .message_parse import CQ_RE, extract_reply_message_id, get_reply_message, parse_cq_params, segment_type_and_data

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_IMAGE_SEGMENT_TYPES = {"image", "mface", "marketface"}
_MIME_EXT = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp"}
_LOCAL_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


# ---- pure helpers ---------------------------------------------------------------

def sniff_image_mime(content: bytes) -> str:
    """Image MIME from magic bytes; empty string when not a known image."""
    head = bytes(content[:16] or b"")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return ""


def guess_image_mime(url: str, content_type: str = "", content: bytes = b"") -> str:
    """Response header > magic bytes > URL suffix > image/jpeg."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith("image/"):
        return ct
    sniffed = sniff_image_mime(content)
    if sniffed:
        return sniffed
    suffix = Path((url or "").split("?", 1)[0].split("#", 1)[0].lower()).suffix
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp"}.get(suffix, "image/jpeg")


def image_ext_from_url_or_mime(url: str, mime_type: str) -> str:
    ext = Path((url or "").split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    return ext if ext in IMAGE_SUFFIXES else _MIME_EXT.get(mime_type, ".jpg")


def image_error_text(error: str) -> str:
    if error == "too_large":
        return f"图片太大，已超过当前限制 {config.IMAGE_MAX_BYTES // 1024 // 1024}MB。"
    if error == "download_failed":
        return "我收到了图片，但下载失败了，可能是 QQ 临时链接已过期。"
    if error == "unsupported_mime":
        return "我收到了图片，但图片格式暂不支持。"
    return "我收到了图片，但处理图片时失败了。"


_FILE_ERRORS = {
    "no_source": "我收到了文件消息，但 OneBot 没有提供可下载地址，也无法通过文件 ID 获取。",
    "local_missing": "我收到了文件，但 OneBot 提供的本地文件路径在当前服务中不存在。",
    "source_expired": "我收到了文件，但 QQ 下载地址已失效或无权访问，请重新发送文件。",
    "network_failed": "我收到了文件，但下载时网络请求失败，请稍后重试。",
    "download_failed": "我收到了文件，但下载或读取失败了。",
}


def file_error_text(error: str) -> str:
    if error == "too_large":
        return f"文件太大，已超过当前限制 {config.FILE_MAX_BYTES // 1024 // 1024}MB。"
    return _FILE_ERRORS.get(error, "我收到了文件，但处理文件时失败了。")


def safe_attachment_name(name: str, default: str = "attachment") -> str:
    """Base name only, no control chars or path separators (no traversal)."""
    raw = Path(str(name or "").replace("\\", "/")).name.strip().strip(". ")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    return (raw or default)[:120]


def _iter_segments(message: Any) -> List[tuple[str, Dict[str, Any]]]:
    """(type, data) pairs from segments or a CQ string."""
    if message is None:
        return []
    if isinstance(message, str):
        return [(m.group(1), parse_cq_params(m.group(2) or "")) for m in CQ_RE.finditer(message)]
    try:
        return [segment_type_and_data(seg) for seg in list(message)]
    except TypeError:
        return []


def iter_image_sources(message: Any) -> List[str]:
    """Download URL / path of image, mface and marketface segments."""
    sources = []
    for seg_type, data in _iter_segments(message):
        if seg_type in _IMAGE_SEGMENT_TYPES:
            src = str(data.get("url") or data.get("path") or data.get("file") or "").strip()
            if src:
                sources.append(src)
    return sources


def iter_file_sources(message: Any) -> List[Dict[str, Any]]:
    sources = []
    for seg_type, data in _iter_segments(message):
        if seg_type != "file":
            continue
        item = normalise_file_item(data)
        if item is not None:
            item["name"] = safe_attachment_name(str(item.get("name") or "file"), "file")
            sources.append(item)
    return sources


def file_source_from_api_result(value: Any) -> str:
    """Real URL / absolute path from the various OneBot API result shapes."""
    if isinstance(value, str):
        return value if is_downloadable_file_source(value) else ""
    if not isinstance(value, dict):
        return ""
    for key in ("url", "path", "file"):
        source = str(value.get(key) or "").strip()
        if is_downloadable_file_source(source):
            return source
    return file_source_from_api_result(value.get("data"))


def attachment_abs_path(attachment: Dict[str, Any], workspace: str | Path | None = None) -> Optional[Path]:
    raw = str(attachment.get("path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path(workspace or config.CLONOTH_WORKSPACE) / raw


def _is_local_path(source: str) -> bool:
    return bool(_LOCAL_PATH_RE.match(source)) or source.startswith("/")


# ---- downloads --------------------------------------------------------------

async def resolve_onebot_file_source(bot: Any, event: Any, item: Dict[str, Any]) -> str:
    """Use the given source, else exchange the file id via group/private file URL APIs."""
    source = str(item.get("source") or "").strip()
    if is_downloadable_file_source(source):
        return source
    file_id = str(item.get("file_id") or "").strip()
    if bot is None or not file_id:
        return ""
    group_id = getattr(event, "group_id", None) if event is not None else None
    user_id = getattr(event, "user_id", None) if event is not None else None
    attempts: list[tuple[str, dict[str, Any]]] = []
    if group_id is not None:
        base = {"group_id": int(group_id), "file_id": file_id}
        busid = item.get("busid")
        # NapCat versions disagree on busid; try both shapes.
        attempts += ([("get_group_file_url", {**base, "busid": busid}), ("get_group_file_url", base)]
                     if busid is not None else
                     [("get_group_file_url", base), ("get_group_file_url", {**base, "busid": 0})])
    else:
        params: dict[str, Any] = {"file_id": file_id}
        if user_id is not None:
            params["user_id"] = int(user_id)
        if item.get("file_hash"):
            params["file_hash"] = item.get("file_hash")
        attempts += [("get_private_file_url", params), ("get_private_file_url", {"file_id": file_id})]
    for api_name, params in attempts:
        try:
            result = await bot.call_api(api_name, **params)
        except Exception:
            logger.debug("OneBot file URL API failed: api=%s", api_name, exc_info=True)
            continue
        source = file_source_from_api_result(result)
        if source:
            return source
    return ""


def _read_local(source: str, max_bytes: int) -> bytes:
    local = Path(source[7:] if source.startswith("file://") else source)
    if not local.is_file():
        raise ValueError("local_missing")
    if local.stat().st_size > max_bytes:
        raise ValueError("too_large")
    return local.read_bytes()


async def read_file_source_bytes(client: httpx.AsyncClient, source: str, max_bytes: Optional[int] = None) -> tuple[bytes, str]:
    """URL / file:// / local path, streaming with a byte limit."""
    max_bytes = config.FILE_MAX_BYTES if max_bytes is None else max_bytes
    if not is_downloadable_file_source(source):
        raise ValueError("no_source")
    if source.startswith("file://") or _is_local_path(source):
        return _read_local(source, max_bytes), "application/octet-stream"
    content = bytearray()
    async with client.stream("GET", source) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "application/octet-stream")
        async for chunk in response.aiter_bytes():
            content.extend(chunk or b"")
            if len(content) > max_bytes:
                raise ValueError("too_large")
    return bytes(content), content_type


def _attachment_dir(conversation_key: str, workspace: Path) -> Path:
    return workspace / "data" / "attachments" / conversation_key.replace(":", "_")


async def file_sources_to_attachments(
    file_sources: List[Dict[str, Any]],
    conversation_key: str,
    *,
    bot: Any = None,
    event: Any = None,
    workspace: str | Path | None = None,
) -> tuple[list[dict], list[str]]:
    result: list[dict] = []
    errors: list[str] = []
    if not config.ENABLE_FILE_INPUT or not file_sources:
        return result, errors
    root = Path(workspace or config.CLONOTH_WORKSPACE)
    att_dir = _attachment_dir(conversation_key, root)
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("cannot create QQ attachment directory: %s", exc)
        return result, [file_error_text("download_failed")]
    async with httpx.AsyncClient(timeout=config.IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for item in file_sources[: config.MAX_FILES_PER_TURN]:
            name = safe_attachment_name(str(item.get("name") or "file"), "file")
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size > config.FILE_MAX_BYTES:
                errors.append(file_error_text("too_large"))
                continue
            source = await resolve_onebot_file_source(bot, event, item)
            if not source:
                errors.append(file_error_text("no_source"))
                continue
            try:
                content, content_type = await read_file_source_bytes(client, source)
                if not content:
                    errors.append(file_error_text("download_failed"))
                    continue
                file_path = att_dir / f"{os.urandom(8).hex()}_{name}"
                file_path.write_bytes(content)
                result.append({
                    "type": "file",
                    "path": file_path.relative_to(root).as_posix(),
                    "mime_type": content_type or "application/octet-stream",
                    "name": name,
                    "source": "onebot",
                })
            except ValueError as exc:
                errors.append(file_error_text(str(exc) or "download_failed"))
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.warning("QQ file download HTTP failure: status=%s", status)
                errors.append(file_error_text("source_expired" if status in {401, 403, 404, 410} else "download_failed"))
            except httpx.RequestError as exc:
                logger.warning("QQ file download network failure: %s", type(exc).__name__)
                errors.append(file_error_text("network_failed"))
            except Exception as exc:
                logger.warning("QQ file attachment failed: %s", type(exc).__name__)
                errors.append(file_error_text("download_failed"))
    return result, errors


async def image_sources_to_attachments(
    image_sources: List[str],
    conversation_key: str,
    *,
    workspace: str | Path | None = None,
) -> tuple[list[dict], list[str]]:
    result: list[dict] = []
    errors: list[str] = []
    if not config.ENABLE_IMAGE_INPUT or not image_sources:
        return result, errors
    root = Path(workspace or config.CLONOTH_WORKSPACE)
    att_dir = _attachment_dir(conversation_key, root)
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("cannot create QQ attachment directory: %s", exc)
        return result, [image_error_text("download_failed")]
    async with httpx.AsyncClient(timeout=config.IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for raw in image_sources[: config.MAX_IMAGES_PER_TURN]:
            source = str(raw or "").strip()
            if not source:
                continue
            try:
                if source.startswith("file://") or _is_local_path(source):
                    content, content_type = _read_local(source, config.IMAGE_MAX_BYTES), ""
                else:
                    response = await client.get(source)
                    response.raise_for_status()
                    content, content_type = response.content, response.headers.get("content-type", "")
                if not content:
                    errors.append(image_error_text("download_failed"))
                    continue
                if len(content) > config.IMAGE_MAX_BYTES:
                    errors.append(image_error_text("too_large"))
                    continue
                mime_type = guess_image_mime(source, content_type, content)
                if not mime_type.startswith("image/"):
                    errors.append(image_error_text("unsupported_mime"))
                    continue
                ext = image_ext_from_url_or_mime(source, mime_type)
                file_path = att_dir / f"{os.urandom(16).hex()}{ext}"
                file_path.write_bytes(content)
                result.append({
                    "type": "image",
                    "path": file_path.relative_to(root).as_posix(),
                    "mime_type": mime_type,
                    "name": f"image{ext}",
                    "source": "onebot",
                })
            except ValueError as exc:
                errors.append(image_error_text(str(exc) if str(exc) == "too_large" else "download_failed"))
            except Exception as exc:
                logger.warning("QQ image attachment failed: %s", type(exc).__name__)
                errors.append(image_error_text("download_failed"))
    return result, errors


async def collect_message_attachments(
    bot: Any, event: Any, conversation_key: str, *, workspace: str | Path | None = None
) -> tuple[list[dict], list[str]]:
    """Images + files of the current message (quoted messages are handled elsewhere)."""
    message = event.get_message() if hasattr(event, "get_message") else None
    images, image_errors = await image_sources_to_attachments(
        iter_image_sources(message), conversation_key, workspace=workspace,
    )
    files, file_errors = await file_sources_to_attachments(
        iter_file_sources(message), conversation_key, bot=bot, event=event, workspace=workspace,
    )
    return images + files, image_errors + file_errors


# ---- indexes ------------------------------------------------------------------

@dataclass
class RecentImageEntry:
    attachment: Dict[str, Any]
    created_at: float
    sender_id: str
    message_id: str


class ReplyAttachmentCache:
    """Persisted ``message_id -> {conversation_key, sender_id, created_at, attachments}``."""

    def __init__(self, path: str | Path | None = None, *, ttl: Optional[float] = None, max_items: int = 1000) -> None:
        self.path = Path(path or config.REPLY_ATTACHMENT_CACHE_FILE)
        self.ttl = max(float(config.IMAGE_CACHE_TTL_SECONDS if ttl is None else ttl), 60.0)
        self.max_items = max(1, max_items)
        self.items: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def remember(self, message_id: Any, conversation_key: str, sender_id: str,
                 attachments: List[Dict[str, Any]], *, created_at: Optional[float] = None) -> bool:
        mid = str(message_id or "").strip()
        stored = [dict(a) for a in attachments or [] if isinstance(a, dict) and a.get("path")]
        if not mid or not stored:
            return False
        self.items[mid] = {
            "conversation_key": str(conversation_key or ""),
            "sender_id": str(sender_id or ""),
            "created_at": time.time() if created_at is None else float(created_at),
            "attachments": stored[: config.MAX_IMAGES_PER_TURN],
        }
        self.trim()
        return True

    def schedule_save(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self.save())
        except RuntimeError:
            pass

    def trim(self, now: Optional[float] = None) -> None:
        cutoff = (time.time() if now is None else now) - self.ttl
        for mid, item in list(self.items.items()):
            created = float(item.get("created_at") or 0.0)
            if created and created < cutoff:
                self.items.pop(mid, None)
        while len(self.items) > self.max_items:
            oldest = min(self.items, key=lambda k: float(self.items[k].get("created_at") or 0.0))
            self.items.pop(oldest, None)

    def get(self, message_id: Any) -> Optional[Dict[str, Any]]:
        item = self.items.get(str(message_id or "").strip())
        return item if isinstance(item, dict) else None

    def images_for(self, message_id: Any, conversation_key: str = "") -> List[Dict[str, Any]]:
        """Stored images of a message; never crosses conversations."""
        item = self.get(message_id)
        if not item:
            return []
        cached_key = str(item.get("conversation_key") or "")
        if conversation_key and cached_key and cached_key != str(conversation_key):
            return []
        return [
            dict(a) for a in (item.get("attachments") or [])[: config.MAX_IMAGES_PER_TURN]
            if isinstance(a, dict) and str(a.get("type") or "") == "image" and a.get("path")
        ]

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load OneBot reply attachment cache: %s", self.path, exc_info=True)
            return 0
        payload = data.get("reply_attachments") if isinstance(data, dict) and isinstance(data.get("reply_attachments"), dict) else data
        if not isinstance(payload, dict):
            return 0
        now = time.time()
        for mid, item in payload.items():
            if not isinstance(item, dict):
                continue
            attachments = [dict(a) for a in item.get("attachments") or [] if isinstance(a, dict)]
            try:
                created = float(item.get("created_at") or 0.0)
            except (TypeError, ValueError):
                created = 0.0
            if attachments and not (created and created < now - self.ttl):
                self.items[str(mid)] = {
                    "conversation_key": str(item.get("conversation_key") or ""),
                    "sender_id": str(item.get("sender_id") or ""),
                    "created_at": created or now,
                    "attachments": attachments,
                }
        self.trim(now)
        return len(self.items)

    async def save(self) -> None:
        async with self._lock:
            self.trim()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "version": 1,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "ttl_seconds": self.ttl,
                    "reply_attachments": dict(self.items),
                }
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self.path)
            except Exception:
                logger.warning("failed to save OneBot reply attachment cache: %s", self.path, exc_info=True)


class RecentImages:
    """Recent inbound images per conversation; also feeds the reply cache."""

    def __init__(self, reply_cache: ReplyAttachmentCache, max_items: Optional[int] = None) -> None:
        self.reply_cache = reply_cache
        size = config.RECENT_IMAGE_MAX_ITEMS if max_items is None else max_items
        self.by_conversation: DefaultDict[str, Deque[RecentImageEntry]] = defaultdict(lambda: deque(maxlen=size))

    def remember(self, conversation_key: str, event: Any, attachments: List[Dict[str, Any]]) -> None:
        sender_id = str(getattr(event, "user_id", "") or "")
        message_id = str(getattr(event, "message_id", "") or "")
        now = time.time()
        images = [dict(a) for a in attachments or [] if str(a.get("type") or "") == "image" and a.get("path")]
        for att in images:
            self.by_conversation[conversation_key].append(RecentImageEntry(att, now, sender_id, message_id))
        if images and message_id and self.reply_cache.remember(message_id, conversation_key, sender_id, images, created_at=now):
            self.reply_cache.schedule_save()

    def for_sender(self, conversation_key: str, sender_id: Any, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Latest single-message batch from this sender only (never another user's image)."""
        return select_recent_image_entries(
            self.by_conversation.get(conversation_key, ()),
            sender_id=str(sender_id or ""),
            now=time.time() if now is None else now,
            max_age_seconds=config.RECENT_IMAGE_MAX_AGE_SECONDS,
            max_images=config.MAX_IMAGES_PER_TURN,
        )

    async def merge_after_text(self, *, event: Any, conversation_key: str, user_text: str,
                               attachments: List[Dict[str, Any]], wait: Optional[float] = None) -> None:
        """Text-only "看看这张图": wait briefly, then attach the sender's latest images.

        A quoted message stays on its own chain (handled by reply context), so
        no fallback happens when the message quotes something.
        """
        reply_id = extract_reply_message_id(
            event.get_message() if hasattr(event, "get_message") else None, getattr(event, "raw_message", None),
        )
        if not should_fallback_to_recent_images(
            has_attachments=bool(attachments),
            image_input_enabled=config.ENABLE_IMAGE_INPUT,
            looks_like_image_query=looks_like_image_query(user_text),
            reply_message_id=reply_id,
        ):
            return
        delay = config.IMAGE_WAIT_AFTER_TEXT_SECONDS if wait is None else wait
        if delay > 0:
            await asyncio.sleep(delay)
        attachments.extend(self.for_sender(conversation_key, getattr(event, "user_id", "")))


async def collect_reply_images(bot: Any, event: Any, conversation_key: str, *,
                               workspace: str | Path | None = None) -> tuple[list[dict], list[str]]:
    """Images of the quoted message (used by the custom-face command)."""
    reply_id = extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None, getattr(event, "raw_message", None),
    )
    if reply_id is None:
        return [], []
    reply_obj = await get_reply_message(bot, reply_id)
    if not reply_obj:
        return [], []
    sources: list[str] = []
    for key in ("message", "raw_message"):
        if reply_obj.get(key):
            sources.extend(iter_image_sources(reply_obj[key]))
    return await image_sources_to_attachments(list(dict.fromkeys(sources)), conversation_key, workspace=workspace)


async def custom_face_command_images(*, bot: Any, event: Any, conversation_key: str,
                                     current: List[Dict[str, Any]], recent: RecentImages,
                                     workspace: str | Path | None = None) -> tuple[list[dict], list[str]]:
    """Image for "收藏表情": current message > quoted message > sender's recent image."""
    if current:
        return current, []
    replied, errors = await collect_reply_images(bot, event, conversation_key, workspace=workspace)
    if replied:
        return replied, errors
    latest = recent.for_sender(conversation_key, getattr(event, "user_id", ""))
    return (latest, []) if latest else ([], errors)
