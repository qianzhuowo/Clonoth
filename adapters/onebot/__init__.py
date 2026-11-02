"""Clonoth OneBot 11 (QQ) adapter entry: nonebot matchers and startup/shutdown."""
from __future__ import annotations

import sys

from .config import CLONOTH_WORKSPACE

if CLONOTH_WORKSPACE not in sys.path:
    sys.path.insert(0, CLONOTH_WORKSPACE)

from .attachment_policy import normalise_file_item
from .config import BQBS_PATH
from .config import CLONOTH_BASE_URL
from .config import CLONOTH_WORKSPACE
from .config import ENABLE_PREEMPT
from .config import ENABLE_QQ_QUEUE
from .config import ENABLE_REACTIONS
from .config import ENTRY_NODE_ID
from .config import QQ_QUEUE_INTERVAL
from .config import QQ_QUEUE_REPLY_TIMEOUT
from .config import QQ_QUEUE_WAIT_FOR_REPLY
from .config import QQ_QUEUE_WORKERS
from .emoji_handler import load_bqbs
from .emoji_handler import set_at_alias_resolver
from clonoth_sdk import BotConfig
from clonoth_sdk import ClonothClient
from clonoth_sdk import EventRouter
from clonoth_sdk import SessionState
from nonebot import get_driver
from nonebot import on_message
from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.onebot.v11 import GroupUploadNoticeEvent
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.rule import Rule
from pathlib import Path
import asyncio
import contextlib
import logging
import os
import time

from . import access, approval, attachment_policy, callbacks, commands, forward_bridge, history, inbound_attachments, message_parse, routing, send, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


driver = get_driver()


@driver.on_startup
async def _startup() -> None:
    """NoneBot 启动时初始化 Clonoth SDK 与事件路由。"""
    if state._router_task is not None and not state._router_task.done():
        return

    state.FACES.bqbs = load_bqbs(BQBS_PATH) if BQBS_PATH else []
    state.FACES.current_metadata()
    state.PROFILES.reload()
    if state.PROFILES.profiles:
        logger.info("loaded %d QQ user profiles", len(state.PROFILES.profiles))
    routing._load_route_state()
    state.REPLY_CACHE.load()
    state.ANON.load()
    # [2026-07-14] 注入 at 别名反查，让 emoji_handler 在处理 [at:UserAF]/[at:显示名]
    # 时能把匿名别名/群昵称回解为真实 QQ 号，避免直接把代号当纯文本 @ 出去。
    set_at_alias_resolver(lambda token: state.ANON.resolve_at_alias_to_real(token, state.PROFILES.name_index))
    # [AutoC] QQ 管理员 /切换模型 命令需要调用受 admin_token 保护的
    # POST /v1/config/openai，因此这里把 Supervisor 写出的 data/.admin_token
    # 传给 ClonothClient（每次请求实时读取，token 轮换也能跟上）。
    state._client = ClonothClient(
        CLONOTH_BASE_URL,
        admin_token=os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip(),
        admin_token_path=str(Path(CLONOTH_WORKSPACE) / "data" / ".admin_token"),
    )
    state._session_state = SessionState()
    for sid, target in list(state._persisted_session_targets.items()):
        conv_key = str(target.get("conversation_key") or "")
        if conv_key:
            state._session_state.register_session(conv_key, sid)
    state._callbacks = callbacks.TangQiuCallbacks()

    bot_config = BotConfig(
        base_url=CLONOTH_BASE_URL,
        entry_node_id=ENTRY_NODE_ID,
        conversation_key_prefix="qq_group",
        # [AutoC] QQ 私聊会话键前缀为 qq_private，需一并声明归属，
        # 否则私聊触发的 approval_requested 会因前缀不匹配被 SDK 丢弃，
        # 导致管理员收不到审批、任务空等到超时。
        extra_conversation_key_prefixes=["qq_private"],
        workspace_root=Path(CLONOTH_WORKSPACE),
        # QQ 侧不再自动审批内部操作；所有 approval_requested 都必须由
        # CLONOTH_ADMIN_QQ_USERS 中的管理员私聊明确同意后才会放行。
        auto_approve_internal=False,
    )
    state._event_router = EventRouter(
        state._client,
        state._session_state,
        state._callbacks,
        bot_config,
        entry_node_id=ENTRY_NODE_ID,
        poll_interval=1.0,
    )
    state._event_router.set_raw_event_hook(callbacks._handle_approval_raw_event)
    state._router_task = asyncio.create_task(state._event_router.run())
    if ENABLE_QQ_QUEUE and not any(not task.done() for task in state._qq_queue_tasks):
        state._qq_queue_tasks = [asyncio.create_task(routing._qq_queue_worker_forever()) for _ in range(QQ_QUEUE_WORKERS)]
        logger.info(
            "QQ queue enabled: workers=%s interval=%ss wait_for_reply=%s reply_timeout=%ss preempt=%s",
            QQ_QUEUE_WORKERS,
            QQ_QUEUE_INTERVAL,
            QQ_QUEUE_WAIT_FOR_REPLY,
            QQ_QUEUE_REPLY_TIMEOUT,
            ENABLE_PREEMPT,
        )
    with contextlib.suppress(Exception):
        await forward_bridge._start_forward_bridge()
    logger.info("Clonoth Agent QQ adapter started: %s", CLONOTH_BASE_URL)


