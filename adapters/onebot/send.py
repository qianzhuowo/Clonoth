"""Outbound delivery to QQ: text splitting, emoji/@ segments, attachments, reactions."""
from __future__ import annotations

import logging
from .config import CLONOTH_WORKSPACE
from .config import ENABLE_IMAGE_FORWARD_MERGE
from .config import ENABLE_REACTIONS
from .config import IMAGE_DOWNLOAD_TIMEOUT
from .config import IMAGE_FORWARD_MERGE_NICKNAME
from .config import IMAGE_FORWARD_MERGE_THRESHOLD
from .config import IMAGE_MAX_BYTES
from .config import ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS
from .config import STRIP_MARKDOWN_ASTERISK_STYLES
from .config import STRIP_MARKDOWN_UNDERSCORE_STYLES
from .emoji_handler import process_emojis
from .send_contract import IdempotencyClaim
from .send_contract import IdempotencyOwnershipError
from .send_contract import OneBotAttachmentBatchError
from .send_contract import OneBotSendContractError
from .send_contract import OutboundSendContext
from .send_contract import classify_send_exception
from .send_contract import image_content_identity
from .send_contract import make_idempotency_key
from .send_contract import protected_claim_send
from .send_contract import validate_send_request
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import asyncio
import base64
import contextlib
import hashlib
import httpx
import re
import time
import uuid

from . import anon, approval, commands, history, inbound_attachments, profiles, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

