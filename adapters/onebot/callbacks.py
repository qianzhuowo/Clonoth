"""Clonoth SDK callbacks for QQ (TangQiuCallbacks) and approval reply handling."""
from __future__ import annotations

import logging
from .config import ADMIN_QQ_USERS
from .send_contract import OneBotSendContractError
from .send_contract import OutboundSendContext
from .send_contract import context_from_sources
from clonoth_sdk import ChildTaskState
from clonoth_sdk import MainTaskState
from clonoth_sdk import TriggerInfo
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Event
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import time

from . import approval, commands, message_parse, send, state, voice

logger = logging.getLogger("nonebot.plugin.clonoth_agent")

async def _resolve_approval_id_by_reply_fallback(bot: Bot, event: Event, reply_message_id: Any) -> Optional[str]:
    """仅从明确引用的本 bot 消息中恢复审批 ID；绝不猜测唯一待审批。"""
    key = approval.normalize_msg_id(reply_message_id)
    if not key or key == "0":
        return None
    try:
        reply_obj = await message_parse.get_reply_message(bot, reply_message_id)
    except Exception:
        return None
    if not isinstance(reply_obj, dict):
        return None
    sender = reply_obj.get("sender") or {}
    bot_id = str(getattr(bot, "self_id", "") or "")
    if not isinstance(sender, dict) or not bot_id or str(sender.get("user_id") or "") != bot_id:
        return None
    parts: List[str] = []
    for field in ("raw_message", "message"):
        value = reply_obj.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for segment in value:
                data = segment.get("data") if isinstance(segment, dict) else None
                if isinstance(data, dict) and isinstance(data.get("text"), str):
                    parts.append(data["text"])
    return state.BOOK.extract_from_text("\n".join(parts))


async def _handle_approval_raw_event(event: Any) -> None:
    if event.type == "approval_decided":
        approval_id = str((event.payload or {}).get("approval_id") or "")
        if approval_id:
            state.BOOK.forget(approval_id)


async def _approval_origin_line(bot: Any, origin: Optional[Dict[str, Any]], conversation_key: str) -> str:
    """来源行：按会话键反查真实群/私聊并补群名/人名。只发给管理员私聊，不进入模型上下文。"""
    info = dict(origin or {})
    key = str(info.get("conversation_key") or conversation_key or "")
    info.setdefault("conversation_key", key)
    target = state.ANON.target_from_conversation_key(key) if key else None
    if not target:
        return approval.origin_line_for(info)
    info["target_type"] = target["type"]
    requester = info.get("user_id")
    user_name = state.PROFILES.display_name(requester) if requester else ""
    if target["type"] == "group":
        group_id = target["group_id"]
        group_name = ""
        try:
            group = await bot.call_api("get_group_info", group_id=int(group_id))
            if isinstance(group, dict):
                group_name = str(group.get("group_remark") or group.get("group_name") or "")
        except Exception:
            logger.debug("get_group_info for approval origin failed: group=%s", group_id, exc_info=True)
        return approval.origin_line_for(info, group_name=group_name, group_id=group_id, user_name=user_name)
    user_id = target["user_id"]
    return approval.origin_line_for(info, user_name=state.PROFILES.display_name(user_id), user_id=user_id)