@driver.on_shutdown
async def _shutdown() -> None:
    """NoneBot 关闭时停止事件路由并释放 HTTP 连接。"""
    if state._event_router is not None:
        state._event_router.stop()
    if state._router_task is not None:
        state._router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._router_task
        state._router_task = None
    if state._qq_queue_tasks:
        for task in state._qq_queue_tasks:
            task.cancel()
        for task in state._qq_queue_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state._qq_queue_tasks = []
    if state._client is not None:
        await state._client.close()
        state._client = None
    state._event_router = None
    state._callbacks = None
    await forward_bridge._stop_forward_bridge()
    # 取消节流中的延迟写盘并立即落盘一次，避免最新匿名别名丢失。
    with contextlib.suppress(Exception):
        await state.ANON.flush()
    logger.info("Clonoth Agent QQ adapter stopped")


# 普通群消息记录器。priority 较低且 block=False，只负责维护上下文缓存。
_history_matcher = on_message(rule=Rule(routing._allowed_group_rule), priority=99, block=False)


@_history_matcher.handle()
async def _handle_history(bot: Bot, event: GroupMessageEvent) -> None:
    """记录非触发消息，供后续 @Bot 请求作为群聊上下文。"""
    state.REPLY_MSGS.remember(event)
    if str(event.user_id) != str(getattr(bot, "self_id", "")):
        expanded_text = await state.forward_expander(bot).to_text(event.get_message(), getattr(bot, "self_id", None))
        # QQ 客户端经常把“问图文本 + 图片”拆成两条事件；第二条纯图片
        # 通常不会 @bot，因此不会进入 agent matcher。这里在低优先级历史
        # matcher 中把非触发附件也下载并写入近期缓存，让后续自然语言转发
        # 和图片提问能选到对应内容。
        real_conversation_key = f"qq_group:{int(event.group_id)}"
        stable_conversation_key = state.ANON.stable_conversation_key(real_conversation_key)
        attachments, _errors = await inbound_attachments.collect_message_attachments(bot, event, stable_conversation_key)
        state.RECENT.remember(stable_conversation_key, event, attachments)
        history._record_group_message(event, bot, override_text=expanded_text, attachments=attachments)


# 群文件上传通知记录器。部分 OneBot 实现把普通文件作为 notice 上报，而不是 message file 段。
_group_upload_matcher = on_notice(priority=99, block=False)