def _target_from_platform_data(platform_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 trigger 平台数据中提取 QQ 回复目标。"""
    # 2026-05-01 修改原因：主回调会携带 matcher 保存的平台数据；优先使用
    # 显式 type 字段区分私聊和群聊，避免只有 user_id 时误判群聊成员为私聊。
    # 2026-05-03 新增：提取触发消息 ID 和发送者 ID，发第一条回复时自动引用 + @
    event = platform_data.get("event")
    msg_id = getattr(event, "message_id", None) if event else None
    sender_id = getattr(event, "user_id", None) if event else None
    target_type = platform_data.get("type")
    conversation_key = str(platform_data.get("conversation_key") or "")
    if target_type == "private" and platform_data.get("user_id") is not None:
        t: Dict[str, Any] = {"type": "private", "user_id": int(platform_data["user_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        return t
    if target_type == "group" and platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    if platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    return None


def _get_fallback_bot() -> Optional[Bot]:
    """获取 fallback 发送用 Bot；EventRouter 可能在 matcher 外触发回调。"""
    if state._last_bot is not None:
        return state._last_bot
    try:
        return get_bot()
    except Exception:
        return None


async def _inline_emoji_segments(
    segments: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], bool]:
    """把收藏表情的远程图片预先转成 base64，绕过 NapCat 临时目录。"""
    candidates = [
        segment for segment in segments
        if segment.get("type") == "image"
        and segment.get("emoji")
        and str(segment.get("url") or "").strip()
        and not str(segment.get("url") or "").startswith("base64://")
    ]
    if not candidates:
        return segments, False

    changed = False
    async with httpx.AsyncClient(
        timeout=IMAGE_DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    ) as client:
        for segment in candidates:
            source = str(segment.get("url") or "").strip()
            try:
                if source.startswith("file://"):
                    raw = Path(source[7:]).read_bytes()
                elif re.match(r"^https?://", source, re.IGNORECASE):
                    response = await client.get(source)
                    response.raise_for_status()
                    raw = response.content
                else:
                    continue
                if not raw:
                    raise ValueError("empty image response")
                if len(raw) > IMAGE_MAX_BYTES:
                    raise ValueError(f"image exceeds {IMAGE_MAX_BYTES} bytes")
                segment["url"] = "base64://" + base64.b64encode(raw).decode("ascii")
                changed = True
            except Exception as exc:
                logger.warning("inline QQ emoji image failed: source=%s error=%s", source, exc)
    return segments, changed


def _message_from_processed_segments(segments: List[Dict[str, Any]]) -> Message:
    """把 emoji_handler 的轻量段描述转换为 OneBot Message。"""
    message_segments: List[MessageSegment] = []
    for segment in segments:
        if segment.get("type") == "text":
            content = str(segment.get("content", ""))
            if content:
                message_segments.append(MessageSegment.text(content))
        elif segment.get("type") == "image":
            url = segment.get("url")
            if url:
                if segment.get("emoji"):
                    # QQ 收藏表情：在普通 image 段基础上补 sub_type=1（表情子类型），
                    # 让 QQ 客户端按小图/贴纸渲染而不是普通大图；summary 提供
                    # "[表情]" 占位，兼容不支持 sub_type 的客户端摘要显示。
                    emoji_seg = MessageSegment.image(url)
                    # NapCat 在 cache/proxy 默认开启时会把 QQ 表情 URL 落到
                    # 容器内的临时目录；当前部署的 Bot 与 NapCat 不共享该目录，
                    # 会导致 retcode=1200/ENOENT。关闭这两个选项，让 NapCat
                    # 直接处理表情 URL，避免依赖不存在的容器临时文件。
                    emoji_seg.data["cache"] = "false"
                    emoji_seg.data["proxy"] = "false"
                    emoji_seg.data["sub_type"] = 1
                    emoji_seg.data.setdefault("summary", "[表情]")
                    message_segments.append(emoji_seg)
                else:
                    message_segments.append(MessageSegment.image(url))
        elif segment.get("type") == "at":
            qq_id = segment.get("qq")
            if qq_id:
                message_segments.append(MessageSegment.at(qq_id))
    return Message(message_segments)


def _mark_qq_reply_finished(conversation_key: str) -> None:
    for key in {conversation_key, state.ANON.real_conversation_key(conversation_key)}:
        event = state._qq_waiting_replies.get(key)
        if event is not None:
            event.set()


def _message_dedup_text(message: Any) -> str:
    """生成用于 QQ 发送幂等判断的稳定文本。"""
    try:
        if isinstance(message, Message):
            return str(message)
    except Exception:
        pass
    return str(message or "")


async def _load_group_members(
    bot: Bot, group_id: int
) -> tuple[set[str], Dict[str, str], Dict[str, tuple[str, str]]] | None:
    """拉取并缓存群成员。

    返回 ({user_id 集合}, {名片/昵称小写 -> user_id}, {user_id -> (card, nickname)})。
    取不到时返回 None（表示无法校验/反查，由调用方决定不做拦截）。
    """
    try:
        gid = int(group_id)
    except Exception:
        return None
    now = time.time()
    cached = state._group_member_cache.get(gid)
    if cached and cached[0] > now:
        return cached[1], cached[2], cached[3]
    try:
        data = await bot.call_api("get_group_member_list", group_id=gid)
    except Exception:
        logger.debug("get_group_member_list failed for group=%s", gid, exc_info=True)
        return None
    members: set[str] = set()
    name_map: Dict[str, str] = {}
    card_map: Dict[str, tuple[str, str]] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or item.get("user_id") is None:
                continue
            uid = str(item.get("user_id"))
            members.add(uid)
            card = str(item.get("card") or "").strip()
            nickname = str(item.get("nickname") or "").strip()
            card_map[uid] = (card, nickname)
            # 名片(card)优先于昵称(nickname)；同名时不覆盖已有映射（保留先遇到的）。
            for key in (card, nickname):
                name = str(key or "").strip()
                if name:
                    name_map.setdefault(name.casefold(), uid)
    if not members:
        return None
    state._group_member_cache[gid] = (now + state._GROUP_MEMBER_CACHE_TTL, members, name_map, card_map)
    return members, name_map, card_map


async def _group_member_ids(bot: Bot, group_id: int) -> set[str] | None:
    """获取群成员 QQ 号集合（带 TTL 缓存）。取不到时返回 None。"""
    loaded = await _load_group_members(bot, group_id)
    return loaded[0] if loaded else None


def _group_member_display_name(group_id: Any, user_id: Any) -> str:
    """返回 @ 某群友时应展示的名字：本群群名片(card) > 本群 QQ 昵称(nickname)。

    仅使用已缓存的群成员数据（不发网络请求）；缓存未就绪或未命中时返回空串，
    由调用方回退到 profile 显示名 / 匿名别名。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = state._group_member_cache.get(gid)
    if not cached:
        return ""
    card, nickname = cached[3].get(uid, ("", ""))
    name = card or nickname
    return profiles.sanitize_name(name) if name else ""


def _resolve_at_alias_in_group(group_id: Any, token: str) -> str:
    """在已缓存的群成员名片/昵称里反查 token 对应的真实 QQ 号（小写匹配）。

    仅使用缓存（不发网络请求）；缓存未就绪或未命中时返回空串。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = state._group_member_cache.get(gid)
    if not cached:
        return ""
    return cached[2].get(raw.casefold(), "")


async def _sanitize_group_at_segments(bot: Bot, group_id: Any, message: Any) -> Any:
    """把群消息里"非本群成员/无法解析"的 at 段降级为文本，避免整条消息发送失败。

    2026-07-14 修复：一条消息 @ 多人时，只要其中一个 at 的 uid 被 NapCat 判为
    Get Uid Error，send_group_msg 就会整条失败。这里在发送前用群成员列表校验，
    命中不了的 at 用 @昵称/@别名 文本代替，@全体成员(all) 放行。
    """
    if not isinstance(message, Message):
        return message
    loaded = await _load_group_members(bot, group_id)
    if loaded is None:
        # 拿不到成员列表时不拦截，保持原样（避免误伤把所有 at 变文本）。
        return message
    members, name_map, _card_map = loaded
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
            # 特例：[at:all] 但群里真有成员名片/昵称叫 "all"，优先当成 @ 那个人，
            # 避免把他误伤为 @全体成员。
            if qq.lower() == "all":
                mapped = name_map.get("all")
                if mapped:
                    logger.info("resolve [at:all] to group member %s in group=%s", mapped, group_id)
                    new_segments.append(MessageSegment.at(mapped))
                    continue
                # 确实是 @全体成员，放行。
                new_segments.append(seg)
                continue
            if not qq:
                new_segments.append(seg)
                continue
            # 以 members（本群真实 QQ 号集合）为权威，按理想顺序判定：
            #   1) qq 是数字且 ∈ members  -> 就是这个真实 QQ 号，放行。
            #   2) 否则先按 name_map（群名片/昵称，card 优先）反查：命中则改用真实 QQ 号。
            #     这能修复“群名片恰好是纯数字（如名片叫 1）”被误当成 QQ 号的问题。
            #   3) 都不中 -> 丢弃这个非法 at（不再降级成 @匿名代号发到群里）。
            if qq in members:
                new_segments.append(seg)
                continue
            mapped = _resolve_at_alias_in_group(group_id, qq)
            if mapped and mapped in members:
                logger.info("resolve at token %r to group member %s in group=%s", qq, mapped, group_id)
                new_segments.append(MessageSegment.at(mapped))
                continue
            # 既不是本群成员 QQ 号，也没有群友名片/昵称叫这个名字：判为无效 at，直接丢弃。
            # Why: 这种情况多半是模型凭空捧造的 at（如 qq=1），降级成 @UserXX 反而产生脗肿文本。
            logger.warning("drop invalid at: token=%r not a member/card in group=%s", qq, group_id)
            continue
        new_segments.append(seg)
    return Message(new_segments)


def _dedupe_group_at_segments(message: Any) -> Any:
    """同一条群消息每个真实 QQ 号只提及一次（在别名解析之后执行）。"""
    if not isinstance(message, Message):
        return message
    seen: set[str] = set()
    segments: List[MessageSegment] = []
    for segment in message:
        if getattr(segment, "type", "") == "at":
            qq = str((getattr(segment, "data", {}) or {}).get("qq", "")).strip()
            if qq:
                if qq in seen:
                    continue
                seen.add(qq)
        segments.append(segment)
    return Message(segments)


def _strip_at_to_text(message: Any, group_id: Any = None) -> Any:
    """把消息里所有 at 段降级为 @文本，用于发送失败后的兜底重发。

    传入 group_id 时，@文本优先取本群群名片/昵称，其次 profile 显示名。
    拿不到可读名字时直接丢弃这个 at（不再用 _anonymize_user_id 把匿名代号发到群里）。
    """
    if not isinstance(message, Message):
        return message
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            data = getattr(seg, "data", {}) or {}
            qq = str(data.get("qq", "")).strip()
            if qq.lower() == "all":
                new_segments.append(MessageSegment.text("@全体成员"))
            elif qq:
                name = _group_member_display_name(group_id, qq) or state.PROFILES.display_name(qq)
                if name:
                    new_segments.append(MessageSegment.text(f"@{name}"))
                # 无可读名字：丢弃该 at，避免 @匿名代号泄露到群里。
            continue
        new_segments.append(seg)
    return Message(new_segments)


def _extract_sent_message_id(result: Any) -> str:
    """从 send_*_msg 返回值里提取 message_id（兼容 dict / 标量 两种形式）。"""
    if isinstance(result, dict):
        mid = result.get("message_id")
        if mid is None:
            mid = result.get("data", {}).get("message_id") if isinstance(result.get("data"), dict) else None
        return str(mid).strip() if mid is not None else ""
    if isinstance(result, (int, str)):
        return str(result).strip()
    return ""


async def _send_qq_message_once(bot: Bot, target: Dict[str, Any], message: Any, *, dedupe: bool = True) -> str:
    """执行一次 OneBot 发送流程（含既有的协议级自动补发）。"""
    # 2026-05-01 修改原因：私聊回复必须调用 send_private_msg，群聊回复继续调用
    # send_group_msg。通过 target 分发，回调发送文本和附件时不再硬编码群聊接口。
    if target.get("type") == "private":
        user_id = target.get("user_id")
        if user_id is None:
            raise ValueError("private target missing user_id")
        try:
            result = await bot.send_private_msg(user_id=int(user_id), message=message)
            return _extract_sent_message_id(result)
        except ActionFailed as exc:
            # [2026-08-06] 私聊补 ENOENT 兜底，与群聊分支对齐。
            # 原因：收藏表情/图片走 image(url) 时 NapCat 先落地到容器 temp/ 再上传，
            # temp 文件被并发清理时报 retcode=1200 ENOENT（真失败、未发出）。原私聊
            # 分支无补发保护，异常直接冒泡到 send_reply，导致整条回复被吞。
            #   - sendMsg 超时 / EventChecker 失败：大概率已送达，raise 不重发（避免双发）。
            #   - ENOENT：真失败，原样重发一次。
            #   - 其它失败：raise 交由上层记录。
            if _is_napcat_ambiguous_ack(exc):
                raise
            if _is_napcat_enoent(exc):
                _alert_napcat_temp_unwritable(exc, "send_private_msg")
                logger.warning(
                    "send_private_msg failed (ENOENT %s); retry once", exc,
                )
                result = await bot.send_private_msg(user_id=int(user_id), message=message)
                return _extract_sent_message_id(result)
            raise
    if target.get("type") == "group":
        group_id = target.get("group_id")
        if group_id is None:
            raise ValueError("group target missing group_id")
        # 发送前先把非本群/无法解析的 at 降级为文本，避免 Get Uid Error 整条失败。
        safe_message = await _sanitize_group_at_segments(bot, group_id, message)
        safe_message = _dedupe_group_at_segments(safe_message)
        try:
            result = await bot.send_group_msg(group_id=int(group_id), message=safe_message)
            return _extract_sent_message_id(result)
        except ActionFailed as exc:
            # [2026-09-17] 修复“连续 @ 同一人两次”：
            #   原实现只要消息里含 at，任何 ActionFailed 都先走“at 转文本重发”，
            #   超时判定放在后面永远轮不到。NapCat sendMsg 超时 / EventChecker 失败时
            #   消息其实已提交给 NTQQ，结果就是群里出现两条 @。
            #   现在的顺序：
            #   1) ack 不明确（超时/EventChecker）：绝不重发，直接 raise 交由上层归类。
            #   2) 仅当错误文本确实指向 at/uid 解析失败，才把 at 降级为文本重发一次。
            #   3) ENOENT（容器 temp 文件缺失，真失败）：原样重发一次。
            #   4) 其它：raise。
            if _is_napcat_ambiguous_ack(exc):
                raise
            if _is_napcat_at_uid_error(exc):
                # 兜底：若仍因 at/uid 问题失败（如成员列表不准确），把所有 at 降级为文本重发一次。
                fallback = _strip_at_to_text(safe_message, group_id)
                if str(fallback) != str(safe_message):
                    logger.warning("send_group_msg failed (%s); retry with at-as-text", exc)
                    result = await bot.send_group_msg(group_id=int(group_id), message=fallback)
                    return _extract_sent_message_id(result)
            # [2026-07-22] 失败与 at 无关时的补发：
            #   - ENOENT（NapCat 容器 temp 临时文件缺失，retcode=1200）：真失败、消息未发出，
            #     原样重发一次（dedupe=False）给一次补发机会，避免“漏回复”。
            #   图片类消息的 base64 重发已在 _send_attachment_path 处理，这里主要兼顾
            #   文本/引用回复等非图片消息因 ENOENT 整条失败的场景。
            if _is_napcat_enoent(exc):
                _alert_napcat_temp_unwritable(exc, "send_group_msg")
                logger.warning(
                    "send_group_msg failed (ENOENT %s); retry once (dedupe off)", exc,
                )
                result = await bot.send_group_msg(group_id=int(group_id), message=safe_message)
                return _extract_sent_message_id(result)
            raise
    raise OneBotSendContractError(f"unknown QQ target type: {target!r}")


def _send_audit_fields(
    context: OutboundSendContext,
    *,
    platform_message_id: str,
    attempt: int,
    idempotency_key: str,
) -> Dict[str, Any]:
    return {
        "event_seq": context.event_seq,
        "event_id": context.event_id,
        "task_id": context.task_id,
        "source_inbound_seq": context.source_inbound_seq,
        "conversation_key": context.conversation_key,
        "platform_message_id": platform_message_id,
        "attempt": attempt,
        "idempotency_key": idempotency_key,
    }


def _sent_ttl_for_context(context: OutboundSendContext) -> float | None:
    """Use short retention only for the last-resort context-free content key."""
    if context.idempotency_key or context.event_id:
        return None
    return ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS


async def _heartbeat_idempotency_claim(claim: IdempotencyClaim) -> None:
    """Renew one logical-send owner until its platform operation finishes."""
    interval = max(0.05, min(30.0, state._outbound_idempotency.lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        try:
            await state._outbound_idempotency.heartbeat(claim)
        except IdempotencyOwnershipError:
            # The protected send commits pending -> sent/ambiguous atomically. A
            # heartbeat already queued at that boundary will then observe that its
            # pending ownership is gone; this is the normal terminal condition.
            # A real takeover is still detected by the protected operation's strict
            # owner-checked commit/release path.
            return


async def _send_qq_message(
    bot: Bot,
    target: Dict[str, Any],
    message: Any,
    *,
    dedupe: bool = True,
    send_context: OutboundSendContext | None = None,
    content_identity: str = "",
    idempotency_key: str = "",
    attempt: int = 1,
) -> str:
    """Send with an explicit contract and pending/sent two-phase idempotency."""
    validate_send_request(bot, target)
    context = send_context or OutboundSendContext(
        conversation_key=str(target.get("conversation_key") or "")
    )
    identity = content_identity or _message_dedup_text(message).strip()
    key = idempotency_key or make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._outbound_idempotency.wait_for_resolution(key)
        if resolved is None:
            claim = await state._outbound_idempotency.begin(key)
        else:
            claim = IdempotencyClaim(key=key, acquired=False, state=resolved)
    fields = _send_audit_fields(
        context,
        platform_message_id="",
        attempt=attempt,
        idempotency_key=key,
    )
    if not claim.acquired:
        logger.info("onebot_send_duplicate", extra={**fields, "claim_state": claim.state})
        return f"idempotent:{key}"
    logger.info("onebot_send_started", extra=fields)
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    try:
        async def send_once() -> str:
            return await _send_qq_message_once(bot, target, message, dedupe=dedupe)

        platform_message_id = await protected_claim_send(
            state._outbound_idempotency,
            claim,
            send_once,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=lambda result: str(result or ""),
        )
        logger.info(
            "onebot_send_sent",
            extra=_send_audit_fields(
                context, platform_message_id=platform_message_id,
                attempt=attempt, idempotency_key=key,
            ),
        )
        return platform_message_id
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _send_emoji_base64_fallback(
    bot: Bot,
    target: Dict[str, Any],
    group: List[Dict[str, Any]],
    exc: BaseException,
    *,
    prefix: Any = None,
    send_context: OutboundSendContext | None = None,
    segment_identity: str = "",
) -> Optional[str]:
    """表情 URL 直发失败后，在 Bot 侧下载转 base64:// 重发一次。

    返回发送成功的 message_id；不适合或重发失败时返回 None（由调用方决定冒泡还是忽略）。
    - ack 不明确（可能已送达）：不重发，避免双发。
    - NapCat temp 目录 ENOENT：base64 同样要落到 temp，重发无意义，只告警。
    """
    if getattr(exc, "ambiguous_ack", False):
        logger.warning("onebot_emoji_send_ambiguous (no base64 retry) identity=%s", segment_identity[:200])
        return None
    if _alert_napcat_temp_unwritable(exc, "emoji_send"):
        return None
    fallback_group = [dict(segment) for segment in group]
    fallback_group, changed = await _inline_emoji_segments(fallback_group)
    if not changed:
        return None
    message = _message_from_processed_segments(fallback_group)
    if prefix is not None:
        message = prefix + message
    identity = f"{segment_identity}:b64"
    logger.warning("onebot_emoji_url_send_failed, retry base64 identity=%s error=%s", segment_identity[:200], exc)
    try:
        return await _send_qq_message(
            bot,
            target,
            message,
            send_context=send_context.child(identity) if send_context else None,
            content_identity=identity,
        )
    except Exception as retry_exc:
        _alert_napcat_temp_unwritable(retry_exc, "emoji_send_base64")
        logger.warning("onebot_emoji_base64_send_failed identity=%s error=%s", segment_identity[:200], retry_exc)
        return None


async def _send_split_text(
    bot: Bot,
    target: Dict[str, Any],
    text: str,
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> bool:
    """按 [SPLIT] 拆分文本，并把每段回复绑定到本轮来源图片。

    [2026-09-17] 表情/图片与正文拆开发送：
      Why: 正文与 [表情:xx] 原本合并在同一条消息里，NapCat 下载表情到容器 temp/
      失败（ENOENT）时整条消息失败，文字答案跟着一起丢，再经 SDK 多轮重试/超时后
      用户什么都收不到。
      How: 每一段按原始顺序把连续“文本 + at”合并发送，并把表情/图片逐个单独
      发送；表情发送失败只记日志，不影响前后正文继续发送。
      表情段本身仍由 _message_from_processed_segments 生成（QQ 收藏表情专用
      image 段：URL + sub_type=1 + summary），不改发送格式。
      Purpose: 图片层面的不稳定不再阻塞文字回复，同时保留“文本 → 表情 → 文本”
      的原始语义顺序。
    """
    # 2026-05-01 修改原因：文本拆分逻辑对群聊和私聊相同，实际发送交给
    # _send_qq_message 处理，确保私聊也能复用表情替换和分段发送能力。
    sent_any = False
    parts = text.split(state._SPLIT_SIGNAL) if text else []
    for index, raw_part in enumerate(parts):
        part = approval.truncate_qq_text(raw_part.strip())
        if not part:
            continue
        segments = await process_emojis(
            part,
            bot,
            state.FACES.bqbs,
            state.FACES.current_names(),
            state.FACES.current_metadata(),
            strip_asterisk_styles=STRIP_MARKDOWN_ASTERISK_STYLES,
            strip_underscore_styles=STRIP_MARKDOWN_UNDERSCORE_STYLES,
        )
        if not segments:
            continue
        # 表情先把实时 URL 直接交给 NapCat（由 NapCat 自己下载），失败后才在 Bot 侧
        # 下载转 base64:// 重发；不再每次预下载，减少 Bot 侧下载超时。
        ordered_groups = _ordered_text_and_image_segments(segments)
        outgoing = [
            (kind, _message_from_processed_segments(group))
            for kind, group in ordered_groups
        ]
        if not outgoing:
            continue
        # 首条消息保留引用；正文已有对发送者的显式 @ 时不再自动添加。
        # 同段表情先发、正文后发时也检查整段，避免两条消息各 @ 一次。
        first_prefix = None
        if not sent_any and target.get("reply_message_id"):
            prefix = MessageSegment.reply(target["reply_message_id"])
            sender_id = str(target.get("reply_sender_id") or "").strip()
            already_mentions_sender = any(
                getattr(segment, "type", "") == "at"
                and str((getattr(segment, "data", {}) or {}).get("qq", "")).strip() == sender_id
                for _kind, message in outgoing
                for segment in message
            )
            if target.get("type") == "group" and sender_id and not already_mentions_sender:
                prefix = prefix + MessageSegment.at(sender_id) + MessageSegment.text(" ")
            kind, first_msg = outgoing[0]
            outgoing[0] = (kind, prefix + first_msg)
            first_prefix = prefix
        has_text = any(kind.startswith("text:") for kind, _msg in outgoing)
        for out_index, (kind, msg) in enumerate(outgoing):
            segment_identity = f"{kind}:{index}:{_message_dedup_text(msg).strip()}"
            segment_context = send_context.child(segment_identity) if send_context else None
            is_primary = kind.startswith("text:") or not has_text
            try:
                sent_message_id = await _send_qq_message(
                    bot,
                    target,
                    msg,
                    send_context=segment_context,
                    content_identity=segment_identity,
                )
            except Exception as exc:
                sent_message_id = None
                if kind.startswith("emoji:"):
                    sent_message_id = await _send_emoji_base64_fallback(
                        bot,
                        target,
                        ordered_groups[out_index][1],
                        exc,
                        prefix=first_prefix if out_index == 0 else None,
                        send_context=send_context,
                        segment_identity=segment_identity,
                    )
                if sent_message_id is None:
                    if is_primary:
                        # 正文（或纯表情段）失败：维持旧语义冒泡，由 SDK 按合约重试/死信。
                        raise
                    # 附带表情失败：正文已送达，只记日志，不阻塞、不重试整段。
                    logger.warning(
                        "onebot_emoji_send_failed (text already delivered) identity=%s error=%s",
                        segment_identity[:200], exc,
                    )
                    continue
            if is_primary and sent_message_id and source_attachments:
                state.REPLY_CACHE.remember(
                    sent_message_id,
                    str(target.get("conversation_key") or ""),
                    str(getattr(bot, "self_id", "") or ""),
                    source_attachments,
                )
            sent_any = True
            if len(outgoing) > 1:
                await asyncio.sleep(0.3)
        if index < len(parts) - 1:
            await asyncio.sleep(0.5)
    return sent_any


def _ordered_text_and_image_segments(
    segments: List[Dict[str, Any]],
) -> List[tuple[str, List[Dict[str, Any]]]]:
    """按原顺序生成可独立发送的文本组和图片组。

    相邻文本/at 合并为一条消息；每张有效图片独立成一条消息。空白文本组和缺少
    URL 的图片会被忽略，避免产生空消息。返回的 kind 含同类型序号，可用于稳定的
    幂等身份。
    """
    outgoing: List[tuple[str, List[Dict[str, Any]]]] = []
    text_group: List[Dict[str, Any]] = []
    text_index = 0
    image_index = 0

    def _visible_text(segment: Dict[str, Any]) -> str:
        if segment.get("type") == "at":
            return "@"
        content = segment.get("content")
        if content is None:
            # 兼容 MessageSegment 形态（data.text），避免误判为空段。
            data = segment.get("data")
            content = data.get("text", "") if isinstance(data, dict) else ""
        return str(content or "")

    def _flush_text_group() -> None:
        nonlocal text_group, text_index
        if any(_visible_text(segment).strip() for segment in text_group):
            outgoing.append((f"text:{text_index}", text_group))
            text_index += 1
        text_group = []

    for segment in segments:
        if segment.get("type") != "image":
            text_group.append(segment)
            continue
        if not segment.get("url"):
            continue
        _flush_text_group()
        outgoing.append((f"emoji:{image_index}", [segment]))
        image_index += 1

    _flush_text_group()
    return _tidy_split_text_groups(outgoing)


def _segment_text_value(segment: Dict[str, Any]) -> Optional[str]:
    """读取文本段内容；非文本段返回 None。"""
    if segment.get("type") != "text":
        return None
    if "content" in segment:
        return str(segment.get("content") or "")
    data = segment.get("data")
    if isinstance(data, dict):
        return str(data.get("text", "") or "")
    return ""


def _with_segment_text(segment: Dict[str, Any], text: str) -> Dict[str, Any]:
    """返回替换文本后的段副本，不修改原段。"""
    new_segment = dict(segment)
    if "content" not in segment and isinstance(segment.get("data"), dict):
        new_segment["data"] = {**segment["data"], "text": text}
    else:
        new_segment["content"] = text
    return new_segment


def _tidy_split_text_groups(
    groups: List[tuple[str, List[Dict[str, Any]]]],
) -> List[tuple[str, List[Dict[str, Any]]]]:
    """清理被表情拆开后的文本组边界。

    [2026-10-27] 表情单独成条后，原文里表情前后的换行/空格会落在下一条气泡
    开头，出现“空行 + 正文”；表情后紧跟的句末标点（如“，”“！”“）”）也会
    单独成为下一条气泡。这里：
      1. 去掉每个文本组首尾的空白；
      2. 表情后的文本组若以收尾标点开头，把标点挪回表情前最近的文本组末尾；
         前面没有正文时直接丢弃该标点；
      3. 清理后变空的文本组整体丢弃，并重新编号 kind。
    """
    tidied: List[tuple[str, List[Dict[str, Any]]]] = []
    last_text_pos: Optional[int] = None
    prev_is_emoji = False
    for kind, group in groups:
        if kind.startswith("emoji:"):
            tidied.append((kind, group))
            prev_is_emoji = True
            continue
        group = list(group)
        # 1) 表情后的收尾标点挪回前一段正文。
        if prev_is_emoji and group:
            first_text = _segment_text_value(group[0])
            if first_text is not None:
                match = state._EMOJI_TRAILING_PUNCT_RE.match(first_text)
                if match:
                    punct = match.group(1)
                    group[0] = _with_segment_text(group[0], first_text[match.end():])
                    if last_text_pos is not None:
                        prev_kind, prev_group = tidied[last_text_pos]
                        prev_group = list(prev_group)
                        tail_text = _segment_text_value(prev_group[-1])
                        if tail_text is not None:
                            prev_group[-1] = _with_segment_text(prev_group[-1], tail_text.rstrip() + punct)
                        else:
                            prev_group.append({"type": "text", "content": punct})
                        tidied[last_text_pos] = (prev_kind, prev_group)
        # 2) 去掉文本组首尾空白（at 段不受影响）。
        if group:
            head_text = _segment_text_value(group[0])
            if head_text is not None:
                group[0] = _with_segment_text(group[0], head_text.lstrip())
            tail_text = _segment_text_value(group[-1])
            if tail_text is not None:
                group[-1] = _with_segment_text(group[-1], tail_text.rstrip())
        group = [seg for seg in group if _segment_text_value(seg) != ""]
        prev_is_emoji = False
        if not any(
            seg.get("type") == "at" or (_segment_text_value(seg) or "").strip()
            for seg in group
        ):
            continue
        tidied.append((kind, group))
        last_text_pos = len(tidied) - 1

    result: List[tuple[str, List[Dict[str, Any]]]] = []
    text_index = 0
    image_index = 0
    for kind, group in tidied:
        if kind.startswith("emoji:"):
            result.append((f"emoji:{image_index}", group))
            image_index += 1
        else:
            result.append((f"text:{text_index}", group))
            text_index += 1
    return result


def _to_workspace_rel_path(raw_path: Any) -> str:
    """把绝对/任意路径尽量转为工作区相对 posix 路径，与其他附件字段保持一致。

    无法相对化（不在工作区内）时，直接返回原始绝对路径字符串。
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    try:
        p = Path(raw)
        if p.is_absolute():
            return p.relative_to(Path(CLONOTH_WORKSPACE)).as_posix()
        return p.as_posix()
    except Exception:
        return raw


def _resolve_attachment_path(attachment: Any) -> Optional[Path]:
    """把 Clonoth 附件解析为本地路径，兼容 dict 与字符串路径。"""
    # OneBot 适配层同时接受 dict 和 str，并统一解析 file://、绝对路径和工作区相对路径
    if isinstance(attachment, dict):
        raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    elif isinstance(attachment, str):
        raw_path = attachment
    else:
        raw_path = None
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else Path(CLONOTH_WORKSPACE) / path


def _attachment_filename(attachment: Any) -> str:
    """从附件对象中提取展示文件名。"""
    if isinstance(attachment, dict):
        return str(attachment.get("name") or attachment.get("filename") or "")
    if isinstance(attachment, str):
        text = attachment[7:] if attachment.startswith("file://") else attachment
        return Path(text).name
    return ""


def _is_napcat_sendmsg_timeout(exc: Exception) -> bool:
    """判断异常是否为 NapCat sendMsg 超时（消息大概率已发出，不应重试）。

    Why: NapCat 在图片/大消息上常回 retcode=1200、message 包含
    "Timeout: NTEvent ... sendMsg"，但实际消息已提交给 NTQQ 并发出。
    How: 只看 message/wording 中是否包含 timeout+sendMsg 关键字，命中才视为
    “超时但可能已送达”。Purpose: 避免 file:// 重试造成同一张图重复发送。

    [2026-07-22] 修复漏发：原实现把 retcode==1200 也当作“超时已送达”，
    但 1200 是 NapCat 的通用失败码——ENOENT（容器内临时文件缺失）等真失败
    同样回 1200，被误判为已送达后直接吞掉、既不重试也不补发，导致图片/回复
    彻底漏发。这里去掉 retcode==1200 兜底，只用 timeout+sendMsg 文本特征判定。
    """
    text = f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'wording', '') or ''} {exc}".lower()
    return "timeout" in text and "sendmsg" in text


