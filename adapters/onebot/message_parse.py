"""OneBot message -> model-readable text.

One converter handles NoneBot segments, OneBot dict segments and CQ strings.
Mentions are rendered through an ``at_name`` callable (profile name or
anonymous alias) so raw QQ ids never reach the model. Bot API access is
duck-typed (``bot.call_api``); nothing here imports nonebot.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import config
from .profiles import sanitize_name

logger = logging.getLogger(__name__)

CST = dt.timezone(dt.timedelta(hours=8))
CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")
FORWARD_SENDER_PLACEHOLDER = "转发用户"

AtName = Callable[[str], str]
ForwardFetcher = Callable[[str], Awaitable[Optional[List[Dict[str, Any]]]]]


def segment_type_and_data(segment: Any) -> tuple[str, Dict[str, Any]]:
    """Works for NoneBot MessageSegment objects and OneBot dict segments."""
    if isinstance(segment, dict):
        seg_type = str(segment.get("type") or "")
        raw = segment.get("data")
    else:
        seg_type = str(getattr(segment, "type", "") or "")
        raw = getattr(segment, "data", {}) or {}
    return seg_type, raw if isinstance(raw, dict) else {}


def parse_cq_params(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in (raw or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            out[key] = value
    return out


def format_hhmm(timestamp: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(timestamp or time.time()), CST).strftime("%H:%M")
    except Exception:
        return dt.datetime.now(CST).strftime("%H:%M")


def compact_text(text: str, limit: Optional[int] = None) -> str:
    """Single line, at most ``limit`` chars (history lines)."""
    limit = config.HISTORY_TEXT_LIMIT if limit is None else limit
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


def _file_label(data: Dict[str, Any]) -> str:
    name = str(data.get("name") or data.get("file_name") or data.get("file") or "").strip()
    return f"[文件:{sanitize_name(Path(name).name if name else '附件', max_len=80)}]"


def _at_text(qq: Any, at_name: AtName) -> str:
    qq = str(qq or "")
    return "@全体成员" if qq.lower() == "all" else f"@{at_name(qq)}"


def _segments(message: Any) -> List[Any]:
    if message is None or isinstance(message, str):
        return []
    try:
        return list(message)
    except TypeError:
        return []


def message_to_text(message: Any, at_name: AtName, bot_self_id: Any = None) -> str:
    """Text with placeholders for non-text content ([图片], [文件:x], ...)."""
    if message is None:
        return ""
    if isinstance(message, str):
        return format_cq_message(message, at_name).strip()
    parts: List[str] = []
    for segment in _segments(message):
        seg_type, data = segment_type_and_data(segment)
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            if bot_self_id is not None and qq == str(bot_self_id):
                continue
            parts.append(_at_text(qq, at_name))
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append(f"[QQ表情:{data.get('id')}]" if data.get("id") else "[QQ表情]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "file":
            parts.append(_file_label(data))
        elif seg_type == "forward":
            parts.append("[合并转发]")
        elif seg_type == "reply":
            continue
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def format_cq_message(text: str, at_name: AtName) -> str:
    def repl(match: re.Match[str]) -> str:
        cq_type = match.group(1)
        params = parse_cq_params(match.group(2) or "")
        if cq_type == "at":
            return _at_text(params.get("qq", ""), at_name)
        if cq_type == "reply":
            return f"[回复:{params.get('id', '')}]"
        if cq_type == "image":
            return "[图片]"
        if cq_type == "face":
            return f"[QQ表情:{params.get('id', '')}]"
        if cq_type == "record":
            return "[语音]"
        if cq_type == "file":
            return _file_label(params)
        if cq_type == "forward":
            return "[合并转发]"
        return match.group(0)

    return CQ_RE.sub(repl, text)


def _pick(data: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return value
    return None


_FORWARD_ID_KEYS = ("id", "res_id", "resId", "forward_id", "forwardId")
_REPLY_ID_KEYS = ("id", "message_id", "messageId", "messageid", "message_seq", "seq")


def extract_forward_ids(message: Any) -> List[str]:
    """Merged-forward res_ids from segments or CQ text."""
    ids: List[str] = []
    for segment in _segments(message):
        seg_type, data = segment_type_and_data(segment)
        value = _pick(data, _FORWARD_ID_KEYS) if seg_type == "forward" else None
        if value is not None:
            ids.append(str(value).strip())
    if isinstance(message, str):
        for match in CQ_RE.finditer(message):
            value = _pick(parse_cq_params(match.group(2) or ""), _FORWARD_ID_KEYS) if match.group(1) == "forward" else None
            if value is not None:
                ids.append(str(value).strip())
    return ids


def extract_reply_message_id(message: Any, raw_message: Any = None) -> Any:
    """Quoted message id from a reply segment or ``[CQ:reply,id=...]``."""
    for segment in _segments(message):
        seg_type, data = segment_type_and_data(segment)
        value = _pick(data, _REPLY_ID_KEYS) if seg_type == "reply" else None
        if value is not None:
            return value
    for candidate in (message, raw_message):
        if not isinstance(candidate, str):
            continue
        for match in CQ_RE.finditer(candidate):
            if match.group(1) == "reply":
                rid = parse_cq_params(match.group(2) or "").get("id")
                if rid is not None and str(rid).strip():
                    return rid
    return None


def format_forward_sender(sender: Any, display_name: Callable[[str], str]) -> str:
    """Forward senders are usually strangers: never register them in the alias map.

    Profile name > card > nickname > a fixed placeholder; never a raw QQ id.
    """
    if isinstance(sender, dict):
        user_id = str(sender.get("user_id") or "").strip()
        raw_name = str(sender.get("card") or sender.get("nickname") or "").strip()
    else:
        user_id = str(getattr(sender, "user_id", "") or "").strip()
        raw_name = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()
    display = display_name(user_id) or raw_name
    return sanitize_name(display) if display else FORWARD_SENDER_PLACEHOLDER


def truncate_forward_text(text: str, limit: Optional[int] = None) -> str:
    limit = config.FORWARD_MSG_TEXT_LIMIT if limit is None else limit
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（合并转发内容过长，已截断）"


class ForwardExpander:
    """Recursively expand merged forwards with depth, loop and count limits."""

    def __init__(
        self,
        fetch: ForwardFetcher,
        at_name: AtName,
        display_name: Callable[[str], str],
        *,
        enabled: Optional[bool] = None,
        max_depth: Optional[int] = None,
        max_messages: Optional[int] = None,
    ) -> None:
        self.fetch = fetch
        self.at_name = at_name
        self.display_name = display_name
        self.enabled = config.ENABLE_FORWARD_MSG_INPUT if enabled is None else enabled
        self.max_depth = config.FORWARD_MSG_MAX_DEPTH if max_depth is None else max_depth
        self.max_messages = config.FORWARD_MSG_MAX_MESSAGES if max_messages is None else max_messages

    async def to_text(
        self,
        message: Any,
        bot_self_id: Any = None,
        *,
        depth: Optional[int] = None,
        visited: Optional[set[str]] = None,
        remaining: Optional[List[int]] = None,
    ) -> str:
        text = message_to_text(message, self.at_name, bot_self_id)
        forward_ids = extract_forward_ids(message) if self.enabled else []
        if not forward_ids:
            return text
        depth = self.max_depth if depth is None else depth
        visited = set() if visited is None else visited
        remaining = [self.max_messages] if remaining is None else remaining
        expanded = [
            await self._format_one(fid, bot_self_id, depth=depth, visited=visited, remaining=remaining)
            for fid in forward_ids
        ]
        return "\n".join([text, *expanded] if text else expanded).strip()

    async def _format_one(
        self, forward_id: str, bot_self_id: Any, *, depth: int, visited: set[str], remaining: List[int]
    ) -> str:
        if depth <= 0:
            return f"[合并转发:id={forward_id}, 已达到读取深度上限]"
        if forward_id in visited:
            return f"[合并转发:id={forward_id}, 已跳过循环引用]"
        if remaining[0] <= 0:
            return "[合并转发:已达到读取条数上限]"
        visited.add(forward_id)
        try:
            messages = await self.fetch(forward_id)
            if messages is None:
                return f"[合并转发:id={forward_id}, 读取失败]"
            lines = [f"【合并转发 id={forward_id}】"]
            for index, item in enumerate(messages, start=1):
                if remaining[0] <= 0:
                    lines.append("…（合并转发条数过多，已截断）")
                    break
                remaining[0] -= 1
                sender = format_forward_sender(item.get("sender"), self.display_name)
                content = item.get("content") if item.get("content") is not None else item.get("message")
                text = await self.to_text(content, bot_self_id, depth=depth - 1, visited=visited, remaining=remaining)
                text = compact_text(text or "[暂不支持的消息类型]", limit=max(config.HISTORY_TEXT_LIMIT, 1200))
                lines.append(f"{index}. [{format_hhmm(item.get('time'))}] {sender}: {text}")
            lines.append("【合并转发结束】")
            return truncate_forward_text("\n".join(lines))
        finally:
            visited.discard(forward_id)


async def get_forward_messages(bot: Any, forward_id: str) -> Optional[List[Dict[str, Any]]]:
    """NapCat ``get_forward_msg``; accepts list or {data:{messages}} shapes."""
    if not forward_id:
        return None
    try:
        data = await bot.call_api("get_forward_msg", id=forward_id)
    except Exception:
        logger.warning("get_forward_msg failed for forward_id=%s", forward_id, exc_info=True)
        return None
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else None


async def get_reply_message(bot: Any, reply_message_id: Any) -> Optional[Dict[str, Any]]:
    """NapCat ``get_msg`` for a quoted message."""
    if reply_message_id is None:
        return None
    raw_id = str(reply_message_id).strip()
    try:
        data = await bot.call_api("get_msg", message_id=int(raw_id) if raw_id.isdigit() else reply_message_id)
    except Exception:
        logger.warning("get_msg failed for reply_message_id=%s", reply_message_id, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


class ReplyMessageCache:
    """Recent inbound messages, for quotes NapCat cannot resolve via get_msg."""

    def __init__(self, max_items: int = 1000) -> None:
        self.max_items = max(1, max_items)
        self.items: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def remember(self, event: Any) -> None:
        message_id = getattr(event, "message_id", None)
        if message_id is None or not str(message_id).strip():
            return
        key = str(message_id)
        self.items[key] = {
            "message": event.get_message() if hasattr(event, "get_message") else None,
            "raw_message": getattr(event, "raw_message", None),
            "sender": getattr(event, "sender", None),
            "user_id": getattr(event, "user_id", None),
            "time": getattr(event, "time", None),
        }
        self.items.move_to_end(key)
        while len(self.items) > self.max_items:
            self.items.popitem(last=False)

    def get(self, message_id: Any) -> Optional[Dict[str, Any]]:
        return self.items.get(str(message_id)) if message_id is not None else None