@_group_upload_matcher.handle()
async def _handle_group_upload_notice(bot: Bot, event: Event) -> None:
    if not isinstance(event, GroupUploadNoticeEvent):
        return
    if not access.is_group_allowed(int(event.group_id)):
        return
    state.REPLY_MSGS.remember(event)
    real_conversation_key = f"qq_group:{int(event.group_id)}"
    stable_conversation_key = state.ANON.stable_conversation_key(real_conversation_key)
    file_info = getattr(event, "file", None)
    if not isinstance(file_info, dict):
        file_info = {}
    file_item = normalise_file_item(file_info) or {"source": "", "name": "文件", "size": 0, "file_id": ""}
    name = inbound_attachments.safe_attachment_name(str(file_item.get("name") or "文件"), "file")
    file_item["name"] = name
    attachments, _errors = await inbound_attachments.file_sources_to_attachments(
        [file_item], stable_conversation_key, bot=bot, event=event,
    )
    display_text = f"[文件:{name}]"
    sender_id = str(getattr(event, "user_id", "") or "")
    name_raw = state.PROFILES.sender_display_name(getattr(event, "sender", None), sender_id)
    line = f"[{message_parse.format_hhmm(getattr(event, 'time', None))}] {state.ANON.anonymize_text(name_raw)}({state.ANON.anonymize_user_id(sender_id)}): {display_text}"
    state._group_history[int(event.group_id)].append(line)
    state._group_content_records[int(event.group_id)].append(state.GroupContentRecord(
        formatted_line=line,
        text=display_text,
        sender_name=name_raw,
        sender_id=sender_id,
        timestamp=float(getattr(event, "time", None) or time.time()),
        message_id=str(getattr(event, "message_id", "") or ""),
        attachments=attachments,
    ))


# Agent 入口 matcher。默认只处理 @Bot；也可通过 ONEBOT_GROUP_TRIGGER=prefix/all 切换触发策略。
_agent_matcher = on_message(rule=Rule(routing._agent_group_rule), priority=10, block=True)


@_agent_matcher.handle()
async def _handle_agent(bot: Bot, event: GroupMessageEvent) -> None:
    """把当前 QQ 群请求提交给 ClonothZX。"""
    state._last_bot = bot

    if state._client is None or state._session_state is None:
        await _agent_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    state.REPLY_MSGS.remember(event)
    group_id = int(event.group_id)
    real_conversation_key = f"qq_group:{group_id}"
    stable_conversation_key = state.ANON.stable_conversation_key(real_conversation_key)
    user_text = (await state.forward_expander(bot).to_text(event.get_message(), getattr(bot, "self_id", None))).strip() or "你好"
    user_text = routing._strip_trigger_prefix(user_text)
    asyncio.create_task(routing._auto_like_user(bot, int(event.user_id)))

    attachments, attachment_errors = await inbound_attachments.collect_message_attachments(bot, event, stable_conversation_key)
    state.RECENT.remember(stable_conversation_key, event, attachments)
    clear_mem_reply = await commands._maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _agent_matcher.finish(clear_mem_reply)
    model_reply = await commands._maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _agent_matcher.finish(model_reply)
    drawtools_reply = await commands._maybe_handle_drawtools_command(user_text)
    if drawtools_reply is not None:
        await _agent_matcher.finish(drawtools_reply)
    custom_face_reply = await state.FACES.handle_command(
        bot=bot,
        event=event,
        text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
        recent=state.RECENT,
        is_admin=access.is_admin_user(event.user_id),
    )
    if custom_face_reply is not None:
        await _agent_matcher.finish(custom_face_reply)
    proactive_reply = await commands._maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _agent_matcher.finish(proactive_reply)
    await state.RECENT.merge_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    history._record_group_message(event, bot, override_text=user_text, attachments=attachments)
    draw_direct_prompt = routing._parse_direct_draw_command(user_text)
    entry_node_id = state.DRAW_NODE_ID if draw_direct_prompt is not None else ""
    inbound_text = await routing._build_draw_direct_inbound_text(event, draw_direct_prompt, False) if draw_direct_prompt is not None else await routing._build_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
    if attachment_policy.looks_like_image_query(user_text) and not attachments:
        attachment_errors.append(
            "当前消息或引用链未能绑定到可确认的图片；不得从聊天历史猜测图片内容，请让用户重新发送或直接引用原图。"
        )
    if attachment_errors:
        inbound_text += "\n\n【图片处理提示】\n" + "\n".join(dict.fromkeys(attachment_errors))
    if attachments:
        for att in attachments:
            inbound_text = inbound_text.replace("[图片]", f"[图片: {att['path']}]", 1)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "group",
        "group_id": group_id,
        "conversation_key": stable_conversation_key,
        "_source_attachments": [dict(att) for att in attachments if isinstance(att, dict)],
    }
    try:
        ok = await routing._enqueue_or_submit_inbound(state.QueuedInbound(
            matcher=_agent_matcher,
            bot=bot,
            event=event,
            channel="qq_group",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=False,
            platform_updates=platform_updates,
            user_text=user_text,
            entry_node_id=entry_node_id,
        ))
        if ENABLE_REACTIONS:
            await send._set_message_react(bot, event, "281", True)
    except Exception as exc:
        logger.exception("submit inbound failed")
        await _agent_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _agent_matcher.finish("Clonoth Agent 未接受本次请求。")
    await _agent_matcher.finish()