def _is_napcat_enoent(exc: Exception) -> bool:
    """判断异常是否为 NapCat 容器内找不到临时文件（ENOENT）。

    Why: NapCat 跑在 Docker 里，发图/合成消息时会先落地到容器自己的
    /app/.config/QQ/NapCat/temp/ 临时目录再发送；该目录被过早清理或并发
    竞争时，NapCat 读不到自己的临时文件，报 retcode=1200 且 message 含
    "ENOENT: no such file or directory"。这类是**真失败**，消息没发出，
    必须补发（用 base64:// 内联绕开 temp 目录）。How: 匹配 message/wording
    中的 enoent / no such file 关键字。Purpose: 与“超时已送达”区分开，
    让 ENOENT 走 base64 补发而不是被静默吞掉。
    """
    text = f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'wording', '') or ''} {exc}".lower()
    return "enoent" in text or "no such file" in text


def _napcat_temp_enoent_path(exc: BaseException | None) -> str:
    """若异常（含 cause/__cause__ 链）是 NapCat temp 目录 ENOENT，返回报错路径，否则空串。"""
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = (
            f"{getattr(current, 'message', '') or ''} "
            f"{getattr(current, 'wording', '') or ''} {current}"
        )
        match = state._NAPCAT_TEMP_ENOENT_RE.search(text)
        if match:
            return match.group(1)
        current = getattr(current, "cause", None) or current.__cause__
    return ""