class TangQiuCallbacks:
    """Clonoth SDK 的 QQ 平台回调实现。

    发送最终回复、附件和主节点中间回复；其余进度、审批、typing、子任务日志均静默。

    2026-05-01 修改原因：QQ 端已开启 send_intermediate_reply，因此类说明需要
    同步说明中间回复会发送；审批流程仍由 Discord 端处理，避免 QQ 侧误展示审批控件。
    """

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        main_state: Optional[MainTaskState] = None,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送主节点最终回复。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or send._get_fallback_bot()
        target = send._target_from_platform_data(platform_data) or state.ANON.target_from_conversation_key(trigger.conversation_key)
        if not bot:
            raise OneBotSendContractError(
                f"send_reply missing bot for session={trigger.session_id}"
            )
        if not target:
            raise OneBotSendContractError(
                f"send_reply missing target for session={trigger.session_id}"
            )
        send_context = delivery_context or context_from_sources(
            trigger=trigger,
            main_state=main_state,
            platform_data=callback_data.get("platform_data"),
        )
        # 提取 [REACT:ID] 标记
        final_text = text or ""
        if final_text:
            from .emoji_handler import _extract_reactions
            final_text, reactions = _extract_reactions(final_text)
            if reactions:
                await self.add_reactions(trigger, reactions)
        source_attachments = platform_data.get("_source_attachments")
        if not isinstance(source_attachments, list):
            source_attachments = []
        await send._send_text_and_attachments(
            bot,
            target,
            final_text,
            attachments or [],
            source_attachments=source_attachments,
            send_context=send_context,
            input_modality=str(platform_data.get("_qq_input_modality") or "text"),
            conversation_key=trigger.conversation_key,
        )
        # 2026-05-03 修改原因：最终回复到达时，触发消息上可能仍残留生命周期 React。
        # 做法是调用统一清理函数移除 76、281、178、97、326，目的在于把本轮
        # 新增的推理和工具阶段也纳入最终收尾。
        event = platform_data.get("event")
        await send._clear_message_reacts(bot, event)

    async def send_reply_attachment(self, session_id: str, path: str, *args: Any, **kwargs: Any) -> None:
        """兼容旧式 session_id 附件回调；当前 SDK 通常把附件放在 send_reply 中。"""
        target = state._session_targets.get(session_id) or state._persisted_session_targets.get(session_id)
        if not target:
            raise OneBotSendContractError(
                f"send_reply_attachment missing target for session={session_id}"
            )
        bot = target.get("bot") or send._get_fallback_bot()
        if not bot:
            raise OneBotSendContractError(
                f"send_reply_attachment missing bot for session={session_id}"
            )
        await send._send_attachment_path(bot, target, Path(path))

    async def send_intermediate_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        *,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送主节点中间回复，但不写入群历史。"""
        if not text:
            return
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or send._get_fallback_bot()
        target = send._target_from_platform_data(platform_data) or state.ANON.target_from_conversation_key(trigger.conversation_key)
        if not bot:
            raise OneBotSendContractError(
                f"send_intermediate_reply missing bot for session={trigger.session_id}"
            )
        if not target:
            raise OneBotSendContractError(
                f"send_intermediate_reply missing target for session={trigger.session_id}"
            )
        send_context = delivery_context or context_from_sources(
            trigger=trigger,
            platform_data=callback_data.get("platform_data"),
        )
        # 2026-05-03 修改原因：中间回复回调表示 Clonoth 已经开始产出用户可见内容。
        # 做法是通过统一阶段切换进入 writing，目的在于从 281、178 或 97 中
        # 任一状态平滑切到 326，并继续让 React API 失败不影响中间文本发送。
        event = platform_data.get("event")
        await send._switch_react_stage(bot, event, platform_data, "writing")
        # 提取 [REACT:ID] 标记
        from .emoji_handler import _extract_reactions
        text, reactions = _extract_reactions(text)
        if reactions:
            await self.add_reactions(trigger, reactions)
        # 中间回复只发送显式 <tts> 段，不因语音输入自动整段合成。
        text, voice_segments = voice.voice_plan(text or "", target, "text")
        if voice_segments:
            await voice.send_voice_segments(bot, target, trigger.conversation_key, voice_segments)
        if text:
            source_attachments = platform_data.get("_source_attachments")
            if not isinstance(source_attachments, list):
                source_attachments = []
            if await send._send_split_text(
                bot,
                target,
                text,
                source_attachments=source_attachments,
                send_context=send_context,
            ):
                conv_key = target.get("conversation_key")
                if conv_key:
                    send._mark_qq_reply_finished(str(conv_key))

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        node_id: str = "",
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """处理没有 trigger 的 fallback 最终输出。"""
        target = state.ANON.target_from_conversation_key(conversation_key)
        if target is None:
            raise OneBotSendContractError(
                f"send_to_channel missing target for conversation={conversation_key}"
            )
        # [2026-07-19] 修复生图 op=recent 查不到图：_target_from_conversation_key
        # 只返回 {type, group_id/user_id}，缺 conversation_key，导致
        # _send_attachments_confirmed 的成功登记链路信息不全。
        # 这里补回 conversation_key（用真实会话 key），让附件登记 bucket 与
        # qq_forward op=recent 的查询 bucket 保持一致。
        if not target.get("conversation_key"):
            target["conversation_key"] = state.ANON.real_conversation_key(conversation_key)
        bot = state._conversation_bots.get(conversation_key) or send._get_fallback_bot()
        if not bot:
            raise OneBotSendContractError(
                f"send_to_channel missing bot for conversation={conversation_key}"
            )
        send_context = delivery_context or context_from_sources(
            platform_data=callback_data.get("platform_data"),
            conversation_key=conversation_key,
        )
        await send._send_text_and_attachments(
            bot,
            target,
            text or "",
            attachments or [],
            send_context=send_context,
        )

    async def delete_status_message(self, trigger: TriggerInfo) -> None:
        return None

    async def edit_status_message(self, trigger: TriggerInfo, content: str) -> None:
        return None

    async def update_progress(self, trigger: TriggerInfo, state: MainTaskState) -> None:
        """根据主任务进度切换触发消息上的 React 表情。"""
        # 2026-05-03 修改原因：EventRouter 不展示 QQ 进度文本，但 QQ 侧需要补齐
        # React 链中的 LLM 推理和工具阶段。做法是读取 MainTaskState.stream_parts
        # 与 progress_records：首次流式文本进入 thinking，真实工具执行进度进入 tool；
        # 目的在于复用 SDK sweep 回调并用阶段元数据避免高频 stream_delta 抖动。
        platform_data = trigger.platform_data
        event = platform_data.get("event")
        if not event or not hasattr(event, "message_id"):
            return
        bot = platform_data.get("bot") or send._get_fallback_bot()
        if not bot:
            return

        target = send._target_from_platform_data(platform_data) or state.ANON.target_from_conversation_key(trigger.conversation_key)
        has_tool_progress = any("执行" in record and "个工具" in record for record in state.progress_records)
        has_search_progress = any(send._progress_mentions_search(record) for record in state.progress_records)
        if has_tool_progress or has_search_progress:
            await send._switch_react_stage(bot, event, platform_data, "tool")
            # [2026-09-17] 只在搜索真正进行中（已开始执行、尚无结果行）才发可见提示；
            # 搜索结束后即使任务仍在运行（生成回复/投递中）也不再刷“还在搜索中”。
            if target and send._search_in_progress(state.progress_records):
                await send._maybe_send_search_progress_notice(bot, target, platform_data)
            return
        if state.stream_parts:
            await send._switch_react_stage(bot, event, platform_data, "thinking")

    async def on_delivery_dead_letter(
        self,
        trigger: Optional[TriggerInfo],
        conversation_key: str,
        error: BaseException,
        *,
        event_type: str = "",
        ambiguous_ack: bool = False,
        idempotency_key: str = "",
        task_id: str = "",
    ) -> None:
        """最终回复进死信：清理触发消息状态，并给用户一句简短提示。

        [2026-09-17] Why: QQ 侧投递失败（NapCat 超时/EventChecker 失败→ambiguous）
        以前完全静默，用户只看到一串“还在联网搜索中”然后没下文。How: SDK 已经
        消费了 trigger，这里只做平台侧收尾：清 React、释放等待回复的事件、发一条
        幂等的失败提示（同一次投递只提示一次）。中间回复死信不提示，因为任务
        还在跑、最终回复会再来。Purpose: 把“静默丢回复”变成可见、可重试的终态。
        """
        if event_type == "intermediate_reply":
            return
        platform_data: Dict[str, Any] = trigger.platform_data if trigger is not None else {}
        bot = platform_data.get("bot") or state._conversation_bots.get(conversation_key) or send._get_fallback_bot()
        target = None
        if platform_data:
            target = send._target_from_platform_data(platform_data)
        if target is None and conversation_key:
            target = state.ANON.target_from_conversation_key(conversation_key)
            if target is not None and not target.get("conversation_key"):
                target["conversation_key"] = state.ANON.real_conversation_key(conversation_key)
        conv_key = str((target or {}).get("conversation_key") or conversation_key or "")
        if conv_key:
            send._mark_qq_reply_finished(conv_key)
        event = platform_data.get("event")
        if bot and event is not None:
            try:
                await send._clear_message_reacts(bot, event)
            except Exception:
                logger.debug("clear reacts after dead letter failed", exc_info=True)
        if not bot or not target:
            logger.warning(
                "onebot_dead_letter_notice_skipped",
                extra={"conversation_key": conversation_key, "task_id": task_id, "reason": "missing bot/target"},
            )
            return
        notice_identity = idempotency_key or f"{conv_key}:{task_id}:{getattr(error, 'args', '')}"
        context = commands._request_send_context("dead-letter-notice", str(notice_identity), conv_key)
        try:
            await send._send_qq_message(
                bot, target, state._DELIVERY_DEAD_LETTER_NOTICE,
                send_context=context,
                content_identity=f"dead-letter:{notice_identity}",
            )
            logger.info(
                "onebot_dead_letter_notice_sent",
                extra={
                    "conversation_key": conv_key, "task_id": task_id,
                    "idempotency_key": idempotency_key, "ambiguous_ack": ambiguous_ack,
                },
            )
        except Exception:
            # 提示本身也发不出去（平台整体不可用）：只记日志，不再层层重试。
            logger.warning("onebot_dead_letter_notice_failed", exc_info=True)

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: Optional[TriggerInfo] = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        return None

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        return None

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        return None

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: Dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
        origin: Optional[Dict[str, Any]] = None,
    ) -> None:
        """QQ 端审批必须由管理员私聊确认；不再自动放行。"""
        if state._client is None:
            logger.warning(
                "approval review skipped on QQ: client unavailable id=%s operation=%s",
                approval_id,
                operation,
            )
            return

        state.BOOK.pending[approval_id] = {
            "operation": operation,
            "details": dict(details or {}),
            "conversation_key": conversation_key,
            "session_id": session_id,
            "created_at": time.time(),
        }

        if not ADMIN_QQ_USERS:
            try:
                await state._client.approve(
                    approval_id,
                    decision="deny",
                    comment="QQ adapter denied because CLONOTH_ADMIN_QQ_USERS is not configured.",
                )
            except Exception:
                logger.exception("approval deny failed on QQ: id=%s operation=%s", approval_id, operation)
            state.BOOK.pending.pop(approval_id, None)
            logger.warning("approval denied on QQ: no admin users configured id=%s operation=%s", approval_id, operation)
            return

        bot = send._get_fallback_bot()
        if not bot:
            logger.warning("approval review pending but no QQ bot available: id=%s operation=%s", approval_id, operation)
            return

        origin_line = await _approval_origin_line(bot, origin, conversation_key)
        summary = approval.approval_summary(approval_id, operation, details or {}, origin_line=origin_line)
        delivered = 0
        for admin_id in ADMIN_QQ_USERS:
            try:
                approval_context = commands._request_send_context(
                    "approval-admin", f"{approval_id}:{admin_id}", conversation_key,
                )
                sent_mid = await send._send_qq_message(
                    bot, {"type": "private", "user_id": int(admin_id)}, summary,
                    send_context=approval_context,
                )
                delivered += 1
                # 记录审批消息 id -> approval_id，使管理员可直接引用该消息回复“审批同意/拒绝”。
                state.BOOK.remember_message(sent_mid, approval_id)
            except Exception:
                logger.exception("send approval request to QQ admin failed: admin=%s id=%s", admin_id, approval_id)

        target = state.ANON.target_from_conversation_key(conversation_key)
        if target:
            try:
                await send._send_qq_message(
                    bot, target, "当前操作需要 Clonoth 管理员审批，已提交等待处理。",
                    send_context=commands._request_send_context(
                        "approval", approval_id, conversation_key,
                    ),
                )
            except Exception:
                logger.debug("send approval pending notice failed: id=%s", approval_id, exc_info=True)

        logger.info(
            "approval pending on QQ: id=%s operation=%s admins_notified=%s",
            approval_id,
            operation,
            delivered,
        )

    async def refresh_typing(self, trigger: TriggerInfo) -> None:
        return None

    async def add_reactions(self, trigger: TriggerInfo, reactions: List[str]) -> None:
        """在 QQ 消息上添加表情反应。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or send._get_fallback_bot()
        event = platform_data.get("event")
        if not bot or not event or not hasattr(event, "message_id"):
            return
        for emoji_id in reactions:
            eid = emoji_id.strip()
            if not eid:
                continue
            try:
                await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id=eid, set=True)
            except Exception:
                pass

    async def on_task_created(self, trigger: TriggerInfo, task_id: str) -> None:
        return None

    async def on_restart_signal(self, conversation_key: str) -> None:
        return None

    async def on_context_reset(self, conversation_key: str, reason: str, cleaned_triggers: List[TriggerInfo]) -> None:
        """上下文重置时同步清理 QQ 侧历史缓存。"""
        target = state.ANON.target_from_conversation_key(conversation_key)
        if target and target.get("type") == "group" and reason != "compact":
            group_id = target.get("group_id")
            if group_id is not None:
                state._group_history.pop(int(group_id), None)

    async def on_engine_restarted(self, payload: Dict[str, Any]) -> None:
        return None

    async def on_task_complete(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_fail(self, *args: Any, **kwargs: Any) -> None:
        return None