# 私聊入口 matcher。私聊不需要 @Bot，直接阻断后续 matcher，避免重复响应。
_private_matcher = on_message(rule=Rule(routing._private_message_rule), priority=10, block=True)


async def _finish_approval_decision(user_id: int, approval_id: str, decision: str) -> None:
    """提交审批决策并结束当前私聊处理（末尾总会抛出 FinishedException）。

    同时服务于“引用回复审批”和“手输审批命令”两个入口，避免重复代码。
    """
    info = dict(state.BOOK.pending.get(approval_id, {}))
    try:
        ok = await state._client.approve(
            approval_id,
            decision=decision,
            comment=f"QQ admin {user_id} {decision}ed via private message.",
        )
    except Exception as exc:
        logger.exception("submit QQ approval decision failed")
        await _private_matcher.finish(f"提交审批失败：{exc}")
    if ok:
        state.BOOK.forget(approval_id)
    operation = str(info.get("operation") or "unknown")
    if ok:
        await _private_matcher.finish(
            f"已{('同意' if decision == 'allow' else '拒绝')}审批：{approval_id}\n操作：{operation}"
        )
    await _private_matcher.finish("审批未生效：请求可能已超时、已处理或被拒绝；未确认批准成功。")


@_private_matcher.handle()
async def _handle_private_agent(bot: Bot, event: PrivateMessageEvent) -> None:
    """把当前 QQ 私聊请求提交给 ClonothZX。"""
    state._last_bot = bot

    if state._client is None or state._session_state is None:
        await _private_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    state.REPLY_MSGS.remember(event)
    user_id = int(event.user_id)
    user_text = (await state.forward_expander(bot).to_text(event.get_message(), getattr(bot, "self_id", None))).strip() or "你好"

    # 快捷审批：管理员引用(回复)审批消息并发“同意/拒绝”即可，无需携带 approval_id。
    reply_mid = message_parse.extract_reply_message_id(event.get_message(), getattr(event, "raw_message", None))
    reply_approval_id = state.BOOK.resolve_by_reply(reply_mid) if reply_mid is not None else None
    # 无有效引用时不得进入快捷审批；普通聊天绝不回退到某条待审批。
    reply_verb = approval.parse_approval_reply_verb(user_text)
    if (
        reply_mid is not None
        and reply_approval_id is None
        and reply_verb is not None
        and access.is_admin_user(user_id)
    ):
        reply_approval_id = await callbacks._resolve_approval_id_by_reply_fallback(bot, event, reply_mid)
    if reply_approval_id is not None:
        if reply_verb is not None:
            if not access.is_admin_user(user_id):
                await _private_matcher.finish("你不是 Clonoth 审批管理员，不能处理审批请求。")
            await _finish_approval_decision(user_id, reply_approval_id, reply_verb)

    approval_command = approval.parse_approval_command(user_text)
    if approval_command is not None:
        if not access.is_admin_user(user_id):
            await _private_matcher.finish("你不是 Clonoth 审批管理员，不能处理审批请求。")
        decision, approval_token = approval_command
        approval_id, error = state.BOOK.resolve_token(approval_token)
        if not approval_id:
            await _private_matcher.finish(error)
        await _finish_approval_decision(user_id, approval_id, decision)

    # 管理员私聊清除群记忆：无参数列出可清理群，带参数按群名/群号清除。
    # 放在 _is_private_allowed 之前，使管理员即便私聊未整体放通也能使用该命令。
    clear_mem_reply = await commands._maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _private_matcher.finish(clear_mem_reply)
    model_reply = await commands._maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _private_matcher.finish(model_reply)

    if not access.is_private_allowed(event.user_id, getattr(event, "sub_type", "")):
        await _private_matcher.finish("当前 QQ 私聊未被允许接入 Clonoth。")

    real_conversation_key = f"qq_private:{user_id}"
    stable_conversation_key = state.ANON.stable_conversation_key(real_conversation_key)
    attachments, attachment_errors = await inbound_attachments.collect_message_attachments(bot, event, stable_conversation_key)
    state.RECENT.remember(stable_conversation_key, event, attachments)
    drawtools_reply = await commands._maybe_handle_drawtools_command(user_text)
    if drawtools_reply is not None:
        await _private_matcher.finish(drawtools_reply)
    custom_face_reply = await state.FACES.handle_command(
        bot=bot,
        event=event,
        text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
        recent=state.RECENT,
        is_admin=access.is_admin_user(event.user_id),
    )
    if custom_face_reply is not None:
        await _private_matcher.finish(custom_face_reply)
    proactive_reply = await commands._maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _private_matcher.finish(proactive_reply)
    await state.RECENT.merge_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    draw_direct_prompt = routing._parse_direct_draw_command(user_text)
    entry_node_id = state.DRAW_NODE_ID if draw_direct_prompt is not None else ""
    inbound_text = await routing._build_draw_direct_inbound_text(event, draw_direct_prompt, True) if draw_direct_prompt is not None else await routing._build_private_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
    if attachment_policy.looks_like_image_query(user_text) and not attachments:
        attachment_errors.append(
            "当前消息或引用链未能绑定到可确认的图片；不得从聊天历史猜测图片内容，请让用户重新发送或直接引用原图。"
        )
    if attachment_errors:
        inbound_text += "\n\n【图片处理提示】\n" + "\n".join(dict.fromkeys(attachment_errors))
    if attachments:
        for att in attachments:
            inbound_text = inbound_text.replace("[图片]", f"[图片: {att['path']}]", 1)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "private",
        "user_id": user_id,
        "conversation_key": stable_conversation_key,
        "_source_attachments": [dict(att) for att in attachments if isinstance(att, dict)],
    }
    try:
        ok = await routing._enqueue_or_submit_inbound(state.QueuedInbound(
            matcher=_private_matcher,
            bot=bot,
            event=event,
            channel="qq_private",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=True,
            platform_updates=platform_updates,
            user_text=user_text,
            entry_node_id=entry_node_id,
        ))
        try:
            await bot.call_api("set_input_status", user_id=int(event.user_id), event_type=1)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("submit private inbound failed")
        await _private_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _private_matcher.finish("Clonoth Agent 未接受本次请求。")
    await _private_matcher.finish()