def _alert_napcat_temp_unwritable(exc: BaseException, action: str) -> bool:
    """NapCat temp ENOENT 时打一条独立的 ERROR 告警；返回是否命中。

    Why: NapCat 发送任何图片（URL / base64 都一样）都要先在
    /app/.config/QQ/NapCat/temp 下落文件。该目录不可写（例如宿主机 bind 源目录
    被删除）时所有图片/表情全部失败，而非单个表情的偶发问题，必须显式告警。
    """
    path = _napcat_temp_enoent_path(exc)
    if not path:
        return False
    logger.error(
        "NapCat 数据目录不可写 (napcat_temp_unwritable) action=%s path=%s："
        "所有图片/表情发送都会失败，请检查 NapCat 容器 /app/.config/QQ 挂载源目录是否存在且可写",
        action, path,
    )
    return True


def _is_napcat_eventchecker_failed(exc: Exception) -> bool:
    """判断异常是否为 NapCat sendMsg 的 EventChecker 失败（ack 不明确，禁止重发）。

    Why: [2026-09-17] NapCat 在 QQ 网络异常时回
    "EventChecker Failed: NTEvent ... sendMsg ... errMsg: 网络连接异常! /
    rich media transfer failed"。此时消息已经提交给 NTQQ，是否送达无法确认；
    日志证实同一条 @ 消息因此被补发成两条。How: 匹配 message/wording 中的
    eventchecker 关键字（配合 sendmsg）。Purpose: 与超时一样按“ack 不明确”
    处理，交给 send_contract 归类为 ambiguous，绝不在本层重发。
    """
    text = f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'wording', '') or ''} {exc}".lower()
    return "eventchecker" in text and "sendmsg" in text


