"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config

from ._helpers import _now
from .types import Task, TaskKind, TaskStatus


log = logging.getLogger(__name__)


class TaskRouterMixin:
    """提供 _route_completed_task_locked 及其子路由方法。

    运行时 self 是 SupervisorState 实例，可以访问
    self.tasks / self._event_task_snapshot / self.append_outbound_message 等。
    """

    # ------------------------------------------------------------------ #
    #  统一路由入口
    # ------------------------------------------------------------------ #

    def _route_completed_task_locked(self, task: Task) -> None:
        """统一路由入口。根据 result.action 分发。"""
        # ---- 批量 task：优先走统一批量收集 ----
        if task.batch_id:
            self._try_complete_batch_locked(task)
            # 后置记忆提取不对 batch 子 task 触发
            return

        # ---- 压缩 dispatch 结果：compactor 子 task 完成 ----
        if self._is_compact_dispatch_result(task):
            self._apply_compact_result_locked(task)
            return

        # ---- 轮摘要 dispatch 结果：summarizer 子 task 完成 ----
        if self._is_turn_summary_result(task):
            self._apply_turn_summary_result_locked(task)
            return

        action = task.result or {}
        act = str(action.get("action") or "").strip()
        if act == "preempted":
            self._route_preempted_locked(task, action)
            return  # preempted 不触发记忆提取
        elif act == "dispatch":
            self._route_dispatch_locked(task, action)
        elif act in ("finish", "ask"):
            self._route_finish_locked(task, action)
        elif act == "fail":
            self._route_fail_locked(task, action)
        # cancelled → 不做路由

        # 后置触发：检查是否需要创建记忆提取任务
        self._maybe_trigger_memory_extract_locked(task)

        # 后置触发：检查是否需要创建轮摘要任务
        self._maybe_trigger_turn_summary_locked(task)

    # ------------------------------------------------------------------ #
    #  dispatch
    # ------------------------------------------------------------------ #

    def _route_dispatch_locked(self, task: Task, action: dict[str, Any]) -> None:
        target = str(action.get("target_node") or "").strip()
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        dispatch_batch = action.get("dispatch_batch")

        # 标记父 task 正在等待压缩 dispatch
        if dispatch_input.get("_compact_dispatch"):
            task.input["_compact_dispatch_pending"] = True
            task.input["_compact_keep_recent"] = int(
                dispatch_input.get("_compact_keep_recent", 6)
            )

        created_child_ids: list[str] = []

        # ---- 批量委派（node 或 tool 均可） ----
        if isinstance(dispatch_batch, list) and dispatch_batch:
            batch_id = str(uuid.uuid4())
            for idx, item in enumerate(dispatch_batch):
                item_kind = str(item.get("kind") or "node").strip()
                item_target = str(item.get("target") or "").strip()
                if not item_target:
                    continue

                if item_kind == "tool":
                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.tool,
                        tool_name=item_target,
                        input_data={
                            "arguments": item.get("arguments", {}),
                            "call_id": str(item.get("call_id") or ""),
                        },
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                else:
                    _batch_ctx_mode = str(item.get("context_mode") or "accumulate").strip()
                    _batch_ctx_key = str(item.get("context_key") or "").strip() or None
                    _batch_input: dict[str, Any] = {
                        "instruction": str(item.get("instruction") or "").strip(),
                    }
                    if _batch_ctx_key:
                        _batch_input["_context_key"] = _batch_ctx_key

                    # Child Session 隔离（Phase B）：批量委派也走 child session
                    _b_child_sid, _b_is_new = self.get_or_create_child_session(
                        task.session_id, item_target, _batch_ctx_key or "", _batch_ctx_mode,
                    )
                    _batch_input["child_session_id"] = _b_child_sid
                    _batch_input["context_mode"] = _batch_ctx_mode
                    _batch_input["use_context"] = False
                    if _batch_ctx_mode == "fork":
                        _batch_input["fork_from_session_id"] = task.session_id
                    # 审计报告 Step 1（2026-04-16）：删除此处对 _find_last_context_ref_locked
                    # 的 accumulate fallback 注入。engine/runner.py 在 child_session_id
                    # 非空时会无条件清空 context_ref（参见 runner.py:514），此注入永远
                    # 不会被消费。保留只是兼容期死代码，现移除。

                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.node,
                        node_id=item_target,
                        input_data=_batch_input,
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                created_child_ids.append(child.task_id)

        # ---- 单节点委派 ----
        if not created_child_ids and target:
            _ctx_mode = str(dispatch_input.get("context_mode") or "accumulate").strip()
            _ctx_key = str(dispatch_input.get("context_key") or "").strip() or None
            _single_input: dict[str, Any] = {
                "instruction": str(dispatch_input.get("instruction") or "").strip(),
            }
            if _ctx_key:
                _single_input["_context_key"] = _ctx_key

            # Child Session 隔离（Phase B）：用 child session 替代 context_ref
            _child_sid, _is_new = self.get_or_create_child_session(
                task.session_id, target, _ctx_key or "", _ctx_mode,
            )
            _single_input["child_session_id"] = _child_sid
            _single_input["context_mode"] = _ctx_mode
            _single_input["use_context"] = False  # child session 模式不走 _fetch_history
            if _ctx_mode == "fork":
                # fork 模式需要告诉 engine 从哪个 session 复制历史
                _single_input["fork_from_session_id"] = task.session_id

            # 审计报告 Step 1（2026-04-16）：删除此处单节点 dispatch 的 accumulate/fresh
            # 兼容期 fallback。engine/runner.py:514 在 child_session_id 非空时会无条件
            # 清空 context_ref，accumulate/fresh 两个分支写的值永远不会被消费。

            child = self._create_task_locked(
                session_id=task.session_id,
                session_generation=task.session_generation,
                kind=TaskKind.node,
                node_id=target,
                input_data=_single_input,
                continuation={},
                source_inbound_seq=task.source_inbound_seq,
                caller_task_id=task.task_id,
            )
            created_child_ids.append(child.task_id)

        # ---- 挂起当前 task ----
        if created_child_ids:
            task.status = TaskStatus.suspended
            task.waiting_for_task_id = created_child_ids[0] if len(created_child_ids) == 1 else None
            task.updated_at = _now()
            self._event_task_snapshot("task_suspended", task)

    # ------------------------------------------------------------------ #
    #  preempted
    # ------------------------------------------------------------------ #

    def _route_preempted_locked(self, task: Task, action: dict[str, Any]) -> None:
        """处理 preempted 结果：保存 context_ref，发射事件，恢复 caller（如有）。"""
        context_ref = str(action.get("context_ref") or "")
        task.preempted_context_ref = context_ref

        # 发射 task_preempted 事件给 Bot
        self.eventlog.append(
            session_id=task.session_id,
            component="supervisor",
            type_="task_preempted",
            payload={
                "task_id": task.task_id,
                "node_id": task.node_id,
                "context_ref": context_ref,
                "session_id": task.session_id,
            },
            transient=True,
        )

        # 子节点被 preempt：恢复 suspended 的 caller，注入 child_preempted
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if caller and caller.status == TaskStatus.suspended:
                caller.status = TaskStatus.pending
                caller.waiting_for_task_id = None
                caller.worker_id = None
                caller.lease_expires_at = None
                caller.updated_at = _now()
                caller.input["resume_data"] = {
                    "type": "child_preempted",
                    "child_node_id": str(task.node_id or ""),
                    "context_ref": context_ref,
                }
                caller.input["context_ref"] = str(
                    caller.result.get("context_ref")
                    or caller.input.get("context_ref")
                    or ""
                )
                self._event_task_snapshot("task_resumed", caller)

        # 不输出给用户，不注入 inbound

    # ------------------------------------------------------------------ #
    #  finish / ask / fail
    # ------------------------------------------------------------------ #

    def _route_finish_locked(self, task: Task, action: dict[str, Any]) -> None:
        result = action.get("result") if isinstance(action.get("result"), dict) else {}
        self._resume_caller_or_output_locked(task, {
            "type": "child_result",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
        }, result)


    def _route_fail_locked(self, task: Task, action: dict[str, Any]) -> None:
        error = str(action.get("error") or "未知错误").strip()
        self._resume_caller_or_output_locked(task, {
            "type": "child_failed",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "error": error,
        }, {"text": f"[错误] {error}"})

    # ------------------------------------------------------------------ #
    #  统一批量收集
    # ------------------------------------------------------------------ #

    def _try_complete_batch_locked(self, task: Task) -> None:
        """统一批量收集。不区分 tool/node，等同 batch_id 全部终态后打包恢复 caller。"""
        batch_id = task.batch_id
        if not batch_id:
            return

        # 找所有同 batch 的兄弟
        siblings: list[Task] = []
        for t in self.tasks.values():
            if t.batch_id == batch_id and t.session_id == task.session_id:
                siblings.append(t)
        if not siblings:
            return

        # 有任何一个还没结束，等着
        if any(not self._task_terminal(t) for t in siblings):
            return

        # 防止重复恢复（多个兄弟同时完成时可能多次进入这里）
        resume_key = f"batch:{batch_id}"
        for t in self.tasks.values():
            if t.session_id == task.session_id and str(t.input.get("_resume_key") or "") == resume_key:
                return

        # 按 batch_index 排序
        siblings.sort(key=lambda x: x.batch_index)

        # 收集结果
        entries: list[dict[str, Any]] = []
        for t in siblings:
            tr = t.result.get("result") if isinstance(t.result.get("result"), dict) else {}
            act = str(t.result.get("action") or "").strip()

            entry: dict[str, Any] = {
                "kind": t.kind.value,
                "status": act,
                "summary": str(t.result.get("summary") or tr.get("summary") or ""),
            }

            if t.kind == TaskKind.node:
                entry["node_id"] = str(t.node_id or "")
                entry["instruction"] = str(t.input.get("instruction") or "")
                entry["text"] = str(tr.get("text") or "")
            elif t.kind == TaskKind.tool:
                entry["name"] = str(t.tool_name or tr.get("tool_name") or "")
                entry["args"] = dict(tr.get("arguments") or t.input.get("arguments") or {})
                entry["format"] = str(tr.get("raw_format") or tr.get("format") or "json")
                entry["raw_inline"] = str(tr.get("raw_inline") or tr.get("text") or "")
                entry["truncated"] = bool(tr.get("truncated", False))
                entry["ref"] = str(tr.get("ref") or "")

            if act == "fail":
                entry["error"] = str(t.result.get("error") or "")

            atts = tr.get("attachments")
            if isinstance(atts, list):
                entry["attachments"] = atts

            entries.append(entry)

        # 构建摘要
        total = len(entries)
        fail_count = sum(1 for e in entries if e["status"] == "fail")
        summary_parts = [f"批量完成 ({total} 个)"]
        if fail_count:
            summary_parts.append(f"{fail_count} 个失败")
        summary_text = ", ".join(summary_parts)

        resume_data: dict[str, Any] = {
            "type": "batch_results",
            "entries": entries,
        }
        self._resume_caller_or_output_locked(
            task, resume_data, {"summary": summary_text},
        )

    # ------------------------------------------------------------------ #
    #  公共：恢复 caller 或输出给用户
    # ------------------------------------------------------------------ #

    def _resume_caller_or_output_locked(
        self, task: Task, resume_data: dict[str, Any], fallback_result: dict[str, Any],
    ) -> None:
        """尝试恢复 caller 节点。如果没有 caller，把 fallback_result 输出给用户。"""
        # 优先尝试直接唤醒 suspended 的 caller
        caller_id = (task.caller_task_id or "").strip()
        if caller_id:
            caller = self.tasks.get(caller_id)
            if caller and caller.status == TaskStatus.suspended:
                caller.status = TaskStatus.pending
                caller.waiting_for_task_id = None
                caller.worker_id = None
                caller.lease_expires_at = None
                caller.updated_at = _now()
                # 注入 resume_data 到 caller 的 input 中
                caller.input["resume_data"] = resume_data
                caller.input["context_ref"] = str(caller.result.get("context_ref") or caller.input.get("context_ref") or "")
                # 标记防重复恢复
                if task.batch_id:
                    caller.input["_resume_key"] = f"batch:{task.batch_id}"
                self._event_task_snapshot("task_resumed", caller)
                return

        # 没有 suspended caller
        # 异步 dispatch 子任务完成 → 注入 inbound 通知入口节点
        if task.input.get("_async_dispatch"):
            self._inject_async_dispatch_result_locked(task, fallback_result)
            return

        # 系统内部任务（如记忆提取）静默完成，不输出给用户
        if task.input.get("_system_task"):
            return
        text = str(fallback_result.get("text") or fallback_result.get("summary") or "").strip()
        atts = fallback_result.get("attachments") if isinstance(fallback_result.get("attachments"), list) else None
        # [Fix] finish(text="") 空文本时也产出 outbound_message 事件。
        # 原先 `if text or atts:` 导致空文本 finish 不发事件，
        # Bot 侧 trigger 和 status_msg（含 stream_delta 累积的原始标记预览）永远无法清理。
        # 去掉守卫后，Bot 侧 send_reply 会跳过 Discord 发送但正常执行清理收尾。
        self.append_outbound_message(
            session_id=task.session_id, text=text,
            attachments=atts, source_inbound_seq=task.source_inbound_seq,
            node_id=task.node_id,
        )


    # ------------------------------------------------------------------ #
    #  异步 dispatch 结果注入
    # ------------------------------------------------------------------ #

    def _inject_async_dispatch_result_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """异步 dispatch 子任务完成后，将结果注入 session。

        优先尝试 preempt 当前 running 的入口 task（V2 preempt：engine
        在 checkpoint 检测到信号后注入消息并继续执行，无需新建 task）。
        若 session 中没有 running 的入口 task，则走传统 inbound 路径
        创建新 task。

        此方法在 self._lock 内调用，不可调用会再次获取 _lock 的公开方法。
        """
        result_text = str(fallback_result.get("text") or "").strip()
        result_summary = str(fallback_result.get("summary") or "").strip()
        caller_node = str(task.input.get("_caller_node_id") or "").strip()

        notify_parts: list[str] = [f"[异步子任务完成] 节点 {task.node_id} 已完成。"]
        if caller_node:
            notify_parts[0] = f"[异步子任务完成] {caller_node} 委派的 {task.node_id} 已完成。"
        if result_summary:
            notify_parts.append(f"摘要：{result_summary}")
        if result_text:
            notify_parts.append(f"结果：\n{result_text}")
        notify_text = "\n".join(notify_parts)

        result_atts = (
            fallback_result.get("attachments")
            if isinstance(fallback_result.get("attachments"), list)
            else None
        )

        session_info = self.sessions.get(task.session_id)
        if not session_info:
            return

        # 检查 session generation 是否过期
        current_gen = self._current_session_generation_locked(task.session_id)
        if task.session_generation and current_gen and task.session_generation != current_gen:
            return

        # 检查 session 是否已被 cancel
        if task.session_id in self._cancelled_sessions:
            return

        # ---- 优先路径：preempt running 的入口 task ----
        running_entry = self._find_running_entry_task_locked(task.session_id)
        if running_entry is not None and not running_entry.preempt_requested:
            running_entry.preempt_requested = True
            running_entry.preempt_message = notify_text
            running_entry.preempt_attachments = list(result_atts or [])
            self.eventlog.append(
                session_id=task.session_id,
                component="supervisor",
                type_="preempt_requested",
                payload={
                    "task_id": running_entry.task_id,
                    "session_id": task.session_id,
                    "has_message": True,
                    "reason": "async_dispatch_result",
                    "source_task_id": task.task_id,
                },
                transient=True,
            )
            return

        # ---- 次优路径：标记 suspended 的入口 task ----
        # 入口 task 可能暂时挂起（如等待 compactor），此时无法立即 preempt。
        # 预设 preempt 标记后，task 恢复为 running 时 engine 会在首次推理
        # 循环中检测到并注入结果，避免结果丢失。
        suspended_entry = self._find_suspended_entry_task_locked(task.session_id)
        if suspended_entry is not None and not suspended_entry.preempt_requested:
            suspended_entry.preempt_requested = True
            suspended_entry.preempt_message = notify_text
            suspended_entry.preempt_attachments = list(result_atts or [])
            self.eventlog.append(
                session_id=task.session_id,
                component="supervisor",
                type_="preempt_requested",
                payload={
                    "task_id": suspended_entry.task_id,
                    "session_id": task.session_id,
                    "has_message": True,
                    "reason": "async_dispatch_result_deferred",
                    "source_task_id": task.task_id,
                },
                transient=True,
            )
            return

        # ---- 回退路径：创建 inbound → 新 task ----
        conv_key = session_info.conversation_key
        channel = session_info.channel
        msg_id = f"async_dispatch:{task.task_id}"

        payload: dict[str, Any] = {
            "channel": channel,
            "conversation_key": conv_key,
            "message_id": msg_id,
            "text": notify_text,
        }
        if result_atts:
            payload["attachments"] = result_atts

        # eventlog 有独立锁，不会与 self._lock 死锁
        evt = self.eventlog.append(
            session_id=task.session_id,
            component="supervisor",
            type_="inbound_message",
            payload=payload,
        )
        seq = int(evt.get("seq", 0))
        self._apply_inbound_message(seq=seq, session_id=task.session_id, payload=payload)
        self._advance_inbound_cursor()
        self._create_entry_task_for_inbound_locked(
            inbound_seq=seq, session_id=task.session_id, payload=payload,
        )

    def _find_entry_task_by_status_locked(self, session_id: str, statuses: set) -> Task | None:
        """在 session 中查找指定状态的入口 task。

        入口 task 定义：无 caller、非异步 dispatch 子任务、非系统任务。
        若存在多个符合条件的 task（理论上不应发生），返回最早创建的。
        """
        candidate: Task | None = None
        for t in self.tasks.values():
            if t.session_id != session_id:
                continue
            if t.status not in statuses:
                continue
            # 排除：有 caller 的子任务
            if t.caller_task_id:
                continue
            # 排除：异步 dispatch 子任务自身
            if t.input.get("_async_dispatch"):
                continue
            # 排除：系统内部任务
            if t.input.get("_system_task"):
                continue
            if candidate is None or t.created_at < candidate.created_at:
                candidate = t
        return candidate

    def _find_running_entry_task_locked(self, session_id: str) -> Task | None:
        """在 session 中查找 running 状态的入口 task。"""
        return self._find_entry_task_by_status_locked(session_id, {TaskStatus.running})

    def _find_suspended_entry_task_locked(self, session_id: str) -> Task | None:
        """在 session 中查找 suspended 状态的入口 task。"""
        return self._find_entry_task_by_status_locked(session_id, {TaskStatus.suspended})

    # ------------------------------------------------------------------ #
    #  压缩 dispatch 结果处理
    # ------------------------------------------------------------------ #

    def _is_compact_dispatch_result(self, task: Task) -> bool:
        """判断是否是 compactor 子 task 的完成事件。"""
        caller_id = (task.caller_task_id or "").strip()
        if not caller_id:
            return False
        caller = self.tasks.get(caller_id)
        if not caller:
            return False
        return bool(caller.input.get("_compact_dispatch_pending"))

    def _apply_compact_result_locked(self, task: Task) -> None:
        """compactor 完成后，对父 task 的上下文执行压缩并恢复父 task。

        Step 2（2026-04-16）：支持两条路径。
        - ConversationStore 路径：caller 是主节点（flag 开启）或子节点（有 child_session_id），
          直接操作 data/conversations/{target}.jsonl，用 summary 消息替换中间部分。
        - 旧 snapshot 路径：flag 关闭时的主节点，沿用 caller.input.context_ref 读写。
        """
        from engine.compact import _format_compact_summary, apply_compact_summary
        from engine.context_store import load_context_snapshot, write_context_snapshot

        caller_id = task.caller_task_id
        caller = self.tasks.get(caller_id)
        if not caller or caller.status != TaskStatus.suspended:
            return

        act = str((task.result or {}).get("action") or "").strip()
        parent_ctx_ref = str(
            caller.result.get("context_ref")
            or caller.input.get("context_ref")
            or ""
        ).strip()

        # ---- 选择压缩路径 ----
        # 1. caller 带 child_session_id → ConversationStore（child session）
        # 2. caller 无 context_ref + main_session_enabled → ConversationStore（主 session）
        # 3. 否则 → 旧 snapshot 路径
        from clonoth_runtime import load_runtime_config
        _rc = load_runtime_config(self.workspace_root)
        _main_conv_enabled = bool(
            _rc.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)
        )
        child_sid = str(caller.input.get("child_session_id") or "").strip()
        target_sid_for_conv = ""
        if child_sid:
            target_sid_for_conv = child_sid
        elif _main_conv_enabled:
            # Step 2 修复：main_session_enabled=true 时无条件走 ConvStore 路径。
            # 原条件 `and not parent_ctx_ref` 会被 _persist_ctx 写入的 context_ref 拦住，
            # 导致 compact 走旧 snapshot 路径，而 runner.py 已改为从 JSONL 读取——读写不一致。
            target_sid_for_conv = caller.session_id

        if act == "finish":
            result = (task.result or {}).get("result") or {}
            raw_summary = str(result.get("text") or "").strip()
            summary = _format_compact_summary(raw_summary)

            if summary and target_sid_for_conv:
                # ---- ConversationStore 路径 ----
                before, after = self._apply_compact_via_conv_store_locked(
                    target_sid_for_conv, summary,
                    keep_recent=int(caller.input.get("_compact_keep_recent", 6)),
                )
                if after < before:
                    self._resume_compact_parent_locked(
                        caller, parent_ctx_ref,
                        before=before, after=after, success=True,
                    )
                    return
                # before == after：消息太少未压缩，静默恢复
                self._resume_compact_parent_locked(
                    caller, parent_ctx_ref,
                    before=before, after=after, success=False,
                )
                return

            if summary and parent_ctx_ref:
                # ---- 旧 snapshot 路径（flag 关闭时的主节点）----
                snapshot = load_context_snapshot(self.workspace_root, parent_ctx_ref)
                if snapshot and isinstance(snapshot.get("messages"), list):
                    old_messages = snapshot["messages"]
                    keep_recent = int(caller.input.get("_compact_keep_recent", 6))
                    compressed = apply_compact_summary(
                        old_messages, summary, keep_recent=keep_recent,
                    )
                    if len(compressed) < len(old_messages):
                        snapshot["messages"] = compressed
                        write_context_snapshot(
                            self.workspace_root, parent_ctx_ref, snapshot,
                        )
                        self._resume_compact_parent_locked(
                            caller, parent_ctx_ref,
                            before=len(old_messages),
                            after=len(compressed),
                            success=True,
                        )
                        return

        # 失败路径：静默恢复父 task
        self._resume_compact_parent_locked(
            caller, parent_ctx_ref, success=False,
        )

    def _apply_compact_via_conv_store_locked(
        self, target_session_id: str, summary: str, *, keep_recent: int,
    ) -> tuple[int, int]:
        """Step 2（2026-04-16）：在 ConversationStore 层面执行压缩。

        读取 target session 的 JSONL，用 summary 消息替换中间部分，保留最后
        keep_recent 条消息。与 engine.compact.apply_compact_summary 的策略一致，
        但直接操作 Message 对象而非 dict。

        注意 ConversationStore 里没有 system 消息（system prompt 每轮由 ai_step
        的 assemble_initial_messages 重建），所以无需处理 prefix/inner system。

        Returns: (before_count, after_count)
        """
        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import ConversationStore, Message, MessageType

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        msgs = store.load(target_session_id)
        before = len(msgs)

        # 消息太少（加上 summary 和保留的也不会变短）→ 不压缩
        if before <= keep_recent + 1:
            return before, before

        to_keep = msgs[-keep_recent:] if keep_recent > 0 else []

        # P6.5 Metadata Preservation: 收集被压缩掉的消息所属的 source_task_id，
        # 存入 summary 消息的 meta 中。L2 snip_history 据此判断哪些 task 已被
        # LLM 压缩过，避免因 ID 丢失而反复 fall through 到 LLM compact。
        to_remove = msgs[:-keep_recent] if keep_recent > 0 else list(msgs)
        compressed_task_ids = list({m.source_task_id for m in to_remove if m.source_task_id})

        summary_msg = Message(
            id=str(uuid4()),
            role="user",
            content="[以下是之前对话的结构化摘要，原始上下文已被压缩]\n\n" + summary,
            message_type=MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_task_id="compact_summary",
            meta={"compressed_task_ids": compressed_task_ids},
        )
        new_messages = [summary_msg] + to_keep
        store.replace_all(target_session_id, new_messages)
        return before, len(new_messages)

    def _resume_compact_parent_locked(
        self, caller: Task, context_ref: str, *,
        before: int = 0, after: int = 0, success: bool = True,
    ) -> None:
        """恢复等待压缩的父 task。"""
        caller.status = TaskStatus.pending
        caller.waiting_for_task_id = None
        caller.worker_id = None
        caller.lease_expires_at = None
        caller.updated_at = _now()
        caller.input["resume_data"] = {
            "type": "compact_done",
            "success": success,
            "before": before,
            "after": after,
        }
        caller.input["context_ref"] = context_ref
        caller.input.pop("_compact_dispatch_pending", None)
        self._event_task_snapshot("task_resumed", caller)


    # ------------------------------------------------------------------ #
    #  Turn Summary — 非阻塞轮摘要节点化
    #  原先在 engine/runner.py 中做阻塞式 LLM 调用生成轮摘要，
    #  改为 supervisor 在 task 完成后按需触发独立的 system.turn_summarizer 节点。
    #  Created: 2026-04-25
    # ------------------------------------------------------------------ #

    def _is_turn_summary_result(self, task: Task) -> bool:
        """判断是否是 turn_summarizer 子 task 的完成事件。"""
        return bool(task.input.get("_turn_summary_dispatch"))

    def _apply_turn_summary_result_locked(self, task: Task) -> None:
        """summarizer 完成后，将摘要回写到 TaskRecord。

        write_task_record 是 append 模式（追加到 JSONL），所以回写 summary 时
        会追加一条新记录。同一 task_id 出现多次时，后者覆盖前者——
        snip_history 遍历全部 records 取最后一条有 summary 的。
        """
        act = str((task.result or {}).get("action") or "").strip()
        if act != "finish":
            return

        result = (task.result or {}).get("result") or {}
        summary = str(result.get("text") or "").strip()
        if not summary or len(summary) < 50:
            return

        target_task_id = str(task.input.get("_target_task_id") or "").strip()
        target_session_id = str(task.input.get("_target_session_id") or "").strip()
        if not target_task_id or not target_session_id:
            return

        # 回写 TaskRecord：追加一条 updated record
        try:
            from engine.task_record import TaskRecord, write_task_record, load_task_records
            from pathlib import Path
            records = load_task_records(Path(self.workspace_root), target_session_id)
            for r in records:
                if r.task_id == target_task_id:
                    r.summary = summary
                    write_task_record(Path(self.workspace_root), r)
                    log.info("Turn summary written for task %s: %d chars", target_task_id[:12], len(summary))
                    break
        except Exception as e:
            log.warning("Failed to write turn summary for task %s: %s", target_task_id[:12], e)

    def _maybe_trigger_turn_summary_locked(self, task: Task) -> None:
        """Task 完成后，检查是否需要创建轮摘要后置任务。

        门控条件：
        - 仅对 finish/fail 的 node 类型 task 触发
        - 不对系统内部任务触发（避免递归）
        - tool_call_count >= 3 或 total_tokens >= 4000
        - runtime.yaml 中 engine.turn_summary.enabled 为 true
        """
        act = str((task.result or {}).get("action") or "").strip()
        if act not in ("finish", "fail") or task.kind != TaskKind.node:
            return
        # 不对系统内部任务触发
        if task.input.get("_system_task"):
            return

        runtime_cfg = load_runtime_config(self.workspace_root)
        if not get_bool(runtime_cfg, "engine.turn_summary.enabled", True):
            return

        # 门控：仅针对长工具链，短任务不触发
        min_calls = get_int(runtime_cfg, "engine.turn_summary.min_tool_calls", 3)
        _tool_call_count = (task.result or {}).get("_tool_call_count", 0)
        if _tool_call_count < min_calls:
            return

        # 格式化 task 消息，作为 summarizer 的输入
        _instruction = ""
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            _store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            _child_sid = task.input.get("child_session_id") or ""
            _load_sid = _child_sid if _child_sid else task.session_id
            _all_msgs = _store.load(_load_sid)
            _task_msgs = [m for m in _all_msgs if m.source_task_id == task.task_id]
            if not _task_msgs:
                return
            _parts: list[str] = []
            for _m in _task_msgs:
                _c = _m.content or ""
                if len(_c) > 5000:
                    _c = _c[:5000] + "\n...[truncated]"
                _parts.append(f"[{_m.role}]\n{_c}")
            _instruction = "\n\n---\n\n".join(_parts)
        except Exception as e:
            log.warning("Failed to format task messages for turn summary: %s", e)
            return

        if not _instruction.strip():
            return

        # 截断（保持在 ~30K chars，与原 turn_summary.py 一致）
        if len(_instruction) > 30000:
            _instruction = _instruction[:30000] + "\n...[truncated]"

        summarizer_node = get_str(runtime_cfg, "engine.turn_summary.node_id", "system.turn_summarizer").strip()
        self._create_task_locked(
            session_id=task.session_id,
            session_generation=task.session_generation,
            kind=TaskKind.node,
            node_id=summarizer_node,
            input_data={
                "instruction": _instruction,
                "_system_task": True,
                "_turn_summary_dispatch": True,
                "_target_task_id": task.task_id,
                "_target_session_id": task.session_id,
            },
            continuation={},
            source_inbound_seq=None,
            caller_task_id=None,
        )

    # ------------------------------------------------------------------ #
    #  后置记忆提取
    # ------------------------------------------------------------------ #

    def _maybe_trigger_memory_extract_locked(self, task: Task) -> None:
        """入口节点 finish 后，检查是否需要创建记忆提取后置任务。

        P3 改进 (2026-04-25):
          - 互斥：主节点已调 save_memory 时跳过（避免重复存储）
          - 缩窄范围：只提取当前 task 的消息，不发全量 session 历史
          - 门控保持消息增量计数（兼容旧逻辑）
        """
        # 仅对入口节点的 finish 动作触发
        act = str((task.result or {}).get("action") or "").strip()
        if act != "finish" or task.kind != TaskKind.node:
            return
        # 不对系统内部任务触发（避免提取节点自身完成后递归触发）
        if task.input.get("_system_task"):
            return

        runtime_cfg = load_runtime_config(self.workspace_root)
        entry_node_id = get_str(runtime_cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip()
        if task.node_id != entry_node_id:
            return
        if not get_bool(runtime_cfg, "memory.auto_extract.enabled", False):
            return

        # P3 互斥：主节点这轮已调 save_memory → 跳过提取
        _tool_names = (task.result or {}).get("_tool_names") or []
        if "save_memory" in _tool_names:
            return

        # 门控：消息数量
        msgs = self.session_messages(session_id=task.session_id, limit=0)  # limit=0 → 全量
        non_system = [m for m in msgs if m.get("role") != "system"]
        current_count = len(non_system)

        min_messages = get_int(runtime_cfg, "memory.auto_extract.min_messages", 4, min_value=2, max_value=100)
        if current_count < min_messages:
            return

        min_increment = get_int(runtime_cfg, "memory.auto_extract.min_increment", 10, min_value=1, max_value=100)
        last_count = self._memory_extract_msg_counts.get(task.session_id, 0)
        if current_count - last_count < min_increment:
            return

        # P3→修正：提取「上次游标到当前」之间所有 task 的消息
        # 避免只取当前 task 导致中间 task 被永久跳过
        _range_msgs = []
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            _store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            _all_msgs = _store.load(task.session_id)
            # 取非 system 消息，按存储顺序截取 last_count → current_count 区间
            _non_sys = [m for m in _all_msgs if m.role != "system"]
            _range_msgs = _non_sys[last_count:current_count]
        except Exception:
            pass

        if _range_msgs:
            _parts: list[str] = []
            for _tm in _range_msgs:
                _c = _tm.content or ""
                if len(_c) > 2000:
                    _c = _c[:2000] + "...<truncated>"
                _parts.append(f"[{_tm.role}]\n{_c}")
            transcript = "\n\n---\n\n".join(_parts)
        else:
            # fallback：旧方式全量格式化
            transcript = self._format_transcript_for_extract(msgs)

        if not transcript.strip():
            return

        # P4b 预注入：扫描已有记忆清单，注入 instruction 防重复
        _existing_memories = ""
        try:
            import yaml as _yaml
            _mem_dir = Path(self.workspace_root) / "data" / "memory"
            if _mem_dir.exists():
                _mem_lines: list[str] = []
                for _yf in sorted(_mem_dir.glob("*.yaml")):
                    try:
                        with open(_yf, "r", encoding="utf-8") as _f:
                            _book_data = _yaml.safe_load(_f) or {}
                        _bname = str(_book_data.get("book") or _yf.stem)
                        for _e in (_book_data.get("entries") or []):
                            if isinstance(_e, dict):
                                _eid = str(_e.get("id") or "")
                                _ec = str(_e.get("content") or "")[:80]
                                if _eid:
                                    _mem_lines.append(f"  - [{_bname}] {_eid}: {_ec}")
                    except Exception:
                        continue
                if _mem_lines:
                    _existing_memories = "\n\n[已有记忆清单 — 避免重复创建]\n" + "\n".join(_mem_lines) + "\n"
        except Exception:
            pass

        # 更新游标
        self._memory_extract_msg_counts[task.session_id] = current_count

        # 创建后置任务
        _full_instruction = transcript
        if _existing_memories:
            _full_instruction = _existing_memories + "\n---\n\n" + transcript

        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        self._create_task_locked(
            session_id=task.session_id,
            session_generation=task.session_generation,
            kind=TaskKind.node,
            node_id=extractor_node,
            input_data={
                "instruction": _full_instruction,
                "_system_task": True,
            },
            continuation={},
            source_inbound_seq=None,
            caller_task_id=None,
        )

    @staticmethod
    def _format_transcript_for_extract(
        messages: list[dict[str, Any]],
        *,
        max_chars: int = 12000,
    ) -> str:
        """将会话消息格式化为对话记录文本，供记忆提取节点分析。"""
        parts: list[str] = []
        total = 0
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                ]
                content = "\n".join(texts)
            if not isinstance(content, str):
                content = str(content)
            if len(content) > 2000:
                content = content[:2000] + "...<truncated>"
            line = f"[{role}]\n{content}"
            total += len(line)
            if total > max_chars:
                break
            parts.append(line)
        parts.reverse()
        return "\n\n---\n\n".join(parts)
