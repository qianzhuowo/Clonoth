"""Task 生命周期 mixin —— 创建、领取、完成、取消。"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from clonoth_runtime import get_str, load_runtime_config

from ._helpers import _now
from .types import Task, TaskKind, TaskStatus


class TaskStoreMixin:
    """提供 Task 创建 / 领取 / 完成 / 取消等方法。

    运行时 self 是 SupervisorState 实例。
    """

    # ---- 基础工具 ----

    def _default_entry_node(self) -> str:
        cfg = load_runtime_config(self.workspace_root)
        return get_str(cfg, "shell.entry_node_id", "bootstrap.shell_orchestrator").strip() or "bootstrap.shell_orchestrator"

    def _task_terminal(self, task: Task) -> bool:
        return task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}

    def _to_positive_int(self, value: Any) -> int | None:
        try:
            v = int(value)
            return v if v > 0 else None
        except Exception:
            return None

    # ---- 内部 task 创建 ----

    def _event_task_snapshot(self, event_type: str, task: Task, *, component: str = "supervisor") -> None:
        self.eventlog.append(
            session_id=task.session_id,
            component=component,
            type_=event_type,
            payload=self._event_payload_for_task(task),
        )

    def _event_payload_for_task(self, task: Task) -> dict[str, Any]:
        """Build a task snapshot payload safe for long-term event storage."""
        payload = task.model_dump(mode="json")
        result = payload.get("result")
        if isinstance(result, dict) and "dispatch_input" in result:
            # Why: dispatch_input can contain a full child prompt or compacted
            # conversation text, and the same stale result used to be repeated in
            # task_started/task_suspended snapshots. How: copy the serialized
            # result and remove only dispatch_input from the event payload while
            # leaving the live Task object untouched. Purpose: reduce future
            # events.jsonl growth and replay memory without changing routing,
            # which reads task.result before or outside event serialization.
            slim_result = dict(result)
            slim_result.pop("dispatch_input", None)
            slim_result["dispatch_input_omitted"] = True
            payload["result"] = slim_result
        return payload

    def _apply_task_snapshot(self, payload: dict[str, Any]) -> None:
        try:
            t = Task.model_validate(payload)
        except Exception:
            return
        existed = t.task_id in self.tasks
        self.tasks[t.task_id] = t
        if not existed:
            self._task_order.append(t.task_id)
        cur = self.session_generations.get(t.session_id, 0)
        if t.session_generation > cur:
            self.session_generations[t.session_id] = t.session_generation

    def _apply_cancel_requested(self, session_id: str, payload: dict[str, Any]) -> None:
        sid = str(session_id or payload.get("session_id") or "")
        if not sid:
            return
        self._cancelled_sessions.add(sid)
        gen = self._to_positive_int(payload.get("session_generation"))
        if gen is not None and gen > self.session_generations.get(sid, 0):
            self.session_generations[sid] = gen

    def _create_task_locked(
        self,
        *,
        session_id: str,
        session_generation: int,
        kind: TaskKind,
        node_id: str | None = None,
        tool_name: str | None = None,
        input_data: dict[str, Any] | None = None,
        continuation: dict[str, Any] | None = None,
        source_inbound_seq: int | None = None,
        caller_task_id: str | None = None,
        batch_id: str | None = None,
        batch_index: int = 0,
    ) -> Task:
        now = _now()
        task = Task(
            task_id=str(uuid.uuid4()),
            session_id=session_id,
            session_generation=session_generation,
            kind=kind,
            node_id=node_id,
            tool_name=tool_name,
            input=dict(input_data or {}),
            continuation=dict(continuation or {}),
            source_inbound_seq=source_inbound_seq,
            caller_task_id=caller_task_id,
            batch_id=batch_id,
            batch_index=batch_index,
            status=TaskStatus.pending,
            cancel_requested=False,
            worker_id=None,
            created_at=now,
            updated_at=now,
            lease_expires_at=None,
            result={},
        )
        self.tasks[task.task_id] = task
        self._task_order.append(task.task_id)
        self._event_task_snapshot("task_created", task)
        return task

    def _cancel_session_tasks_locked(self, session_id: str) -> None:
        now = _now()
        for task in self.tasks.values():
            if task.session_id != session_id or self._task_terminal(task):
                continue
            if task.status == TaskStatus.pending:
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = now
                task.lease_expires_at = None
                # [Fork/Merge 2026-05-12] pending 入口分支被 session cancel 直接终结时也要清理。
                # 原因：此路径不会经过 complete_task/task_router。做法：调用幂等 finalize。
                # 目的：避免未启动分支在主 session 取消后遗留 JSONL 与映射。
                self._finalize_branch_task_locked(task, merge=True)
                self._event_task_snapshot("task_cancelled", task)
            elif task.status == TaskStatus.suspended:
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.waiting_for_task_id = None
                task.updated_at = now
                self._finalize_branch_task_locked(task, merge=True)
                self._event_task_snapshot("task_cancelled", task)
            elif task.status == TaskStatus.running and not task.cancel_requested:
                task.cancel_requested = True
                task.updated_at = now
                self._event_task_snapshot("task_cancel_requested", task)

    def _find_last_context_ref_locked(self, session_id: str, node_id: str, *, context_key: str | None = None) -> str:
        """查找同一 session 中指定 node_id 最近一次完成的 task 的 context_ref。

        如果提供了 context_key，则按 (session_id, node_id, context_key) 精确匹配，
        用于区分同一 node_id 的多个并发实例。否则按 (session_id, node_id) 匹配（默认行为）。

        DEPRECATED — Child Session 隔离（Phase D）
        新 dispatch 路径使用 get_or_create_child_session() 替代本方法。
        当前仍被 task_router 的兼容期 fallback 代码调用，待 child session
        稳定运行后可删除。
        """
        for tid in reversed(self._task_order):
            t = self.tasks.get(tid)
            if (t is not None
                and t.session_id == session_id
                and t.node_id == node_id
                and t.status == TaskStatus.completed
                and t.result.get("context_ref")):
                if context_key:
                    if t.input.get("_context_key") == context_key:
                        return str(t.result["context_ref"])
                else:
                    return str(t.result["context_ref"])
        return ""

    def _create_entry_task_for_inbound_locked(self, *, inbound_seq: int, session_id: str, payload: dict[str, Any]) -> Task | None:
        text = str(payload.get("text") or "").strip()
        has_attachments = isinstance(payload.get("attachments"), list) and bool(payload.get("attachments"))
        if not text and not has_attachments:
            return None
        # 优先级: session 覆盖（AI switch） > 前端指定 > session 记录的 > 全局默认
        default_node = self._default_entry_node()
        session_override = self.session_entry_overrides.get(session_id, "").strip()
        session_info = self.sessions.get(session_id)
        # Why: after a supervisor restart, session_entry_overrides is deliberately
        # cleared. How: read the persisted SessionInfo.entry_node_id before using
        # the global runtime default. Purpose: callbacks keep the node binding
        # selected when the session was first routed or switched.
        session_recorded = (session_info.entry_node_id if session_info else "").strip() if session_info else ""
        payload_entry = str(payload.get("entry_node_id") or "").strip()
        entry_node = (
            session_override
            or payload_entry
            or session_recorded
            or default_node
        )
        if session_info and not session_info.entry_node_id:
            # Why: newly-created sessions may not yet know their frontend-selected
            # entry node. How: once an inbound is actually routed, copy the node
            # into memory and sessions.json. Purpose: later restarts can reproduce
            # the same route even if the callback payload has no entry_node_id.
            session_info.entry_node_id = entry_node
            self._session_store.update_entry_node(session_id, entry_node)
        # Record the actual entry node used for this session (for getActiveNode API)
        self.session_last_entry_node[session_id] = entry_node
        generation = self._current_session_generation_locked(session_id) or 1
        if not self.session_generations.get(session_id):
            self.session_generations[session_id] = generation
        self._cancelled_sessions.discard(session_id)
        # [Fork/Merge 2026-05-12] 每条 inbound 都创建独立入口分支。
        # 原因：同一主 session 的新消息不再抢占旧入口 task，而是并发运行在各自
        # branch session 上。做法：在 supervisor 持锁期间 fork ConversationStore，
        # 并把 fork 基准写入 task.input。目的：task 结束时能按 base_count merge 回主 session。
        branch_session_id, fork_meta = self._create_entry_branch_locked(session_id, inbound_seq)
        branch_base_count = int(fork_meta.get("base_count") or 0)
        self._cancelled_sessions.discard(branch_session_id)
        # 收集当前活跃 task 摘要，注入给入口节点 AI 判断
        active_tasks_summary = self._active_tasks_summary_locked(session_id)
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
        use_context = bool(payload.get("use_context", True))
        # Step 2（2026-04-16）：主节点切 ConversationStore。
        # flag 开启时不再注入 context_ref，engine 侧 runner.py 会从 data/conversations/{session_id}.jsonl
        # 加载 history，不再依赖 node_contexts snapshot。
        # flag 关闭时走旧路径，调用 _find_last_context_ref_locked 注入 snapshot ref。
        # _find_last_context_ref_locked 本体保留（兼容期回退路径仍需要）。
        cfg = load_runtime_config(self.workspace_root)
        main_use_conv = bool(cfg.get("engine", {}).get("child_session", {}).get("main_session_enabled", True))
        if main_use_conv:
            last_ctx_ref = ""
        else:
            # 查找入口节点上一轮的 context_ref，使对话上下文（含工具调用）跨轮次连续
            last_ctx_ref = self._find_last_context_ref_locked(session_id, entry_node)
        task = self._create_task_locked(
            session_id=branch_session_id,
            session_generation=generation,
            kind=TaskKind.node,
            node_id=entry_node,
            input_data={
                "instruction": text,
                "context_ref": last_ctx_ref,
                "switched_from": default_node if session_override else "",
                "resume_data": {},
                "use_context": use_context,
                "_system_task": bool(payload.get("_system_task", False)),
                "active_tasks_summary": active_tasks_summary,
                "attachments": attachments or [],
                # [Fork/Merge 2026-05-12] 入口分支元数据随 task 持久化。
                # 原因：完成路由只拿到 Task 快照，不能依赖易失内存索引判断 merge 目标。
                # 做法：记录 parent、branch 与 fork base_count/base_last_id。目的：finish、fail、
                # cancel 终态都能独立完成 merge，并保持没有这些字段的旧 task 正常工作。
                "parent_session_id": session_id,
                "branch_session_id": branch_session_id,
                "base_count": branch_base_count,
                "base_last_id": str(fork_meta.get("base_last_id") or ""),
                "fork_copied": int(fork_meta.get("copied") or 0),
                "task_context": {
                    "conversation_key": str(payload.get("conversation_key") or ""),
                    "channel": str(payload.get("channel") or ""),
                    "message_id": str(payload.get("message_id") or ""),
                    "entry_node_id": entry_node,
                    "session_id": branch_session_id,
                    "parent_session_id": session_id,
                    "branch_session_id": branch_session_id,
                    "base_count": branch_base_count,
                    "session_generation": generation,
                    "is_system_task": bool(payload.get("_system_task", False)),
                    "switched_from": default_node if session_override else "",
                    "use_context": use_context,
                },
            },
            continuation={},
            source_inbound_seq=inbound_seq,
            caller_task_id=None,
        )
        # Emit transient inbound_accepted so Bot can advance watermark
        self.eventlog.append(
            session_id=session_id,
            component="supervisor",
            type_="inbound_accepted",
            payload={
                "inbound_seq": inbound_seq,
                "task_id": task.task_id,
                "session_id": session_id,
                "branch_session_id": branch_session_id,
                "conversation_key": str(payload.get("conversation_key") or ""),
            },
            transient=True,
        )
        return task

    def _active_tasks_summary_locked(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session 中活跃（pending/running）task 的摘要列表。"""
        result: list[dict[str, Any]] = []
        session_ids = {session_id, *self._entry_branch_ids_for_parent_locked(session_id)}
        for task in self.tasks.values():
            if task.session_id not in session_ids:
                continue
            if self._task_terminal(task):
                continue
            result.append({
                "task_id": task.task_id,
                "kind": task.kind.value,
                "node_id": task.node_id or "",
                "tool_name": task.tool_name or "",
                "status": task.status.value,
                "instruction": str(task.input.get("instruction") or "")[:200],
                "created_at": task.created_at.isoformat() if task.created_at else "",
                # [Fork/Merge 2026-05-12] 摘要暴露分支来源。
                # 原因：主 session 可能同时有多个 branch 任务，入口节点需要区分它们。
                # 做法：优先读取 task.input.branch_session_id，否则用 task.session_id。
                # 目的：保留旧摘要字段的同时支持并发分支可观测性。
                "branch_session_id": str(task.input.get("branch_session_id") or (task.session_id if task.session_id != session_id else "")),
                "parent_session_id": str(task.input.get("parent_session_id") or session_id),
                "source_inbound_seq": task.source_inbound_seq,
            })
        return result

    def cancel_active_tasks(self, session_id: str, *, exclude_task_id: str | None = None) -> dict[str, Any]:
        sid = (session_id or "").strip()
        if not sid:
            return {"ok": False}
        with self._lock:
            keep_ids: set[str] = set()
            tid = (exclude_task_id or "").strip()
            while tid:
                keep_ids.add(tid)
                t = self.tasks.get(tid)
                if t is None:
                    break
                tid = (t.caller_task_id or "").strip()

            now = _now()
            count = 0
            session_ids = {sid, *self._entry_branch_ids_for_parent_locked(sid)}
            for task in self.tasks.values():
                if task.session_id not in session_ids or self._task_terminal(task) or task.task_id in keep_ids:
                    continue
                if task.status == TaskStatus.pending:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    # [Fork/Merge 2026-05-12] 本地取消直接置终态时同步收束入口分支。
                    # 原因：pending 任务不会再由 engine 上报完成。做法：调用幂等 finalize。
                    # 目的：让 cancel_active_tasks 与 finish/fail 路径一样清理分支。
                    self._finalize_branch_task_locked(task, merge=True)
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.suspended:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.waiting_for_task_id = None
                    task.updated_at = now
                    self._finalize_branch_task_locked(task, merge=True)
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.running:
                    if task.cancel_requested:
                        # 已标记取消但 worker 未响应；如果 lease 已过期，直接终结
                        if task.lease_expires_at and task.lease_expires_at < now:
                            task.status = TaskStatus.cancelled
                            task.lease_expires_at = None
                            task.updated_at = now
                            self._finalize_branch_task_locked(task, merge=True)
                            self._event_task_snapshot("task_cancelled", task)
                            count += 1
                    else:
                        task.cancel_requested = True
                        task.updated_at = now
                        # 如果 lease 已过期，worker 大概率已死，直接终结
                        if task.lease_expires_at and task.lease_expires_at < now:
                            task.status = TaskStatus.cancelled
                            task.lease_expires_at = None
                            self._finalize_branch_task_locked(task, merge=True)
                            self._event_task_snapshot("task_cancelled", task)
                        else:
                            self._event_task_snapshot("task_cancel_requested", task)
                        count += 1
            return {"ok": True, "cancelled_count": count}

    def cancel_single_task(self, task_id: str) -> dict[str, Any]:
        """取消单个 task 及其所有子任务链。"""
        tid = (task_id or "").strip()
        if not tid:
            return {"ok": False, "error": "task_id required"}
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return {"ok": False, "error": "task not found"}
            if self._task_terminal(task):
                return {"ok": True, "cancelled_count": 0, "already_terminal": True}

            # 收集目标 task 及其所有后代子任务（BFS）
            to_cancel: set[str] = {tid}
            queue = [tid]
            while queue:
                parent_id = queue.pop(0)
                for t in self.tasks.values():
                    if t.caller_task_id == parent_id and not self._task_terminal(t) and t.task_id not in to_cancel:
                        to_cancel.add(t.task_id)
                        queue.append(t.task_id)

            now = _now()
            count = 0
            for cancel_tid in to_cancel:
                t = self.tasks.get(cancel_tid)
                if t is None or self._task_terminal(t):
                    continue
                if t.status == TaskStatus.pending:
                    t.cancel_requested = True
                    t.status = TaskStatus.cancelled
                    t.updated_at = now
                    t.lease_expires_at = None
                    # [Fork/Merge 2026-05-12] 单任务取消可能直接终结入口分支。
                    # 原因：pending 分支不会再进入完成路由。做法：在记录取消事件前 finalize。
                    # 目的：保证显式 task cancel 与自然完成一样回收 branch。
                    self._finalize_branch_task_locked(t, merge=True)
                    self._event_task_snapshot("task_cancelled", t)
                    count += 1
                elif t.status == TaskStatus.suspended:
                    t.cancel_requested = True
                    t.status = TaskStatus.cancelled
                    t.waiting_for_task_id = None
                    t.updated_at = now
                    self._finalize_branch_task_locked(t, merge=True)
                    self._event_task_snapshot("task_cancelled", t)
                    count += 1
                elif t.status == TaskStatus.running:
                    if t.cancel_requested:
                        # 已标记取消但 worker 未响应；如果 lease 已过期或从未获得 lease，直接终结
                        # fix: lease_expires_at 为 None 时也视为 worker 已死，避免永久僵尸
                        if not t.lease_expires_at or t.lease_expires_at < now:
                            t.status = TaskStatus.cancelled
                            t.lease_expires_at = None
                            t.updated_at = now
                            self._finalize_branch_task_locked(t, merge=True)
                            self._event_task_snapshot("task_cancelled", t)
                            count += 1
                    else:
                        t.cancel_requested = True
                        t.updated_at = now
                        # 如果 lease 已过期或从未获得 lease，worker 大概率已死，直接终结
                        # fix: lease_expires_at 为 None 时同样强制终结，防止无 lease 任务成为永久僵尸
                        if not t.lease_expires_at or t.lease_expires_at < now:
                            t.status = TaskStatus.cancelled
                            t.lease_expires_at = None
                            self._finalize_branch_task_locked(t, merge=True)
                            self._event_task_snapshot("task_cancelled", t)
                        else:
                            self._event_task_snapshot("task_cancel_requested", t)
                        count += 1
            return {"ok": True, "cancelled_count": count}

    # ---- orphaned task cleanup ----

    def cancel_orphaned_tasks(self) -> int:
        """Cancel all non-terminal tasks after a full restart.

        After a full restart, engine workers are gone and LLM context is
        not recoverable, so requeue is meaningless.  Call once during
        startup after rebuild_from_events.
        """
        with self._lock:
            now = _now()
            count = 0
            for task in self.tasks.values():
                if self._task_terminal(task):
                    continue
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = now
                task.lease_expires_at = None
                if task.waiting_for_task_id:
                    task.waiting_for_task_id = None
                self._event_task_snapshot("task_cancelled", task)
                count += 1
            return count

    # ---- preempt ----

    def preempt_task(self, task_id: str, message: str = "", attachments: list | None = None) -> bool:
        """标记单个 task 为 preempt_requested。不影响 session 状态。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status not in (TaskStatus.running, TaskStatus.pending):
                return False
            task.preempt_requested = True
            task.preempt_message = message
            if attachments:
                task.preempt_attachments = attachments
            # [Fork/Merge 2026-05-17] Why: a task can be running inside a
            # temporary entry branch, but session event consumers watch the
            # durable parent session. How: use the task route helper when the
            # supervisor includes it, and keep the runtime branch id in the
            # payload for diagnostics. Purpose: accepted preempt requests remain
            # visible without changing the task-local preempt flag semantics.
            route_for_task = getattr(self, "_route_session_id_for_task_locked", None)
            route_session_id = (
                str(route_for_task(task) or "").strip()
                if callable(route_for_task)
                else ""
            ) or task.session_id
            payload = {"task_id": task_id, "session_id": route_session_id, "has_message": bool(message)}
            if route_session_id != task.session_id:
                payload["runtime_session_id"] = task.session_id
            self.eventlog.append(
                session_id=route_session_id,
                component="supervisor",
                type_="preempt_requested",
                payload=payload,
                transient=True,
            )
            return True

    def consume_preempt_message(self, task_id: str) -> dict:
        """消费 preempt message，返回 {message, attachments} 并清空。同时重置 preempt_requested。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return {"message": "", "attachments": []}
            msg = task.preempt_message
            atts = list(task.preempt_attachments)
            task.preempt_message = ""
            task.preempt_attachments = []
            task.preempt_requested = False
            return {"message": msg, "attachments": atts}

    def is_task_preempted(self, task_id: str) -> dict:
        """查询 task 的 preempt 状态，包含 message。"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return {"preempted": False, "message": "", "attachments": []}
            return {
                "preempted": task.preempt_requested,
                "message": task.preempt_message,
                "attachments": list(task.preempt_attachments),
            }

    def renew_lease(self, task_id: str, worker_id: str, lease_sec: float = 120.0) -> bool:
        """续租 task 的 lease，延长 lease_expires_at。仅 status=running 且 worker_id 匹配时生效。"""
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        if not tid or not wid:
            return False
        with self._lock:
            task = self.tasks.get(tid)
            if task is None or task.status != TaskStatus.running:
                return False
            if task.worker_id != wid:
                return False
            lease_val = max(10.0, min(lease_sec, 600.0))
            task.lease_expires_at = _now() + timedelta(seconds=lease_val)
            return True

    # ---- task 队列（公开方法） ----

    def assign_next_task(self, *, worker_id: str, lease_sec: float = 120.0) -> dict[str, Any] | None:
        wid = (worker_id or "").strip()
        if not wid:
            return None
        with self._lock:
            now = _now()
            lease_val = max(10.0, min(lease_sec, 600.0))
            for tid in self._task_order:
                task = self.tasks.get(tid)
                if task is None or task.status != TaskStatus.pending:
                    continue

                current_gen = self._current_session_generation_locked(task.session_id)
                if task.cancel_requested or (current_gen and task.session_generation != current_gen):
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_cancelled", task)
                    continue

                if task.lease_expires_at and task.lease_expires_at > now:
                    continue

                if task.worker_id and task.worker_id != wid:
                    if task.lease_expires_at and task.lease_expires_at > now:
                        continue
                    # lease expired – 重新排队
                    task.status = TaskStatus.pending
                    task.worker_id = None
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_requeued", task)

                task.status = TaskStatus.running
                task.worker_id = wid
                task.updated_at = now
                task.lease_expires_at = now + timedelta(seconds=lease_val)
                self._event_task_snapshot("task_started", task)
                return task.model_dump(mode="json")
            return None

    def complete_task(self, *, task_id: str, worker_id: str, result: dict[str, Any]) -> Task | None:
        tid = (task_id or "").strip()
        wid = (worker_id or "").strip()
        if not tid or not wid:
            return None
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return None
            if self._task_terminal(task):
                return task
            if task.worker_id and task.worker_id != wid and task.lease_expires_at and task.lease_expires_at > _now():
                return None

            current_gen = self._current_session_generation_locked(task.session_id)
            if task.cancel_requested or (current_gen and task.session_generation != current_gen):
                task.cancel_requested = True
                task.status = TaskStatus.cancelled
                task.updated_at = _now()
                task.lease_expires_at = None
                task.result = dict(result or {})
                # [Fork/Merge 2026-05-12] 运行中的入口分支被取消时仍需收束分支。
                # 原因：cancel_requested 分支原先直接 return，不会进入 task_router 的终态路由。
                # 做法：在写 task_cancelled 事件前调用分支 finalize，执行 merge 与 cleanup。
                # 目的：finish/fail/cancel 三类终态都能把分支历史回写主 session。
                self._finalize_branch_task_locked(task, merge=True)
                self._event_task_snapshot("task_cancelled", task, component="engine")
                return task

            task.result = dict(result or {})
            task.updated_at = _now()
            task.lease_expires_at = None

            act = str(task.result.get("action") or "").strip()
            if act == "cancelled":
                task.status = TaskStatus.cancelled
            elif act == "fail":
                task.status = TaskStatus.failed
            else:
                task.status = TaskStatus.completed

            self._event_task_snapshot("task_completed", task, component="engine")
            self._route_completed_task_locked(task)
            return task