def _is_napcat_ambiguous_ack(exc: Exception) -> bool:
    """sendMsg 超时或 EventChecker 失败：消息可能已发出，本层一律不重发。"""
    return _is_napcat_sendmsg_timeout(exc) or _is_napcat_eventchecker_failed(exc)


def _is_napcat_at_uid_error(exc: Exception) -> bool:
    """判断异常是否与 at/uid 解析有关（只有这类失败才值得把 at 降级为文本重发）。

    Why: [2026-09-17] 原实现只要消息里含 at、send_group_msg 抛任何 ActionFailed
    就立刻把 at 转文本再发一次；NapCat 超时/EventChecker 失败（消息其实已发出）
    也会触发，导致群里连续出现两条 @ 同一人的消息。How: 只看 NapCat 的
    message/wording（不看异常 repr，避免 data=None 之类字段误命中），匹配
    Get Uid Error / uid / uin / at / 群成员 等关键字。Purpose: 把 at-as-text
    兜底收窄到“确实因 at 解析失败”的场景。
    """
    text = f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'wording', '') or ''}"
    if not text.strip():
        text = f"{exc}"
    return bool(state._NAPCAT_AT_UID_ERROR_RE.search(text))


def _is_api_call_timeout(exc: Exception) -> bool:
    """判断异常是否为 NoneBot 侧“WebSocket call api ... timeout”。

    Why: 合并转发（send_group_forward_msg）等大耗时操作，NapCat 处理时间
    可能超过 NoneBot 默认 API 响应超时，报 NetWorkError('... call api ... timeout')；
    但日志证实 NapCat 实际已把消息发出。How: 匹配异常文本中
    "call api"+"timeout" 或 "networkerror"+"timeout" 关键字。Purpose: 避免把
    “已发出但未及时回 ack”误判为失败而重复发送。
    """
    text = f"{exc}".lower()
    if "timeout" not in text:
        return False
    return "call api" in text or "networkerror" in text or "network error" in text


