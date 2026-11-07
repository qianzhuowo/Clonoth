"""Group/private trigger rules, inbound text building, route state and the inbound queue."""
from __future__ import annotations

import logging
from .attachment_policy import is_outbound_or_self_event
from .attachment_policy import source_attachments_from_merged
from .config import AUTO_LIKE_TIMES
from .config import ENABLE_AUTO_LIKE
from .config import ENABLE_PREEMPT
from .config import ENABLE_QQ_QUEUE
from .config import ENABLE_REACTIONS
from .config import ENTRY_NODE_ID
from .config import GROUP_TRIGGER
from .config import ONEBOT_STATE_FILE
from .config import QQ_QUEUE_INTERVAL
from .config import QQ_QUEUE_REPLY_TIMEOUT
from .config import QQ_QUEUE_WAIT_FOR_REPLY
from .config import TRIGGER_PREFIXES
from clonoth_sdk import TriggerInfo
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import asyncio
import datetime as dt
import json
import time

from . import access, history, message_parse, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

def _event_is_outbound_or_self(event: Event, bot: Bot | None = None) -> bool:
    """过滤 Bot 自身回显及 message_sent 文件状态事件，避免形成回复回路。"""
    self_ids = {
        getattr(bot, "self_id", None),
        getattr(event, "self_id", None),
    }
    return is_outbound_or_self_event(
        post_type=getattr(event, "post_type", None),
        user_id=getattr(event, "user_id", None),
        self_ids=self_ids,
    )


async def _allowed_group_rule(bot: Bot, event: Event) -> bool:
    """把群类型和白名单放在规则层过滤，避免无关消息进入上下文缓存。"""
    if not isinstance(event, GroupMessageEvent) or _event_is_outbound_or_self(event, bot):
        return False
    return access.is_group_allowed(int(event.group_id))


async def _agent_group_rule(bot: Bot, event: Event) -> bool:
    """匹配允许群里的 Agent 触发消息，兼容 @Bot / 前缀 / 全量触发。"""
    if not isinstance(event, GroupMessageEvent) or _event_is_outbound_or_self(event, bot):
        return False
    if not access.is_group_allowed(int(event.group_id)):
        return False
    text = message_parse.message_to_text(event.get_message(), state.at_name, getattr(bot, "self_id", None)).strip()
    return _group_should_trigger(event, bot, text)


async def _private_message_rule(bot: Bot, event: Event) -> bool:
    """只匹配真实用户私聊，排除 Bot 回显和 message_sent 系统状态。"""
    # 2026-05-01 修改原因：私聊没有群号，也不需要 @Bot；这里单独识别
    # PrivateMessageEvent，使私聊入口和现有群聊入口互不影响。
    return isinstance(event, PrivateMessageEvent) and not _event_is_outbound_or_self(event, bot)


def _serializable_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """提取可持久化的 QQ 回复目标，避免把 Bot/Event 对象写进状态文件。"""
    allowed = {"type", "group_id", "user_id", "conversation_key"}
    return {key: target[key] for key in allowed if key in target and target[key] is not None}


