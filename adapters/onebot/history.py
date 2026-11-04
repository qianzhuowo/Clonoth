"""Recent group chat history used as model context."""
from __future__ import annotations

from .config import STRIP_MARKDOWN_ASTERISK_STYLES
from .config import STRIP_MARKDOWN_UNDERSCORE_STYLES
from .emoji_handler import strip_output_markers
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
import datetime as dt
import time

from . import config, inbound_attachments, message_parse, profiles, routing, send, state


def label_bot_faces(text: str, message_id: Any) -> str:
    """Bot 自己发的表情：按发送时登记的名称依次标成 [表情包: 名称]，无记录时为 [表情包]。"""
    for name in state.sent_faces(message_id):
        if "[图片]" not in text:
            break
        text = text.replace("[图片]", f"[表情包: {name}]", 1)
    return text.replace("[图片]", "[表情包]")

def _format_history_line(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> str:
    """把群消息格式化为 tangqiu_main 提示词要求的历史行，并匿名化 QQ ID。"""
    text = state.ANON.anonymize_text(message_parse.compact_text(override_text or message_parse.message_to_text(event.get_message(), state.at_name, getattr(bot, "self_id", None))))
    name = state.ANON.anonymize_text(state.PROFILES.sender_display_name(event.sender, event.user_id))
    user = state.ANON.anonymize_user_id(event.user_id)
    return f"[{message_parse.format_hhmm(getattr(event, 'time', None))}] {name}({user}): {text}"


def _record_group_message(
    event: GroupMessageEvent,
    bot: Bot,
    override_text: str = "",
    attachments: List[Dict[str, Any]] | None = None,
) -> None:
    """记录群最近消息；@Bot 触发消息由 Agent matcher 手动记录，避免被 block 跳过。"""
    text = override_text or message_parse.message_to_text(event.get_message(), state.at_name, getattr(bot, "self_id", None))
    if text.strip():
        line = _format_history_line(event, bot, override_text=text)
        group_id = int(event.group_id)
        state._group_history[group_id].append(line)
        sender_id = str(getattr(event, "user_id", "") or "")
        state._group_content_records[group_id].append(state.GroupContentRecord(
            formatted_line=line,
            text=message_parse.compact_text(text, limit=max(config.HISTORY_TEXT_LIMIT, 1200)),
            sender_name=state.PROFILES.sender_display_name(getattr(event, "sender", None), sender_id),
            sender_id=sender_id,
            timestamp=float(getattr(event, "time", None) or time.time()),
            message_id=str(getattr(event, "message_id", "") or ""),
            attachments=[dict(att) for att in (attachments or []) if isinstance(att, dict)],
        ))


def _record_bot_reply(group_id: int, text: str) -> None:
    """把 Bot 最终回复写回群历史，保持后续对话连续性。"""
    if not text:
        return
    text = strip_output_markers(
        text,
        strip_asterisk_styles=STRIP_MARKDOWN_ASTERISK_STYLES,
        strip_underscore_styles=STRIP_MARKDOWN_UNDERSCORE_STYLES,
    ).replace(state._SPLIT_SIGNAL, " ")
    text = state._QQ_EMOJI_MARK_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
    text = state.ANON.anonymize_text(message_parse.compact_text(text))
    if text:
        line = f"[{dt.datetime.now(state._CST).strftime('%H:%M')}] Bot: {text}"
        state._group_history[int(group_id)].append(line)
        state._group_content_records[int(group_id)].append(state.GroupContentRecord(
            formatted_line=line,
            text=text,
            sender_name="Bot",
            sender_id="",
            timestamp=time.time(),
            message_id="",
            attachments=[],
        ))


async def _build_reply_context(event: Event, bot: Bot, conversation_key: str) -> tuple[str, List[Dict[str, Any]]]:
    """增强引用消息解析：event.reply → 本地缓存 → NapCat get_msg，并采集引用图片附件。"""
    reply_message_id = message_parse.extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply_obj: Any = getattr(event, "reply", None)
    reply: Dict[str, Any] | None = None

    if reply_obj is not None:
        reply = {
            "message": getattr(reply_obj, "message", None),
            "raw_message": getattr(reply_obj, "raw_message", None),
            "sender": getattr(reply_obj, "sender", None),
            "user_id": getattr(reply_obj, "user_id", None),
            "time": getattr(reply_obj, "time", None),
        }
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        reply = state.REPLY_MSGS.get(reply_message_id)
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        reply = await message_parse.get_reply_message(bot, reply_message_id)

    if not reply:
        if reply_message_id is not None:
            return state.ANON.anonymize_text(f"（无法获取引用消息内容：message_id={reply_message_id}）"), []
        return "", []

    message = reply.get("message")
    raw_message = reply.get("raw_message")
    text = await state.forward_expander(bot).to_text(message, getattr(bot, "self_id", None))
    if not text:
        text = await state.forward_expander(bot).to_text(raw_message, getattr(bot, "self_id", None))

    # 判断引用的消息是否为 Bot 自己发出的。QQ Bot 的回复里可能带表情包图片，
    # 用户引用这类回复时不应该把 Bot 自己的图片下载成附件再送去识图——那既浪费
    # 算力，也会让模型误以为用户在要求识别一张图片。这里仅当被引用消息不是 Bot
    # 自身发出时才采集图片附件，否则保留 [图片] 文本占位符。
    reply_sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else None
    reply_sender_qq = ""
    if reply_sender is not None:
        reply_sender_qq = str(reply_sender.get("user_id") or reply.get("user_id") or "")
    else:
        reply_sender_qq = str(reply.get("user_id") or "")
    bot_ids = routing._bot_self_id_candidates(event, bot)
    reply_from_bot = routing._qq_matches_bot(reply_sender_qq, bot_ids)

    if reply_from_bot:
        # Bot 自己的回复：默认不下载图片附件，避免把表情包送去识图。
        # 但若这条消息是 Bot 发出的真实附件（如绘图产物，已在发送时登记
        # message_id -> 本地路径），则把原图作为附件送给 AI，支持“引用该图继续改”。
        quoted_attachments = []
        bot_msg_atts = send._message_attachment_records(reply_message_id, only_images=True)
        if not bot_msg_atts:
            # Text-only Bot replies are bound to the source images of their task
            # when sent. This persistent cache lets a user's follow-up reply keep
            # the original image even after adapter restart.
            bot_msg_atts = state.REPLY_CACHE.images_for(reply_message_id, conversation_key)
        if bot_msg_atts:
            for att in bot_msg_atts:
                rel_path = send._to_workspace_rel_path(att["path"])
                quoted_attachments.append({
                    "type": "image",
                    "path": rel_path,
                    "name": att.get("name", "") or Path(att["path"]).name,
                    "source": "onebot_bot_reply",
                })
                text = text.replace("[图片]", f"[图片: {rel_path}]", 1)
            # 剩余未匹配的图片占位符（如同消息里的表情包）仍标为表情包。
            text = label_bot_faces(text, reply_message_id)
        else:
            text = label_bot_faces(text, reply_message_id)
    else:
        images = inbound_attachments.iter_image_sources(message) or inbound_attachments.iter_image_sources(raw_message)
        quoted_attachments, _quoted_errors = await inbound_attachments.image_sources_to_attachments(images, conversation_key)
        for att in quoted_attachments:
            text = text.replace("[图片]", f"[图片: {att['path']}]", 1)

    text = state.ANON.anonymize_text(message_parse.compact_text(text))
    if not text:
        if reply_message_id is not None:
            return state.ANON.anonymize_text(f"（引用消息为空或暂不支持的消息类型：message_id={reply_message_id}）"), quoted_attachments
        return "", quoted_attachments

    sender = reply.get("sender")
    sender_id = ""
    if isinstance(sender, dict):
        sender_id = str(sender.get("user_id") or reply.get("user_id") or "")
        name_raw = profiles.sanitize_name(str(sender.get("card") or sender.get("nickname") or sender_id or "原作者"))
    elif sender is not None:
        sender_id = str(getattr(sender, "user_id", "") or "")
        name_raw = state.PROFILES.sender_display_name(sender, sender_id)
    else:
        sender_id = str(reply.get("user_id") or "")
        name_raw = profiles.sanitize_name(sender_id or "原作者")
    name = state.ANON.anonymize_text(name_raw)
    user = state.ANON.anonymize_user_id(sender_id) if sender_id else "UserUnknown"
    return f"[{message_parse.format_hhmm(reply.get('time'))}] {name}({user}): {text}", quoted_attachments