async def _send_attachment_path(
    bot: Bot,
    target: Dict[str, Any],
    path: Path,
    filename: str = "",
    *,
    send_context: OutboundSendContext | None = None,
) -> str:
    """Send one attachment under one persistent logical owner claim."""
    display_name = filename or path.name
    if not path.exists():
        raise OneBotSendContractError(f"attachment does not exist: {path}")
    raw_bytes = path.read_bytes()
    context = send_context or OutboundSendContext(
        conversation_key=str(target.get("conversation_key") or "")
    )

    if path.suffix.lower() in inbound_attachments.IMAGE_SUFFIXES:
        identity = image_content_identity(raw_bytes)
        context = context.child(identity)
        key = make_idempotency_key(
            target, identity, event_id=context.idempotency_key or context.event_id,
        )
        claim = await state._outbound_idempotency.begin(key)
        if not claim.acquired and claim.state == "pending":
            resolved = await state._outbound_idempotency.wait_for_resolution(key)
            claim = (
                await state._outbound_idempotency.begin(key)
                if resolved is None else IdempotencyClaim(key, False, resolved)
            )
        if not claim.acquired:
            return f"idempotent:{key}"
        heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
        errors: list[BaseException] = []
        file_path = str(path.resolve())
        encoded = "base64://" + base64.b64encode(raw_bytes).decode("ascii")
        attempts = (
            MessageSegment.image(file=file_path),
            MessageSegment.image(file=encoded),
            MessageSegment.image(file=f"file://{file_path}"),
        )
        fields = _send_audit_fields(
            context, platform_message_id="", attempt=context.attempt,
            idempotency_key=key,
        )
        try:
            async def send_image() -> str:
                for index, message in enumerate(attempts, start=1):
                    try:
                        return await _send_qq_message_once(
                            bot, target, message, dedupe=index == 1,
                        )
                    except Exception as exc:
                        classified = classify_send_exception(exc)
                        if classified.ambiguous_ack:
                            raise classified from exc
                        errors.append(classified)
                failure = OneBotAttachmentBatchError(errors)
                logger.warning(
                    "onebot_send_failed",
                    extra={**fields, "retryable": failure.retryable, "send_error": str(failure)},
                )
                raise failure

            message_id = await protected_claim_send(
                state._outbound_idempotency,
                claim,
                send_image,
                sent_ttl=_sent_ttl_for_context(context),
                message_id_getter=lambda result: str(result or ""),
            )
            logger.info(
                "onebot_send_sent",
                extra=_send_audit_fields(
                    context, platform_message_id=message_id,
                    attempt=context.attempt, idempotency_key=key,
                ),
            )
            return message_id
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    identity = f"file:sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    context = context.child(identity)
    key = make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._outbound_idempotency.wait_for_resolution(key)
        claim = (
            await state._outbound_idempotency.begin(key)
            if resolved is None else IdempotencyClaim(key, False, resolved)
        )
    if not claim.acquired:
        return f"idempotent:{key}"
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    fields = _send_audit_fields(
        context, platform_message_id="", attempt=context.attempt,
        idempotency_key=key,
    )
    try:
        file_str = "base64://" + base64.b64encode(raw_bytes).decode("ascii")

        async def upload_file() -> Any:
            if target.get("type") == "group" and target.get("group_id") is not None:
                return await bot.call_api(
                    "upload_group_file", group_id=target["group_id"],
                    file=file_str, name=display_name,
                )
            if target.get("type") == "private" and target.get("user_id") is not None:
                return await bot.call_api(
                    "upload_private_file", user_id=target["user_id"],
                    file=file_str, name=display_name,
                )
            raise OneBotSendContractError(f"attachment target is invalid: {target!r}")

        result = await protected_claim_send(
            state._outbound_idempotency,
            claim,
            upload_file,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=_extract_sent_message_id,
        )
        message_id = _extract_sent_message_id(result)
        logger.info(
            "onebot_file_upload_sent",
            extra=_send_audit_fields(
                context, platform_message_id=message_id,
                attempt=context.attempt, idempotency_key=key,
            ),
        )
        return message_id or f"uploaded:{key}"
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def _target_forward_kind(target: Dict[str, Any]) -> tuple[str, int] | None:
    """从发送 target 解析出合并转发所需的 (类型, id)。无法解析时返回 None。"""
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        try:
            return "group", int(target["group_id"])
        except (TypeError, ValueError):
            return None
    if ttype == "private" and target.get("user_id") is not None:
        try:
            return "private", int(target["user_id"])
        except (TypeError, ValueError):
            return None
    return None