def _load_route_state() -> None:
    """加载 stable conversation/session 到真实 QQ 目标的本地路由状态。"""
    path = Path(ONEBOT_STATE_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load OneBot route state: %s", path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    real_map = data.get("real_conversation_keys")
    if isinstance(real_map, dict):
        state.ANON.real_conversation_keys.update({str(k): str(v) for k, v in real_map.items() if str(k) and str(v)})
        state.ANON.stable_conversation_keys.update({str(v): str(k) for k, v in state.ANON.real_conversation_keys.items() if str(k) and str(v)})

    targets = data.get("session_targets")
    if isinstance(targets, dict):
        for sid, target in targets.items():
            if isinstance(target, dict):
                state._persisted_session_targets[str(sid)] = _serializable_target(target)

    logger.info("loaded OneBot route state from %s", path)


async def _save_route_state() -> None:
    """保存本地路由状态，保障 bot.py 重启后能把 stable key 映射回真实 QQ 目标。"""
    async with state._route_state_lock:
        path = Path(ONEBOT_STATE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "real_conversation_keys": dict(state.ANON.real_conversation_keys),
                "session_targets": {
                    sid: _serializable_target(target)
                    for sid, target in {**state._persisted_session_targets, **state._session_targets}.items()
                    if isinstance(target, dict)
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("failed to save OneBot route state: %s", path, exc_info=True)


async def _remember_route_state(
    *,
    stable_conversation_key: str,
    real_conversation_key: str,
    session_id: str = "",
    target: Optional[Dict[str, Any]] = None,
) -> None:
    if stable_conversation_key and real_conversation_key:
        state.ANON.real_conversation_keys[stable_conversation_key] = real_conversation_key
        state.ANON.stable_conversation_keys[real_conversation_key] = stable_conversation_key
    if session_id and target:
        state._persisted_session_targets[session_id] = _serializable_target(target)
    await _save_route_state()


def _bot_self_id_candidates(event: Event | None, bot: Bot | None) -> set[str]:
    """收集 Bot 自身 QQ 号候选值，兼容不同 OneBot/NoneBot 对 self_id 的暴露方式。"""
    candidates: set[str] = set()
    for source in (bot, event):
        if source is None:
            continue
        for attr in ("self_id", "bot_id"):
            value = getattr(source, attr, None)
            if value is not None and str(value).strip():
                candidates.add(str(value).strip())
    return candidates


def _qq_matches_bot(qq: Any, bot_ids: set[str]) -> bool:
    value = str(qq or "").strip()
    return bool(value and value in bot_ids)


def _group_message_mentions_bot(message: Any, bot_ids: set[str]) -> bool:
    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for segment in segments:
        seg_type, data = message_parse.segment_type_and_data(segment)
        if seg_type != "at":
            continue
        if _qq_matches_bot(data.get("qq"), bot_ids):
            return True
    if isinstance(message, str):
        return _raw_message_mentions_bot(message, bot_ids)
    return False


def _raw_message_mentions_bot(raw_message: Any, bot_ids: set[str]) -> bool:
    raw = str(raw_message or "")
    if not raw or not bot_ids:
        return False
    for match in state._AT_QQ_RE.finditer(raw):
        if _qq_matches_bot(match.group(1), bot_ids):
            return True
    return False


def _event_mentions_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    """判断群消息是否 @ 当前 Bot。

    NapCat/OneBot 实现和 NoneBot 版本不同，@Bot 可能表现为：
    1. 标准 MessageSegment.at；
    2. event.to_me / event.is_tome()；
    3. raw_message 中的 CQ/日志风格 at 标记。

    这里把三种信号都纳入判断，避免控制台能看到 [at:qq=...] 但
    mention_only 规则没有触发正式回复。
    """
    bot_ids = _bot_self_id_candidates(event, bot)
    try:
        if bool(getattr(event, "to_me", False)):
            return True
    except Exception:
        pass
    try:
        is_tome = getattr(event, "is_tome", None)
        if callable(is_tome) and bool(is_tome()):
            return True
    except Exception:
        pass
    try:
        if _group_message_mentions_bot(event.get_message(), bot_ids):
            return True
    except Exception:
        pass
    return _raw_message_mentions_bot(getattr(event, "raw_message", ""), bot_ids)


def _group_should_trigger(event: GroupMessageEvent, bot: Bot, text: str) -> bool:
    mode = (GROUP_TRIGGER or "mention_only").lower()
    if mode in {"all", "always"}:
        return True
    mentions_bot = _event_mentions_bot(event, bot)
    if mode in {"prefix", "prefix_or_mention", "mention_or_prefix"}:
        return mentions_bot or any((text or "").strip().startswith(prefix) for prefix in TRIGGER_PREFIXES)
    return mentions_bot


def _strip_trigger_prefix(text: str) -> str:
    value = (text or "").strip()
    for prefix in TRIGGER_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):].strip() or value
    return value


async def _auto_like_user(bot: Bot, user_id: int) -> None:
    if not ENABLE_AUTO_LIKE:
        return
    today = dt.datetime.now(state._CST).strftime("%Y-%m-%d")
    if state._auto_like_today.get(user_id) == today:
        return
    try:
        await bot.call_api("send_like", user_id=int(user_id), times=int(AUTO_LIKE_TIMES))
        state._auto_like_today[user_id] = today
    except Exception:
        logger.debug("send_like failed for user=%s", user_id, exc_info=True)


async def _build_inbound_text(event: GroupMessageEvent, bot: Bot, user_text: str, conversation_key: str, attachments: List[Dict[str, Any]]) -> str:
    """组装提交给 tangqiu_main 的群聊 inbound 文本，并匿名化 QQ 群号/用户号。"""
    group_id = int(event.group_id)
    history_lines = list(state._group_history[group_id])[-state._HISTORY_MAX_LEN:]
    current_name = state.ANON.anonymize_text(state.PROFILES.sender_display_name(event.sender, event.user_id))
    current_user = state.ANON.anonymize_user_id(event.user_id)
    now = dt.datetime.now(state._CST).strftime("%Y-%m-%d %H:%M CST")

    parts: List[str] = ["【群聊上下文记录】"]
    parts.extend(history_lines or ["（暂无）"])

    reply_context, quoted_attachments = await history._build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])

    parts.extend([
        "",
        f"当前时间: {now}",
        "【当前用户身份】",
        *state.PROFILES.identity_lines(event.user_id, current_name, is_admin=access.is_admin_user(event.user_id)),
        "",
        "【当前用户指令】",
        f"{current_name}（{current_user}）: {state.ANON.anonymize_text(user_text)}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


def _parse_direct_draw_command(user_text: str) -> Optional[str]:
    """解析 QQ 侧 /生图 直达命令；返回清理后的绘图需求，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None
    match = state._DRAW_DIRECT_RE.match(text)
    if not match:
        return None
    prompt = (match.group(1) or "").strip()
    if not prompt:
        prompt = "请根据上下文生成一张合适的图片"
    return prompt


async def _build_draw_direct_inbound_text(event: Event, user_text: str, is_dm: bool) -> str:
    """构造直达绘图节点的入站文本，避免普通入口 AI 再判断一次意图。"""
    now = dt.datetime.now(state._CST).strftime("%Y-%m-%d %H:%M CST")
    name = state.ANON.anonymize_text(state.PROFILES.sender_display_name(getattr(event, "sender", None), getattr(event, "user_id", "")))
    user = state.ANON.anonymize_user_id(getattr(event, "user_id", ""))
    target = "私聊" if is_dm else "群聊"
    return "\n".join([
        f"当前时间: {now}",
        f"来源: QQ{target} / /生图 直达命令",
        "【当前用户身份】",
        *state.PROFILES.identity_lines(getattr(event, "user_id", ""), name, is_admin=access.is_admin_user(getattr(event, "user_id", ""))),
        "",
        "【绘图请求】",
        f"{name}（{user}）: {user_text}",
        "",
        "请直接按绘图节点规则处理：如果用户要求画图/生图则生成图片；如果用户明确只要提示词/tag则只给提示词。",
    ])


async def _build_private_inbound_text(event: PrivateMessageEvent, bot: Bot, user_text: str, conversation_key: str, attachments: List[Dict[str, Any]]) -> str:
    """组装提交给 tangqiu_main 的私聊 inbound 文本，并匿名化 QQ 用户号。"""
    fallback_text = await state.forward_expander(bot).to_text(event.get_message(), getattr(bot, "self_id", None))
    text = state.ANON.anonymize_text((user_text or fallback_text).strip() or "你好")
    name = state.ANON.anonymize_text(state.PROFILES.sender_display_name(event.sender, event.user_id))
    user = state.ANON.anonymize_user_id(event.user_id)
    now = dt.datetime.now(state._CST).strftime("%Y-%m-%d %H:%M CST")
    parts: List[str] = [f"当前时间: {now}"]
    reply_context, quoted_attachments = await history._build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])
    parts.extend([
        "",
        "【当前用户身份】",
        *state.PROFILES.identity_lines(event.user_id, name, is_admin=access.is_admin_user(event.user_id)),
        "",
        "【当前用户指令】",
        f"{name}（{user}）: {text}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


async def _try_preempt_running_task(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    inbound_text: str,
    attachments: Optional[List[Dict[str, Any]]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
) -> bool:
    """尝试把 QQ 新消息注入当前会话正在运行的入口任务。"""
    # 2026-05-03 修改原因：TangQiu 以前每次消息都 submit_inbound，会在同一
    # 会话已有入口任务运行时并发启动新任务。这里移植 Discord 的 Preempt V2：
    # 先查询当前 session 的入口任务，再用 preempt_task 注入新消息；目的
    # 是让同一会话的新指令打断并接续旧任务，而不是重复开任务。
    if state._client is None or state._session_state is None:
        return False

    existing_sid = state._session_state.get_session_id(conversation_key)
    if not existing_sid:
        return False

    try:
        running_tasks = await state._client.get_running_tasks(existing_sid)
    except Exception:
        logger.exception("preempt_v2: get running tasks failed")
        return False

    for rt in running_tasks:
        if not rt.task_id or not rt.is_user_entry:
            continue

        rt_src_seq = rt.source_inbound_seq or 0
        rt_trigger = state._session_state.get_trigger(rt_src_seq) if rt_src_seq else None
        if not rt_trigger:
            result = state._session_state.find_trigger_by_session(existing_sid)
            rt_trigger = result[1] if result else None

        if not is_dm:
            # 2026-05-03 修改原因：群聊里多个用户共享同一个 conversation_key。
            # 这里必须用旧 trigger 保存的 event.user_id 校验来源；无法确认同一用户
            # 时不执行 preempt，目的是避免一个群成员打断另一个群成员的任务。
            if not rt_trigger:
                continue
            rt_event = rt_trigger.platform_data.get("event")
            if getattr(rt_event, "user_id", None) != getattr(event, "user_id", None):
                continue

        try:
            ok = await state._client.preempt_task(
                rt.task_id,
                message=inbound_text,
                attachments=attachments,
            )
        except Exception:
            logger.exception("preempt_v2: preempt task failed")
            continue

        if not ok:
            continue

        # 2026-05-03 修改原因：Preempt 成功后不会生成新的 inbound_seq。
        # 因此必须把旧 trigger 的平台对象更新为本次 QQ event 和 bot；目的
        # 是让最终回复引用新的触发消息，并继续发到正确的 QQ 会话。
        fresh_platform = dict(platform_updates)
        fresh_platform["last_typing_time"] = time.time()
        if rt_trigger:
            rt_trigger.platform_data.update(fresh_platform)
        elif rt_src_seq:
            state._session_state.register_trigger(
                TriggerInfo(
                    inbound_seq=rt_src_seq,
                    conversation_key=conversation_key,
                    session_id=existing_sid,
                    is_dm=is_dm,
                    platform_data=fresh_platform,
                )
            )

        state._session_targets[existing_sid] = dict(fresh_platform)
        state._conversation_bots[conversation_key] = bot
        await _remember_route_state(
            stable_conversation_key=conversation_key,
            real_conversation_key=state.ANON.real_conversation_key(conversation_key),
            session_id=existing_sid,
            target=fresh_platform,
        )
        logger.info("preempt_v2: injected into QQ task %s", rt.task_id[:8])
        return True

    return False


async def _submit_or_preempt_inbound(
    *,
    bot: Bot,
    event: Event,
    channel: str,
    real_conversation_key: str,
    stable_conversation_key: str,
    inbound_text: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
    entry_node_id: str = "",
) -> bool:
    """提交或打断 QQ 入站消息；对外使用稳定哈希 conversation_key，对内保留真实路由。"""
    if state._client is None or state._session_state is None:
        return False

    if ENABLE_PREEMPT:
        preempt_ok = await _try_preempt_running_task(
            bot=bot,
            event=event,
            conversation_key=stable_conversation_key,
            inbound_text=inbound_text,
            attachments=attachments or None,
            is_dm=is_dm,
            platform_updates=platform_updates,
        )
        if preempt_ok:
            return True

    result = await state._client.submit_inbound(
        channel=channel,
        conversation_key=stable_conversation_key,
        text=inbound_text,
        message_id=str(getattr(event, "message_id", "")),
        attachments=attachments or None,
        use_context=True,
        entry_node_id=entry_node_id or ENTRY_NODE_ID,
        platform_auth={
            "platform": "qq",
            "user_id": str(getattr(event, "user_id", "")),
            "is_admin": access.is_admin_user(getattr(event, "user_id", "")),
        },
        route_hints={
            "platform": "qq",
            "channel": channel,
            "has_image": any(str(a.get("type") or "") == "image" for a in (attachments or [])),
            "target_type": "private" if is_dm else "group",
        },
    )
    if not result.session_id or not result.accepted:
        return False

    state.ANON.real_conversation_keys[stable_conversation_key] = real_conversation_key
    state._session_state.register_session(stable_conversation_key, result.session_id)
    target = dict(platform_updates)
    target["conversation_key"] = stable_conversation_key
    state._session_targets[result.session_id] = target
    state._conversation_bots[stable_conversation_key] = bot
    await _remember_route_state(
        stable_conversation_key=stable_conversation_key,
        real_conversation_key=real_conversation_key,
        session_id=result.session_id,
        target=target,
    )

    if result.inbound_seq:
        react_meta: Dict[str, Any] = {}
        if platform_updates.get("type") == "group" and ENABLE_REACTIONS:
            react_meta = {
                "_react_stage": "submitted",
                "_react_stage_emoji": state._REACT_STAGE_EMOJIS["submitted"],
            }
        trigger = TriggerInfo(
            inbound_seq=result.inbound_seq,
            conversation_key=stable_conversation_key,
            session_id=result.session_id,
            is_dm=is_dm,
            platform_data={
                **platform_updates,
                "last_typing_time": time.time(),
                **react_meta,
            },
        )
        state._session_state.register_trigger(trigger)
    return True


async def _enqueue_or_submit_inbound(item: state.QueuedInbound) -> bool:
    if not ENABLE_QQ_QUEUE:
        return await _submit_or_preempt_inbound(
            bot=item.bot,
            event=item.event,
            channel=item.channel,
            real_conversation_key=item.real_conversation_key,
            stable_conversation_key=item.stable_conversation_key,
            inbound_text=item.text,
            user_text=item.user_text,
            attachments=item.attachments,
            is_dm=item.is_dm,
            platform_updates=item.platform_updates,
            entry_node_id=item.entry_node_id,
        )

    async with state._qq_queue_condition:
        existing = state._qq_queue_by_key.get(item.stable_conversation_key)
        if existing is not None:
            existing.text = f"{existing.text}\n\n【排队期间追加消息】\n{item.text}"
            existing.attachments.extend(item.attachments)
            existing.event = item.event
            existing.platform_updates = dict(item.platform_updates)
            # The merged task uses all accumulated attachments, so its eventual
            # Bot text reply must bind all of those same source images rather than
            # only the last queued message's list.
            existing.platform_updates["_source_attachments"] = source_attachments_from_merged(
                existing.attachments
            )
            existing.user_text = item.user_text
        else:
            state._qq_queue.append(item)
            state._qq_queue_by_key[item.stable_conversation_key] = item
        state._qq_queue_condition.notify()
    return True


async def _qq_queue_worker_forever() -> None:
    while True:
        async with state._qq_queue_condition:
            while not state._qq_queue:
                await state._qq_queue_condition.wait()
            item = state._qq_queue.popleft()
            state._qq_queue_by_key.pop(item.stable_conversation_key, None)

        reply_event: Optional[asyncio.Event] = None
        if QQ_QUEUE_WAIT_FOR_REPLY:
            reply_event = asyncio.Event()
            state._qq_waiting_replies[item.stable_conversation_key] = reply_event
        try:
            await _submit_or_preempt_inbound(
                bot=item.bot,
                event=item.event,
                channel=item.channel,
                real_conversation_key=item.real_conversation_key,
                stable_conversation_key=item.stable_conversation_key,
                inbound_text=item.text,
                user_text=item.user_text,
                attachments=item.attachments,
                is_dm=item.is_dm,
                platform_updates=item.platform_updates,
                entry_node_id=item.entry_node_id,
            )
            if reply_event is not None:
                try:
                    await asyncio.wait_for(reply_event.wait(), timeout=QQ_QUEUE_REPLY_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("QQ queue reply wait timed out conversation=%s timeout=%ss", item.real_conversation_key, QQ_QUEUE_REPLY_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to process QQ queue item conversation=%s", item.real_conversation_key)
        finally:
            if reply_event is not None:
                state._qq_waiting_replies.pop(item.stable_conversation_key, None)
        if QQ_QUEUE_INTERVAL > 0:
            await asyncio.sleep(QQ_QUEUE_INTERVAL)
