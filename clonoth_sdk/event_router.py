"""EventRouter — SDK 事件轮询主循环与协议路由。

Phase 3 step 2 (2026-04-17): 初始创建。

替代 bot_adapter.py _outbound_poller() (L1206-2040)，将 834 行协议逻辑
提取到 SDK。EventRouter 轮询 Supervisor 事件流，执行协议状态管理，
然后通过 AdapterCallbacks 通知适配器执行平台操作。

核心设计约束（参见 data/sdk_refactor_plan_final.md）：
  1. 展示层逻辑不进 SDK：dot_state、点阵动画、节流(should_edit/mark_edited)、
     thinking_preview 等全部不处理。适配器通过 on_raw_event hook 拿到
     stream_delta 自行管理。
  2. 协议标记清理进 SDK：[CLONOTH_TOOL_TRACE v...]...[/CLONOTH_TOOL_TRACE]
     由 strip_protocol_markers() 统一处理。
  3. Bot 自定义标记不碰：[SPLIT]、[REACT:...]、[BOT_RESTART] 留在 text 中
     不处理，由适配器的 callback 实现自行解析。
  4. sweep 阶段：SDK 只做 typing refresh 通知和状态传递
     （调用 update_progress / update_child_progress），不做节流判断。
     适配器在 callback 实现中自行决定是否 edit。
  5. 审批：SDK 做全局去重(ApprovalTracker) + per-trigger 去重
     (MainTaskState.handled_approvals) + 内外分类 + 自动放行。
     外部操作通过 show_approval_ui 回调让适配器展示审批 UI。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

from .approval import ApprovalTracker, auto_approve, is_external_operation
from .callbacks import AdapterCallbacks
from .client import ClonothClient
from .config import BotConfig
from .state import ChildTaskState, MainTaskState, SessionState, TriggerInfo
from .types import Event

logger = logging.getLogger("clonoth_sdk.event_router")

# --------------------------------------------------------------------------
#  协议标记清理
# --------------------------------------------------------------------------

# 匹配 SDK 内部协议标记 [CLONOTH_TOOL_TRACE v...]...[/CLONOTH_TOOL_TRACE]
# 这些标记用于 engine 内部调试，不应暴露给 Bot 用户
_TOOL_TRACE_RE = re.compile(
    r"\[CLONOTH_TOOL_TRACE v\d+\].*?\[/CLONOTH_TOOL_TRACE\]",
    re.DOTALL,
)


def strip_protocol_markers(text: str) -> str:
    """移除 SDK 协议标记。Bot 自定义标记（[SPLIT]、[REACT] 等）不受影响。

    提取自 bot_adapter.py 中散落的 TOOL_TRACE 清理逻辑，
    整合为单一入口，由 EventRouter 在所有文本输出点统一调用。
    """
    if not text:
        return text
    return _TOOL_TRACE_RE.sub("", text).strip()


# ==========================================================================
#  EventRouter
# ==========================================================================


class EventRouter:
    """事件轮询主循环 + 协议状态管理 + 适配器分发。

    替代 bot_adapter.py _outbound_poller()。
    轮询 Supervisor 事件流，处理协议状态（trigger 匹配、session 映射、
    watermark 推进、审批去重），然后通过 AdapterCallbacks 通知适配器
    执行平台操作（发送消息、刷新 typing、编辑进度日志等）。

    双层钩子架构：
      Layer 1 — on_raw_event hook：适配器注册的原始事件拦截器，
               在 SDK 默认处理之前调用。返回 'handled' 跳过默认处理。
      Layer 2 — AdapterCallbacks：SDK 完成协议处理后的通知回调，
               适配器实现平台操作。

    用法::

        router = EventRouter(client, state, callbacks, config)
        # 可选：注册 Layer 1 钩子
        router.set_raw_event_hook(my_hook)
        # 启动事件循环（阻塞，直到被取消或调用 stop()）
        await router.run()
    """

    # 事件类型过滤列表，传给 Supervisor GET /v1/events 的 types 参数。
    # 与 bot_adapter.py _outbound_poller() 中的过滤列表一致。
    # 2026-04-17: 追加 compact_start/compact_done/compact_failed，
    # 使 _handle_compact 处理器能接收到这些事件。
    _EVENT_TYPES = (
        "inbound_message,outbound_message,intermediate_reply,"
        "handoff_progress,stream_delta,approval_requested,"
        "task_created,task_completed,task_cancelled,"
        "node_started,node_completed,cancel_requested,"
        "context_reset,inbound_accepted,task_preempted,"
        "compact_start,compact_done,compact_failed,snip_compact,"
    "engine_registered"
    )

    def __init__(
        self,
        client: ClonothClient,
        state: SessionState,
        callbacks: AdapterCallbacks,
        config: BotConfig,
        *,
        approval_tracker: ApprovalTracker | None = None,
        entry_node_id: str = "",
        poll_interval: float = 3.0,
    ):
        """
        Args:
            client: Supervisor HTTP API 客户端。
            state: 集中状态管理器。
            callbacks: 适配器回调接口实现。
            config: Bot 配置。
            approval_tracker: 审批去重追踪器（可选，默认新建）。
            entry_node_id: 入口节点 ID（可选，默认取 config.entry_node_id）。
            poll_interval: 轮询间隔秒数（默认 3.0）。
        """
        self._client = client
        self._state = state
        self._cb = callbacks
        self._config = config
        self._approval = approval_tracker or ApprovalTracker()
        self._entry_node_id = entry_node_id or config.entry_node_id
        self._poll_interval = poll_interval
        self._after_seq = 0
        self._caught_up = False
        self._running = False

        # Layer 1 hook: 原始事件拦截器
        # 返回 None → SDK 继续默认处理；返回 'handled' → SDK 跳过此事件
        self._on_raw_event: Callable[[Event], Awaitable[str | None]] | None = None

    # ------------------------------------------------------------------
    #  公共接口
    # ------------------------------------------------------------------

    def set_raw_event_hook(
        self,
        hook: Callable[[Event], Awaitable[str | None]] | None,
    ) -> None:
        """注册 Layer 1 原始事件拦截器。

        钩子在 SDK 默认协议处理之前调用。
        返回 None → SDK 继续默认处理。
        返回 'handled' → SDK 跳过此事件。
        传入 None 可注销钩子。
        """
        self._on_raw_event = hook

    @property
    def after_seq(self) -> int:
        """当前事件流游标（最近处理的 seq）。"""
        return self._after_seq

    async def run(self) -> None:
        """事件轮询主循环。阻塞运行，直到被取消或调用 stop()。"""
        self._running = True
        await self._init_seq()
        logger.info("EventRouter started, after_seq=%d", self._after_seq)

        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                events = await self._client.poll_events(
                    after_seq=self._after_seq,
                    types=self._EVENT_TYPES,
                )

                # 首批事件快进：如果 _init_seq 失败（_caught_up=False），
                # 跳过首批事件直接推进游标到最新，避免处理历史积压。
                if not self._caught_up and events:
                    self._after_seq = max(e.seq for e in events)
                    self._caught_up = True
                    logger.info("Fast-forward to seq=%d", self._after_seq)
                    continue

                # 超时 trigger 清理（10 分钟阈值）
                stale = self._state.cleanup_stale_triggers(timeout=600.0)
                for t in stale:
                    try:
                        await self._cb.delete_status_message(t)
                    except Exception:
                        pass

                # 逐事件分发
                for event in events:
                    self._after_seq = max(self._after_seq, event.seq)
                    await self._dispatch(event)

                # Sweep 阶段：遍历所有活跃 trigger，
                # 刷新 typing 并通知适配器更新主任务进度显示
                for seq, trigger in list(self._state.triggers.items()):
                    try:
                        await self._cb.refresh_typing(trigger)
                    except Exception:
                        pass

                    main_state = self._state.get_main_state(seq)
                    if main_state:
                        try:
                            await self._cb.update_progress(trigger, main_state)
                        except Exception:
                            pass

                # Sweep 阶段：遍历所有活跃子任务，通知适配器刷新显示
                for task_key, child_state in list(self._state.child_task_states.items()):
                    try:
                        await self._cb.update_child_progress(task_key, child_state)
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll error: %s", e, exc_info=True)
                await asyncio.sleep(5)

        logger.info("EventRouter stopped")

    def stop(self) -> None:
        """通知主循环停止。下一轮 poll 结束后退出。"""
        self._running = False

    # ------------------------------------------------------------------
    #  初始化与分发
    # ------------------------------------------------------------------

    async def _init_seq(self) -> None:
        """启动时初始化事件流游标。

        用 limit=5000 大步翻页追到事件流末尾，使后续轮询只处理新事件。
        不对历史事件做任何 dispatch，仅记录最新 seq。
        启动时连接可能暂不可用，最多重试 10 次（每次间隔 2 秒）。
        """
        for attempt in range(10):
            try:
                seq = 0
                while True:
                    events = await self._client.poll_events(after_seq=seq, limit=5000)
                    if not events:
                        break
                    seq = max(e.seq for e in events)
                self._after_seq = seq
                self._caught_up = True
                logger.info("_init_seq: caught up to seq=%d", self._after_seq)
                return
            except Exception as e:
                logger.warning("_init_seq attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2)
        logger.warning("_init_seq: all attempts failed, will fast-forward from seq=0")

    async def _dispatch(self, event: Event) -> None:
        """将单个事件路由到 Layer 1 钩子，然后到协议处理器。"""
        # Layer 1: 原始事件拦截
        if self._on_raw_event:
            try:
                result = await self._on_raw_event(event)
                if result == "handled":
                    return
            except Exception as e:
                logger.error("on_raw_event hook error: %s", e)

        # Layer 2: 协议分发
        handler = self._HANDLERS.get(event.type)
        if handler:
            try:
                await handler(self, event)
            except Exception as e:
                logger.error(
                    "Handler error for %s: %s", event.type, e, exc_info=True,
                )

    # ==================================================================
    #  事件处理器
    #  每个方法处理一种或一组事件类型，注册在类末尾的 _HANDLERS dict 中。
    # ==================================================================

    # ------------------------------------------------------------------
    #  inbound_message — 注册 conversation_key ↔ session_id 映射
    # ------------------------------------------------------------------

    async def _handle_inbound_message(self, event: Event) -> None:
        """处理 inbound_message 事件：维护会话双向映射。

        对应 bot_adapter.py L1280-1283 中 inbound_message 事件的
        session 映射注册。
        """
        conv_key = event.payload.get("conversation_key", "")
        if conv_key and event.session_id:
            self._state.register_session(conv_key, event.session_id)

    # ------------------------------------------------------------------
    #  task_created — 回填 task_id 到 trigger
    # ------------------------------------------------------------------

    async def _handle_task_created(self, event: Event) -> None:
        """处理 task_created 事件：将 task_id 回填到 trigger。

        仅处理根任务（无 caller_task_id）且有 source_inbound_seq 匹配的情况。
        回填后适配器可通过 trigger.task_id 精准取消单个任务。
        对应 bot_adapter.py L1284-1295。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        task_id = p.get("task_id", "")
        caller = p.get("caller_task_id") or ""
        # 仅根任务（无 caller）且有 src_seq 时回填
        if src_seq and task_id and not caller:
            trigger = self._state.get_trigger(src_seq)
            if trigger:
                trigger.task_id = task_id
                try:
                    await self._cb.on_task_created(trigger, task_id)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    #  outbound_message — 主节点回复 / fallback 发送
    # ------------------------------------------------------------------

    async def _handle_outbound_message(self, event: Event) -> None:
        """处理 outbound_message 事件。

        两条路径：
          Path 1（主节点回复）：src_seq 命中 trigger → 消费 trigger，
              移除 MainTaskState，清理协议标记，调用 send_reply。
              对应 bot_adapter.py L1297-1373。
          Path 2（Fallback）：无 trigger 匹配 → 过滤 system.* 节点，
              通过 session_conv_map 解析 conv_key，调用 send_to_channel。
              对应 bot_adapter.py L1375-1434。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)

        # Path 1: 主节点回复（src_seq 在 triggers 中）
        if src_seq and src_seq in self._state.triggers:
            trigger = self._state.consume_trigger(src_seq)
            main_state = self._state.remove_main_state(src_seq)
            text = strip_protocol_markers((p.get("text") or "").strip())
            attachments = p.get("attachments") or []
            try:
                await self._cb.send_reply(
                    trigger, text, attachments, main_state=main_state,
                )
            except Exception as e:
                logger.error("send_reply failed: %s", e)
            return

        # Path 2: Fallback（子节点 / 调度任务输出）
        node_id = p.get("node_id", "") or ""
        # 过滤系统内部节点，不向平台发送
        if node_id.startswith("system."):
            logger.debug("Skip system node outbound: %s", node_id)
            return

        conv_key = self._state.get_conversation_key(event.session_id) or ""
        text = strip_protocol_markers((p.get("text") or "").strip())
        attachments = p.get("attachments") or []
        if not text and not attachments:
            return

        try:
            await self._cb.send_to_channel(
                conv_key, text, attachments, node_id=node_id,
            )
        except Exception as e:
            logger.error("send_to_channel failed: %s", e)

    # ------------------------------------------------------------------
    #  intermediate_reply — 中间回复（入口节点 / 子节点）
    # ------------------------------------------------------------------

    async def _handle_intermediate_reply(self, event: Event) -> None:
        """处理 intermediate_reply 事件。

        两条路径：
          Path 1（入口节点中间回复）：src_seq 命中 trigger 且为入口节点 →
              刷新 trigger、清空 stream buffer、调用 send_intermediate_reply。
              对应 bot_adapter.py L1435-1465。
          Path 2（子节点/Fallback）：非入口节点 → 更新子任务日志，
              通过 session_conv_map fallback 调用 send_to_channel。
              对应 bot_adapter.py L1467-1520。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        node_id = p.get("node_id", "")

        # Path 1: 入口节点中间回复
        if (
            src_seq
            and src_seq in self._state.triggers
            and node_id == self._entry_node_id
        ):
            trigger = self._state.get_trigger(src_seq)
            trigger.refresh()
            text = strip_protocol_markers((p.get("text") or "").strip())
            # 清空流式 buffer（中间回复已包含完整内容）
            main_state = self._state.get_main_state(src_seq)
            if main_state:
                main_state.stream_parts.clear()
            if text:
                try:
                    await self._cb.send_intermediate_reply(trigger, text)
                except Exception as e:
                    logger.error("send_intermediate_reply failed: %s", e)
            return

        # 过滤系统内部节点
        if (node_id or "").startswith("system."):
            return

        # 更新子任务日志（如存在）
        task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
        child_state = self._state.get_child_state(task_key)
        if child_state:
            child_state.lines.append("↳ 已发送中间回复")
            try:
                await self._cb.update_child_progress(task_key, child_state)
            except Exception:
                pass

        # Fallback: 通过 session_conv_map 发送到频道
        conv_key = self._state.get_conversation_key(event.session_id) or ""
        text = strip_protocol_markers((p.get("text") or "").strip())
        if text:
            try:
                await self._cb.send_to_channel(
                    conv_key, text, [], node_id=node_id,
                )
            except Exception as e:
                logger.error(
                    "intermediate_reply send_to_channel failed: %s", e,
                )

    # ------------------------------------------------------------------
    #  node_started / node_completed — 节点生命周期
    # ------------------------------------------------------------------

    async def _handle_node_event(self, event: Event) -> None:
        """处理 node_started 和 node_completed 事件。

        入口节点 → 更新 MainTaskState 进度记录 + 通知适配器。
        非入口节点 → 更新 ChildTaskState 日志行 + 通知适配器。
        对应 bot_adapter.py L1539-1587。
        """
        p = event.payload
        node_id = p.get("node_id", "")
        node_name = p.get("node_name", "") or node_id
        src_seq = int(p.get("source_inbound_seq") or 0)
        label = "▶ 节点启动" if event.type == "node_started" else "✓ 节点完成"
        msg = f"{label}: {node_name}"

        # 隔离系统任务：system.* 节点严禁回退到主触发，必须作为独立子任务日志处理
        is_system = (node_id or "").startswith("system.")

        # [Fork/Merge 2026-05-12] 查找关联 trigger 时只使用 source_inbound_seq。
        # Why: entry branches share the parent session, and session fallback can attach a node event
        # to another active inbound. How: resolve_trigger now ignores session_id while preserving the
        # call signature. Purpose: keep progress records scoped to the exact triggering message.
        result = None
        if not is_system:
            result = self._state.resolve_trigger(src_seq, event.session_id)

        # 入口节点 → 主任务进度
        if result and node_id == self._entry_node_id:
            trigger_seq, trigger = result
            trigger.refresh()
            main_state = self._state.get_or_create_main_state(trigger_seq)
            main_state.progress_records.append(msg)
            try:
                await self._cb.update_progress(trigger, main_state)
            except Exception:
                pass
        # 非入口节点 → 子任务日志
        elif node_id and node_id != self._entry_node_id:
            task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
            # 自动创建子任务状态：确保 ▶ 节点启动 等生命周期事件不丢失
            child_state, is_new = self._state.get_or_create_child_state(
                task_key, prefix=node_id,
            )
            child_state.lines.append(msg)

            if is_new:
                # 新创建：通知适配器创建消息
                conv_key = self._state.get_conversation_key(event.session_id) or ""
                try:
                    await self._cb.create_child_progress(
                        task_key, child_state,
                        trigger=None,  # 系统任务或孤儿任务无关联 trigger
                        conversation_key=conv_key,
                        session_id=event.session_id,
                    )
                except Exception:
                    pass
            else:
                # 已存在：仅刷新显示
                try:
                    await self._cb.update_child_progress(task_key, child_state)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    #  handoff_progress — 节点进度汇报（主节点 / 子节点）
    # ------------------------------------------------------------------

    async def _handle_handoff_progress(self, event: Event) -> None:
        """处理 handoff_progress 事件。

        入口节点 → 追加到 MainTaskState.progress_records + 通知更新。
        非入口节点 → 创建或更新 ChildTaskState + 通知适配器。
        子节点首次出现时调用 create_child_progress 让适配器创建显示消息。
        对应 bot_adapter.py L1595-1707。
        """
        p = event.payload
        node_id = p.get("node_id", "")
        # 过滤系统内部节点（如 turn_summarizer），不推送进度
        if (node_id or "").startswith("system."):
            return
        src_seq = int(p.get("source_inbound_seq") or 0)
        hp_msg = (p.get("message") or "").strip()
        is_system = (node_id or "").startswith("system.")

        # 主节点进度
        if (
            not is_system
            and src_seq
            and src_seq in self._state.triggers
            and node_id == self._entry_node_id
        ):
            trigger = self._state.get_trigger(src_seq)
            trigger.refresh()
            main_state = self._state.get_or_create_main_state(src_seq)
            if hp_msg:
                main_state.progress_records.append(hp_msg)
                try:
                    await self._cb.update_progress(trigger, main_state)
                except Exception:
                    pass
            return

        # 子节点进度
        if not node_id or node_id == self._entry_node_id or not hp_msg:
            return

        task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
        child_state, is_new = self._state.get_or_create_child_state(
            task_key, prefix=node_id,
        )

        if is_new:
            # 新子任务：追加首条消息 → 通知适配器创建显示消息
            child_state.lines.append(hp_msg)
            # [Fork/Merge 2026-05-12] Use only source_inbound_seq for trigger attachment.
            # Why: session fallback is ambiguous when several branch tasks share one parent session.
            # How: resolve_trigger keeps the old signature but ignores event.session_id. Purpose:
            # child progress is attached only to the inbound that created this task.
            result = self._state.resolve_trigger(src_seq, event.session_id)
            trigger = result[1] if result else None
            conv_key = self._state.get_conversation_key(event.session_id) or ""
            try:
                await self._cb.create_child_progress(
                    task_key, child_state,
                    trigger=trigger,
                    conversation_key=conv_key,
                    session_id=event.session_id,
                )
            except Exception as e:
                logger.error("create_child_progress failed: %s", e)
        else:
            # 已有子任务：追加新行 → 通知适配器刷新显示
            child_state.lines.append(hp_msg)
            try:
                await self._cb.update_child_progress(task_key, child_state)
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  stream_delta — 流式输出片段缓存
    # ------------------------------------------------------------------

    async def _handle_stream_delta(self, event: Event) -> None:
        """处理 stream_delta 事件：将文本片段缓存到 MainTaskState。

        SDK 仅做 buffer 累积。dot_state / thinking_preview / 动画等
        展示层逻辑由适配器通过 on_raw_event hook 自行管理。
        对应 bot_adapter.py L1716-1760 的流式处理。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        content = p.get("content", "")
        stream_type = p.get("type", "text")

        if src_seq and src_seq in self._state.triggers:
            self._state.get_trigger(src_seq).refresh()
            main_state = self._state.get_or_create_main_state(src_seq)
            if content and stream_type == "text":
                main_state.stream_parts.append(content)
        # 注意：dot_state / thinking preview / animation 是适配器责任。
        # 适配器可通过 set_raw_event_hook 拦截 stream_delta 事件来实现。

    # ------------------------------------------------------------------
    #  approval_requested — 审批请求处理
    # ------------------------------------------------------------------

    async def _handle_approval_requested(self, event: Event) -> None:
        """处理 approval_requested 事件。

        处理流程：
          1. 全局去重（ApprovalTracker）
          2. per-trigger 去重（MainTaskState.handled_approvals）
          3. 分类：外部操作 → 通知适配器展示审批 UI；
                   内部操作 → 自动放行
          4. 向 progress_records 追加审批状态记录
        对应 bot_adapter.py L993-1006 _process_approval_event。
        """
        p = event.payload
        appr_id = p.get("approval_id", "")
        if not appr_id:
            return

        # 全局去重
        if self._approval.is_handled(appr_id):
            return
        self._approval.mark_handled(appr_id)

        # [Fork/Merge 2026-05-12] per-trigger 去重只能按 source_inbound_seq 精确挂载。
        # Why: approval events may be routed through a shared parent session while multiple branch
        # tasks are active. How: do not call find_trigger_by_session; if the payload has no source
        # sequence, still show the approval UI by session mapping but skip main-progress attachment.
        # Purpose: approval handling remains visible without corrupting another trigger's progress.
        ap_session = str(p.get("session_id") or event.session_id)
        src_seq = int(p.get("source_inbound_seq") or 0)
        result = self._state.resolve_trigger(src_seq, ap_session)
        if result:
            trigger_seq, trigger = result
            trigger.refresh()
            main_state = self._state.get_or_create_main_state(trigger_seq)
            if appr_id in main_state.handled_approvals:
                return
            main_state.handled_approvals.add(appr_id)

        # 分类审批操作：外部 vs 内部，结合 bot 侧 auto_approve_internal 配置决定放行策略。
        # fix: 原逻辑在 workspace_root 未配置时默认自动放行所有操作，不安全。
        # 现改为：auto_approve 行为由 bot 侧 config.auto_approve_internal 显式控制，
        # SDK 默认不自动放行。
        details = p.get("details") or {}
        operation = details.get("tool_name") or p.get("operation", "")

        # 判断是否为外部操作
        is_external = False
        if self._config.workspace_root:
            is_external = is_external_operation(
                details, self._config.workspace_root, self._config.extra_roots,
            )
        else:
            logger.warning("workspace_root not configured, treating all approvals as requiring manual review")
            is_external = True  # 没配置 workspace_root 视为全部需要人工审批

        # 决定是否自动放行
        if is_external or not self._config.auto_approve_internal:
            # 外部操作 或 bot 未开启自动放行 → 通知适配器展示审批 UI
            conv_key = self._state.get_conversation_key(ap_session) or ""
            try:
                await self._cb.show_approval_ui(
                    appr_id, operation, details,
                    conversation_key=conv_key,
                    session_id=ap_session,
                )
            except Exception as e:
                logger.error("show_approval_ui failed: %s", e)
            status = f"⏳ 等待审批: {operation}"
        else:
            # 内部操作 且 bot 开启了自动放行 → 自动放行
            ok = await auto_approve(self._client, appr_id)
            status = (
                f"✅ 自动放行: {operation}"
                if ok
                else f"❌ 自动放行失败: {operation}"
            )

        # 将审批状态追加到主任务进度记录
        if result and status:
            main_state = self._state.get_main_state(result[0])
            if main_state:
                main_state.progress_records.append(status)

    # ------------------------------------------------------------------
    #  task_completed / task_cancelled — 任务生命周期终结
    # ------------------------------------------------------------------

    async def _handle_task_lifecycle(self, event: Event) -> None:
        """处理 task_completed 和 task_cancelled 事件。

        入口节点 task_cancelled → 清理 trigger + 编辑状态消息。
        非入口节点 → 移除子任务状态 + 通知适配器做最终更新。
        对应 bot_adapter.py L1790-1831。
        """
        p = event.payload
        node_id = p.get("node_id", "")
        src_seq = int(p.get("source_inbound_seq") or 0)

        # [Fork/Merge 2026-05-12] 入口节点取消只按 source_inbound_seq 清理 trigger。
        # Why: cancelling one branch must not consume another trigger on the same parent session.
        # How: resolve_trigger performs exact source lookup only. Purpose: lifecycle cleanup follows
        # the task that actually ended.
        if node_id == self._entry_node_id and event.type == "task_cancelled":
            result = self._state.resolve_trigger(src_seq, event.session_id)
            if result:
                trigger_seq, trigger = result
                self._state.consume_trigger(trigger_seq)
                self._state.remove_main_state(trigger_seq)
                try:
                    await self._cb.edit_status_message(
                        trigger, "⚠️ 任务已取消。",
                    )
                except Exception:
                    pass
            return

        # 非入口节点 → 移除子任务状态并通知最终更新
        if not node_id or node_id == self._entry_node_id:
            return
        task_key = p.get("task_id") or f"{node_id}:{event.session_id}"
        child_state = self._state.remove_child_state(task_key)
        if child_state:
            status = (
                "✓ 任务完成"
                if event.type == "task_completed"
                else "✗ 任务已取消"
            )
            # [Fork/Merge 2026-05-12] DM 判断也只从精确 trigger 取得。
            # Why: session_dm_channels is session-scoped and can be ambiguous with concurrent
            # branches. How: if source_inbound_seq no longer maps to a trigger, use False instead
            # of a session fallback. Purpose: avoid applying another inbound's DM cleanup policy.
            result = self._state.resolve_trigger(src_seq, event.session_id)
            is_dm = result[1].is_dm if result else False
            try:
                await self._cb.finalize_child_progress(
                    task_key, child_state, status, is_dm=is_dm,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  cancel_requested — 会话级取消请求
    # ------------------------------------------------------------------

    async def _handle_cancel_requested(self, event: Event) -> None:
        """处理 cancel_requested 事件：清理 trigger 并编辑状态消息。

        对应 bot_adapter.py L1838-1850。
        """
        p = event.payload
        # [Fork/Merge 2026-05-12] Do not clear triggers by session-level cancel events.
        # Why: a parent session can own several running branch triggers. How: only clear when a
        # source_inbound_seq is present and still maps to a trigger; otherwise task_cancelled events
        # will perform exact cleanup. Purpose: session cancel no longer deletes unrelated status UI.
        src_seq = int(p.get("source_inbound_seq") or 0)
        result = self._state.resolve_trigger(src_seq, event.session_id)
        if result:
            trigger_seq, trigger = result
            self._state.consume_trigger(trigger_seq)
            self._state.remove_main_state(trigger_seq)
            try:
                await self._cb.edit_status_message(
                    trigger, "⚠️ 任务已取消。",
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  task_preempted — 任务被新消息打断
    # ------------------------------------------------------------------

    async def _handle_task_preempted(self, event: Event) -> None:
        """处理 task_preempted 事件：清理被打断任务的所有关联状态。

        通过 SessionState.cleanup_for_task_preempted 一次性完成
        trigger 消费、MainTaskState 移除、子任务状态移除。
        然后通知适配器编辑状态消息和最终化子任务进度。
        对应 bot_adapter.py L1852-1887。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)
        task_id = str(p.get("task_id") or "")

        # [Fork/Merge 2026-05-12] Preempt cleanup is exact by source_inbound_seq and task_id.
        # Why: session fallback can clear a different branch's trigger or child progress. How:
        # cleanup_for_task_preempted consumes only the matching source trigger and removes only the
        # exact child task key when present. Purpose: a preempt event cannot affect sibling branches.
        trigger, child_states = self._state.cleanup_for_task_preempted(
            src_seq, event.session_id, task_id=task_id,
        )

        if trigger:
            try:
                await self._cb.edit_status_message(
                    trigger, "⚡ 已被新消息打断。",
                )
            except Exception:
                pass
        # 最终化所有精确关联的子任务状态。无 trigger 时按非 DM 处理，避免 session fallback。
        is_dm = trigger.is_dm if trigger else False
        for task_key, child_state in child_states:
            try:
                await self._cb.finalize_child_progress(
                    task_key, child_state, "⚡ 被打断", is_dm=is_dm,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  compact_start / compact_done / compact_failed — 上下文压缩
    # ------------------------------------------------------------------

    async def _handle_compact(self, event: Event) -> None:
        """处理上下文压缩生命周期事件。

        将压缩状态消息追加到 MainTaskState.progress_records，
        然后通知适配器刷新进度显示。
        对应 bot_adapter.py L1889-1948。
        """
        p = event.payload
        src_seq = int(p.get("source_inbound_seq") or 0)

        # [Fork/Merge 2026-05-12] Compact progress attaches only to the exact inbound sequence.
        # Why: compact events from branch tasks may share a parent session. How: resolve_trigger
        # ignores session fallback and returns None when source_inbound_seq is missing. Purpose:
        # compression progress cannot appear on another active request.
        result = self._state.resolve_trigger(src_seq, event.session_id)
        if not result:
            return
        trigger_seq, trigger = result
        trigger.refresh()
        main_state = self._state.get_or_create_main_state(trigger_seq)

        # 根据事件类型生成状态消息
        if event.type == "snip_compact":
            snipped = p.get("snipped_tasks", 0)
            msg = f"✂️ 轮摘要替换：压缩了 {snipped} 个旧任务"
        elif event.type == "compact_start":
            msg = "🗜️ 上下文压缩中…"
        elif event.type == "compact_done":
            before = p.get("before", 0)
            after = p.get("after", 0)
            if p.get("success", True):
                msg = f"✅ 上下文已压缩：{before} → {after} 条消息"
            else:
                msg = "⚠️ 上下文压缩失败（静默恢复）"
        else:  # compact_failed
            err = p.get("error", "")
            msg = (
                f"⚠️ 上下文压缩失败：{err[:80]}"
                if err
                else "⚠️ 上下文压缩失败"
            )

        main_state.progress_records.append(msg)
        try:
            await self._cb.update_progress(trigger, main_state)
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  context_reset — 会话上下文重置
    # ------------------------------------------------------------------

    async def _handle_context_reset(self, event: Event) -> None:
        """处理 context_reset 事件。

        按 reason 区分：
          reason='compact' → 仅重置水位标记，不清理 trigger/session 状态。
          其他 reason → 通过 cleanup_for_context_reset 完整清理。
        然后通知适配器执行平台侧清理。
        对应 bot_adapter.py L1950-1982。
        """
        p = event.payload
        conv_key = p.get("conversation_key", "")
        reason = p.get("reason", "")

        if reason == "compact":
            # Compact：仅重置水位标记
            # 约定：conv_key 格式为 "prefix:channel_id"（如 "discord:123456"）
            parts = conv_key.split(":", 1)
            if len(parts) == 2:
                try:
                    ch_id = int(parts[1])
                    self._state.reset_channel_watermark(ch_id)
                except ValueError:
                    pass
            cleaned_triggers: list[TriggerInfo] = []
        else:
            # 完整清理
            # 约定：conv_key 格式为 "prefix:channel_id"（如 "discord:123456"）
            # 若其他平台 conv_key 含多个冒号，int(parts[1]) 会失败并被 except 捕获
            parts = conv_key.split(":", 1)
            ch_id = None
            if len(parts) == 2:
                try:
                    ch_id = int(parts[1])
                except ValueError:
                    pass
            cleaned_triggers = self._state.cleanup_for_context_reset(
                conv_key, channel_id=ch_id,
            )

        try:
            await self._cb.on_context_reset(conv_key, reason, cleaned_triggers)
        except Exception as e:
            logger.error("on_context_reset callback failed: %s", e)

    # ------------------------------------------------------------------
    #  inbound_accepted — 水位确认
    # ------------------------------------------------------------------

    async def _handle_inbound_accepted(self, event: Event) -> None:
        """处理 inbound_accepted 事件：推进频道高水位。

        inbound 消息被 Supervisor 正式接受后，才将 pending_watermark
        推进为 last_ctx_seq，防止未接受的消息错误地认为历史已发送。
        对应 bot_adapter.py L1984-1989。
        """
        p = event.payload
        ia_seq = int(p.get("inbound_seq") or 0)
        if ia_seq:
            self._state.accept_watermark(ia_seq)

    # ------------------------------------------------------------------
    #  engine_registered — Engine 重启检测
    # ------------------------------------------------------------------

    async def _handle_engine_registered(self, event: Event) -> None:
        """Handle engine_registered: notify adapter when engine restarts."""
        p = event.payload
        prev_gen = (p.get("previous_generation_id") or "").strip()
        if not prev_gen:
            # First boot, not a restart — skip
            return
        try:
            await self._cb.on_engine_restarted(p)
        except Exception as e:
            logger.error("on_engine_restarted callback error: %s", e)

    # ==================================================================
    #  Handler 分发表
    #  类变量，映射 event.type → 处理器方法。
    #  _dispatch() 通过此表路由事件到对应处理器。
    # ==================================================================

    _HANDLERS: dict[
        str, Callable[["EventRouter", Event], Awaitable[None]]
    ] = {
        "inbound_message":   _handle_inbound_message,
        "task_created":      _handle_task_created,
        "outbound_message":  _handle_outbound_message,
        "intermediate_reply": _handle_intermediate_reply,
        "node_started":      _handle_node_event,
        "node_completed":    _handle_node_event,
        "handoff_progress":  _handle_handoff_progress,
        "stream_delta":      _handle_stream_delta,
        "approval_requested": _handle_approval_requested,
        "task_completed":    _handle_task_lifecycle,
        "task_cancelled":    _handle_task_lifecycle,
        "cancel_requested":  _handle_cancel_requested,
        "task_preempted":    _handle_task_preempted,
        "compact_start":     _handle_compact,
        "compact_done":      _handle_compact,
        "compact_failed":    _handle_compact,
        "snip_compact":      _handle_compact,
        "context_reset":     _handle_context_reset,
        "inbound_accepted":  _handle_inbound_accepted,
        "engine_registered": _handle_engine_registered,
    }