async def _try_send_images_as_forward(
    bot: Bot,
    target: Dict[str, Any],
    image_attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> bool:
    """尝试把多张图片用合并转发（forward node）一次性发送。

    Why: NapCat/NTQQ 逐张 send_group_msg 发大图时，每张都要 base64 上传并
    等 NTQQ ack，单张常超 baseTimeout(10s) 而报 retcode=1200，多图连发还会
    把 WS 心跳拖断（已在日志中观察到 keepalive ping timeout）。
    How: send_group_forward_msg / send_private_forward_msg 只需一次 API 调用，
    把每张图包成一个 node 一次提交。Purpose: 大幅减少往返与逐张超时，
    且失败时返回 False 回退到逐张直发。

    返回 True 表示已通过合并转发发出；False 表示需回退到逐张直发。
    """
    kind = _target_forward_kind(target)
    if kind is None:
        return False
    forward_type, forward_id = kind
    self_id = getattr(bot, "self_id", "") or "10000"
    nodes: list[dict[str, Any]] = []
    for attachment in image_attachments:
        path = _resolve_attachment_path(attachment)
        if path is None or not path.exists():
            continue
        node = {
            "type": "node",
            "data": {
                "user_id": str(self_id),
                "nickname": profiles.sanitize_name(IMAGE_FORWARD_MERGE_NICKNAME, max_len=32),
                "content": [
                    {"type": "image", "data": {"file": f"file://{str(path.resolve())}"}},
                ],
            },
        }
        nodes.append(node)
    if len(nodes) < 2:
        return False
    batch_identity = "forward:" + hashlib.sha256(
        "|".join(
            image_content_identity((_resolve_attachment_path(att) or Path()).read_bytes())
            for att in image_attachments
            if _resolve_attachment_path(att) is not None
        ).encode("utf-8")
    ).hexdigest()
    context = send_context.child(batch_identity) if send_context else OutboundSendContext()
    key = make_idempotency_key(
        target, batch_identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await state._outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await state._outbound_idempotency.wait_for_resolution(key)
        if resolved is None:
            claim = await state._outbound_idempotency.begin(key)
        else:
            claim = IdempotencyClaim(key, False, resolved)
    if not claim.acquired:
        return True
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    try:
        call_timeout = min(240.0, max(60.0, len(nodes) * 45.0))

        async def send_forward() -> Any:
            if forward_type == "group":
                return await bot.call_api(
                    "send_group_forward_msg", group_id=forward_id, messages=nodes, _timeout=call_timeout,
                )
            return await bot.call_api(
                "send_private_forward_msg", user_id=forward_id, messages=nodes, _timeout=call_timeout,
            )

        result = await protected_claim_send(
            state._outbound_idempotency,
            claim,
            send_forward,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=_extract_sent_message_id,
        )
        logger.info(
            "onebot_forward_sent",
            extra=_send_audit_fields(
                context, platform_message_id=_extract_sent_message_id(result),
                attempt=context.attempt, idempotency_key=key,
            ),
        )
        return True
    except Exception as exc:
        classified = classify_send_exception(exc)
        logger.warning(
            "onebot_forward_failed",
            extra={
                **_send_audit_fields(context, platform_message_id="", attempt=context.attempt, idempotency_key=key),
                "retryable": classified.retryable, "send_error": str(classified),
            },
        )
        raise classified from exc
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _send_attachments(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """发送 Clonoth 返回的附件列表。

    每发出一个附件，就把返回的 message_id -> 本地附件路径 登记下来，
    让用户引用这条（如绘图产物）时能把原图作为附件送回给 AI。
    """
    # [2026-07-17] 多图合并转发优化：当图片张数 ≥ 阈值时，优先用
    # send_group_forward_msg 一次性发送，避免逐张 sendMsg 超时与 WS 拖断。
    if ENABLE_IMAGE_FORWARD_MERGE:
        image_atts = [
            att for att in attachments
            if (p := _resolve_attachment_path(att)) is not None
            and p.exists() and p.suffix.lower() in inbound_attachments.IMAGE_SUFFIXES
        ]
        non_image_atts = [
            att for att in attachments
            if (p := _resolve_attachment_path(att)) is None
            or not p.exists() or p.suffix.lower() not in inbound_attachments.IMAGE_SUFFIXES
        ]
        if len(image_atts) >= IMAGE_FORWARD_MERGE_THRESHOLD:
            merged = await _try_send_images_as_forward(
                bot, target, image_atts, send_context=send_context,
            )
            if merged:
                # 合并转发不返回逐张 message_id，无法逐张登记 message_id->path；
                # 但会路非图片附件继续逐个发送。
                if non_image_atts:
                    await _send_attachments_one_by_one(
                        bot, target, non_image_atts, send_context=send_context
                    )
                return
            # 合并转发失败，回退到逐张直发（下方通用循环）。
    await _send_attachments_one_by_one(
        bot, target, attachments, send_context=send_context
    )


async def _send_attachments_one_by_one(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """逐张/逐个发送附件，单个失败不阻断后续。"""
    # [2026-07-17] 批量发图（如一次生 4 张）时，单张 sendMsg 可能耗时较长；
    # 若其中一张抛异常（如 NapCat 超时/WS 短暂断开），旧逻辑会让整个循环
    # 中断，导致后续图片全部丢失（现象：“要 4 张只发出 1 张”）。
    # 这里逐张单独容错，单张失败不阻断后续图片发送。
    total = len(attachments)
    failures: list[BaseException] = []
    for idx, attachment in enumerate(attachments):
        path = _resolve_attachment_path(attachment)
        filename = _attachment_filename(attachment)
        if path is None:
            if filename:
                await _send_qq_message(
                    bot, target, f"Clonoth 生成了文件：{filename}",
                    send_context=(send_context.child(f"unsupported:{filename}") if send_context else None),
                )
            else:
                logger.warning("skip unsupported QQ attachment payload: %r", attachment)
            continue
        try:
            message_id = await _send_attachment_path(
                bot,
                target,
                path,
                filename=filename,
                send_context=send_context,
            )
        except Exception as exc:  # noqa: BLE001
            classified = classify_send_exception(exc)
            failures.append(classified)
            logger.warning(
                "onebot_attachment_failed",
                extra={
                    "attachment_index": idx + 1, "attachment_total": total,
                    "attachment_name": filename or path.name,
                    "retryable": classified.retryable,
                    "event_id": (send_context.event_id if send_context else ""),
                    "attempt": (send_context.attempt if send_context else 1),
                    "idempotency_key": (send_context.idempotency_key if send_context else ""),
                },
            )
            continue
        if message_id:
            _remember_message_attachments(message_id, [attachment])
    if failures:
        raise OneBotAttachmentBatchError(failures)


def _sent_attachment_bucket_key(target: Dict[str, Any]) -> str:
    """为最近发送附件索引生成稳定的会话桶 key。

    优先用 group_id/user_id（Bot 进程内部值），避免 real/stable conversation_key
    不一致导致记录与查询对不上。
    """
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        return f"group:{int(target['group_id'])}"
    if ttype == "private" and target.get("user_id") is not None:
        return f"private:{int(target['user_id'])}"
    conv_key = str(target.get("conversation_key") or "")
    return f"conv:{conv_key}" if conv_key else ""


def _record_sent_attachments(target: Dict[str, Any], attachments: List[Any]) -> None:
    """把 Bot 发出/生成的附件记入会话级最近附件索引，供 qq_forward 后续检索。

    Why: 生图插件产出的图片文件名随机，用户说“把刚才那张图发给 xx”时
    AI 无从得知具体路径。How: 发送时把已解析的本地路径 + 显示名 + 时间戳
    按会话桶缓存。Purpose: 让后续转发能按“最近生成/发送”定位图片。
    """
    bucket = _sent_attachment_bucket_key(target)
    if not bucket or not attachments:
        return
    now = time.time()
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in inbound_attachments.IMAGE_SUFFIXES
        state._recent_sent_attachments[bucket].append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
            "timestamp": now,
        })


def _remember_message_attachments(message_id: Any, attachments: List[Any]) -> None:
    """登记 Bot 已发出消息的 message_id -> 本地附件路径列表。

    仅记录能解析出本地路径的附件（图片/文件），供用户引用这条消息时回取原图。
    """
    mid = str(message_id or "").strip()
    if not mid or not attachments:
        return
    records: List[Dict[str, Any]] = []
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in inbound_attachments.IMAGE_SUFFIXES
        records.append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
        })
    if not records:
        return
    state._sent_message_attachments[mid] = records
    state._sent_message_attachments.move_to_end(mid)
    while len(state._sent_message_attachments) > state._MESSAGE_ATTACHMENT_MAX_ITEMS:
        state._sent_message_attachments.popitem(last=False)


