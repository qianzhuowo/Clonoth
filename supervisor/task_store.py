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
            payload=task.model_dump(mode="json"),
        )

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
                self._event_task_snapshot("task_cancelled", task)
            elif task.status == TaskStatus.running and not task.cancel_requested:
                task.cancel_requested = True
                task.updated_at = now
                self._event_task_snapshot("task_cancel_requested", task)

    def _find_last_context_ref_locked(self, session_id: str, node_id: str) -> str:
        """查找同一 session 中指定 node_id 最近一次完成的 task 的 context_ref。

        用于在新任务中传递上下文快照引用，使 AI 能看到上一轮的完整对话（包括工具调用）。
        """
        for tid in reversed(self._task_order):
            t = self.tasks.get(tid)
            if (t is not None
                and t.session_id == session_id
                and t.node_id == node_id
                and t.status == TaskStatus.completed
                and t.result.get("context_ref")):
                return str(t.result["context_ref"])
        return ""

    def _create_entry_task_for_inbound_locked(self, *, inbound_seq: int, session_id: str, payload: dict[str, Any]) -> Task | None:
        text = str(payload.get("text") or "").strip()
        has_attachments = isinstance(payload.get("attachments"), list) and bool(payload.get("attachments"))
        if not text and not has_attachments:
            return None
        entry_node = str(payload.get("entry_node_id") or "").strip() or self._default_entry_node()
        generation = self._current_session_generation_locked(session_id) or 1
        if not self.session_generations.get(session_id):
            self.session_generations[session_id] = generation
        self._cancelled_sessions.discard(session_id)
        # 收集当前活跃 task 摘要，注入给入口节点 AI 判断
        active_tasks_summary = self._active_tasks_summary_locked(session_id)
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
        use_context = bool(payload.get("use_context", True))
        # 查找入口节点上一轮的 context_ref，使对话上下文（含工具调用）跨轮次连续
        last_ctx_ref = self._find_last_context_ref_locked(session_id, entry_node)
        return self._create_task_locked(
            session_id=session_id,
            session_generation=generation,
            kind=TaskKind.node,
            node_id=entry_node,
            input_data={
                "instruction": text,
                "context_ref": last_ctx_ref,
                "resume_data": {},
                "use_context": use_context,
                "active_tasks_summary": active_tasks_summary,
                "attachments": attachments or [],
            },
            continuation={},
            source_inbound_seq=inbound_seq,
            caller_task_id=None,
        )

    def _active_tasks_summary_locked(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session 中活跃（pending/running）task 的摘要列表。"""
        result: list[dict[str, Any]] = []
        for task in self.tasks.values():
            if task.session_id != session_id:
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
            for task in self.tasks.values():
                if task.session_id != sid or self._task_terminal(task) or task.task_id in keep_ids:
                    continue
                if task.status == TaskStatus.pending:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.updated_at = now
                    task.lease_expires_at = None
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.suspended:
                    task.cancel_requested = True
                    task.status = TaskStatus.cancelled
                    task.waiting_for_task_id = None
                    task.updated_at = now
                    self._event_task_snapshot("task_cancelled", task)
                    count += 1
                elif task.status == TaskStatus.running and not task.cancel_requested:
                    task.cancel_requested = True
                    task.updated_at = now
                    self._event_task_snapshot("task_cancel_requested", task)
                    count += 1
            return {"ok": True, "cancelled_count": count}

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