def _message_attachment_records(message_id: Any, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回某条 Bot 已发消息登记的本地附件记录（过滤不存在的文件）。"""
    mid = str(message_id or "").strip()
    if not mid:
        return []
    records: list[dict[str, Any]] = []
    for item in state._sent_message_attachments.get(mid, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    return records


def _recent_sent_attachment_records(bucket_key: str, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回会话最近发出/生成的附件（旧→新），可选只要图片，并过滤不存在的文件。"""
    bucket = str(bucket_key or "")
    if not bucket:
        return []
    records: list[dict[str, Any]] = []
    for item in state._recent_sent_attachments.get(bucket, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    return records


def _attachment_send_lock_for(target: Dict[str, Any]) -> asyncio.Lock:
    key = _sent_attachment_bucket_key(target) or str(target.get("conversation_key") or "default")
    lock = state._attachment_send_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        state._attachment_send_locks[key] = lock
    return lock


def _ensure_bucket_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 target 的 group_id/user_id，使最近附件登记 bucket 稳定可查。

    [2026-07-19] 生图（gpt_image_2 / gemini_image）经 emit_intermediate 后台
    推图时，走 send_to_channel fallback，其 target 可能只有 conversation_key
    而缺 group_id/user_id。若不补齐，_sent_attachment_bucket_key 会退化成
    conv:<key> 桶，而 qq_forward op=recent 用 group:<群号>/private:<qq> 桶查询，
    两者对不上导致查不到刚生成的图。这里从 conversation_key 反解出
    group_id/user_id 补进 target，保证登记与查询命中同一个桶。
    """
    ttype = str(target.get("type") or "")
    conv_key = state.ANON.real_conversation_key(str(target.get("conversation_key") or ""))
    if ttype == "group" and target.get("group_id") is None and conv_key:
        gid = anon.group_id_from_conversation_key(conv_key)
        if gid is not None:
            target["group_id"] = gid
    if ttype == "private" and target.get("user_id") is None and conv_key:
        uid = anon.private_user_id_from_conversation_key(conv_key)
        if uid is not None:
            target["user_id"] = uid
    return target


async def _send_attachments_confirmed(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """串行发送附件，并仅在全部平台确认后登记最近附件索引。"""
    lock = _attachment_send_lock_for(target)
    try:
        async with lock:
            await _send_attachments(
                bot, target, attachments, send_context=send_context
            )
            # Register/finish only after every platform-visible send is confirmed.
            _record_sent_attachments(_ensure_bucket_target(dict(target)), attachments)
    except Exception:
        logger.warning("onebot_attachment_batch_failed", exc_info=True)
        raise
    conv_key = target.get("conversation_key")
    if conv_key:
        _mark_qq_reply_finished(str(conv_key))


async def _send_text_and_attachments(
    bot: Bot,
    target: Dict[str, Any],
    text: str,
    attachments: List[Any],
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> None:
    """统一发送最终文本/附件，并持久绑定回复所依据的来源图片。"""
    conv_key = target.get("conversation_key")
    # 2026-05-01 修改原因：私聊没有群历史缓存，不应写入 _group_history；群聊
    # 仍保留原来的 Bot 回复入库逻辑，维持后续 @Bot 请求的上下文连续性。
    if text:
        if await _send_split_text(
            bot,
            target,
            text,
            source_attachments=source_attachments,
            send_context=send_context,
        ) and target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None:
                history._record_bot_reply(int(group_id), text)
    if attachments:
        # Delivery completion means the platform-confirmed final attachment send,
        # not merely scheduling a background task.  Failures propagate to the SDK
        # so its outbox cannot acknowledge/consume the trigger prematurely.
        await _send_attachments_confirmed(
            bot, target, attachments, send_context=send_context
        )
        return
    if conv_key:
        _mark_qq_reply_finished(str(conv_key))


async def _set_message_react(bot: Bot, event: Any, emoji_id: str, enabled: bool) -> bool:
    """设置或移除触发消息上的 QQ React，失败时静默返回 False。"""
    # 2026-05-03 修改原因：React 阶段切换需要多处复用 OneBot 扩展 API。
    # 做法是把单次 set_msg_emoji_like 调用包进独立函数，并强制 emoji_id 为 str；
    # 目的在于满足每次 API 调用都单独容错，避免 React 失败影响消息回复。
    if not ENABLE_REACTIONS or not bot or not event or not hasattr(event, "message_id"):
        return False
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=int(event.message_id),
            emoji_id=str(emoji_id),
            set=enabled,
        )
        return True
    except Exception:
        return False


async def _switch_react_stage(
    bot: Bot,
    event: Any,
    platform_data: Dict[str, Any],
    stage: str,
) -> None:
    """按单调阶段切换触发消息 React，防止流式事件造成重复抖动。"""
    # 2026-05-03 修改原因：stream_delta 会持续到达，tool 进度也可能被 sweep 多次看到。
    # 做法是在 trigger.platform_data 中记录当前阶段和 emoji_id，并只允许阶段前进；
    # 目的在于首个推理片段和首次工具调用各切换一次，不对每条事件重复调 API。
    next_order = state._REACT_STAGE_ORDER.get(stage, 0)
    current_stage = str(platform_data.get("_react_stage") or "")
    current_order = state._REACT_STAGE_ORDER.get(current_stage, 0)
    if not next_order or current_order >= next_order:
        return

    next_emoji = state._REACT_STAGE_EMOJIS[stage]
    current_emoji = str(platform_data.get("_react_stage_emoji") or state._REACT_STAGE_EMOJIS.get(current_stage, ""))
    if current_emoji and current_emoji != next_emoji:
        await _set_message_react(bot, event, current_emoji, False)
    elif not current_emoji:
        # 2026-05-03 修改原因：旧 trigger 或私聊路径可能没有阶段元数据。
        # 做法是在缺少当前 emoji 时清理所有已知阶段 emoji；目的在于补齐状态链时
        # 不让 76、281、178 或 97 残留在同一条触发消息上。
        for emoji_id in state._REACT_CLEANUP_EMOJIS:
            if emoji_id != next_emoji:
                await _set_message_react(bot, event, emoji_id, False)

    if await _set_message_react(bot, event, next_emoji, True):
        platform_data["_react_stage"] = stage
        platform_data["_react_stage_emoji"] = next_emoji


async def _clear_message_reacts(bot: Bot, event: Any) -> None:
    """移除任务生命周期使用过的全部 QQ React。"""
    # 2026-05-03 修改原因：最终回复代表任务结束，触发消息不应继续显示中间状态。
    # 做法是按统一清理列表逐个移除 76、281、178、97、326；目的在于包含本轮
    # 新增的推理和工具阶段，并让任一移除失败都不影响最终回复发送。
    for emoji_id in state._REACT_CLEANUP_EMOJIS:
        await _set_message_react(bot, event, emoji_id, False)


def _progress_mentions_search(record: str) -> bool:
    """判断进度记录是否与联网搜索工具相关。"""
    text = str(record or "").lower()
    return any(keyword in text for keyword in state._SEARCH_PROGRESS_KEYWORDS)


def _progress_is_tool_start(record: str) -> bool:
    """是否为“[node] 执行 N 个工具：...”这类工具开始执行的进度行。"""
    text = str(record or "")
    return "执行" in text and "个工具" in text


def _search_in_progress(progress_records: List[str]) -> bool:
    """仅当“最近一条搜索相关进度是开始执行、且尚未出现结果行”时返回 True。

    [2026-09-17] Why: 原实现只要 progress_records 里出现过 web_search 字样就持续
    发“还在联网搜索中”，搜索早就拿到结果（甚至答案已生成）也照刷不误。
    How: 按顺序扫描进度行：“执行 N 个工具：web_search”视为搜索开始；之后任何
    提到搜索工具的非开始行（“web_search: 已获得结果/失败/已自动转为异步…”）
    视为搜索结束。Purpose: 只在搜索真正进行中才发可见进度提示。
    """
    running = False
    for record in progress_records or []:
        if not _progress_mentions_search(record):
            continue
        if _progress_is_tool_start(record):
            running = True
        else:
            running = False
    return running


async def _maybe_send_search_progress_notice(
    bot: Bot,
    target: Dict[str, Any],
    platform_data: Dict[str, Any],
) -> None:
    """在 QQ 侧为长搜索任务发送低频可见进度提示（调用方保证搜索确实在进行中）。"""
    now = time.time()
    request_identity = str(
        platform_data.setdefault("_qq_search_request_identity", uuid.uuid4().hex)
    )
    context = commands._request_send_context(
        "search-progress", request_identity,
        str(target.get("conversation_key") or ""),
    )
    if not platform_data.get("_qq_search_notice_sent"):
        platform_data["_qq_search_notice_sent"] = True
        platform_data["_qq_search_notice_last_at"] = now
        try:
            await _send_qq_message(
                bot, target, state._SEARCH_PROGRESS_FIRST_NOTICE,
                send_context=context.child("first"),
            )
        except Exception:
            logger.debug("send QQ search progress notice failed", exc_info=True)
        return

    last_at = float(platform_data.get("_qq_search_notice_last_at") or 0.0)
    if now - last_at < state._SEARCH_PROGRESS_STILL_RUNNING_INTERVAL_SEC:
        return
    # [2026-09-17] still-running 提示设上限，达到后不再发（也不再刷新时间）。
    still_count = int(platform_data.get("_qq_search_notice_still_count") or 0)
    if still_count >= max(0, state._SEARCH_PROGRESS_STILL_RUNNING_MAX_COUNT):
        return
    platform_data["_qq_search_notice_last_at"] = now
    platform_data["_qq_search_notice_still_count"] = still_count + 1
    try:
        await _send_qq_message(
            bot, target, state._SEARCH_PROGRESS_STILL_RUNNING_NOTICE,
            send_context=context.child(f"still:{still_count + 1}"),
        )
    except Exception:
        logger.debug("send QQ search still-running notice failed", exc_info=True)
