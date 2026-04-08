"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import uuid
from typing import Any

from ._helpers import _now
from .types import Task, TaskKind, TaskStatus


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
        action = task.result or {}
        act = str(action.get("action") or "").strip()
        if act == "dispatch":
            self._route_dispatch_locked(task, action)
        elif act == "finish":
            self._route_finish_locked(task, action)
        elif act == "ask":
            self._route_ask_locked(task, action)
        elif act == "fail":
            self._route_fail_locked(task, action)
        # cancelled → 不做路由

    # ---- dispatch ----

    def _route_dispatch_locked(self, task: Task, action: dict[str, Any]) -> None:
        target = str(action.get("target_node") or "").strip()
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        if not target:
            return

        context_ref = str(action.get("context_ref") or "").strip()
        created_child_ids: list[str] = []

        # ---- 多工具批量 ----
        if target == "__tool_batch__":
            tool_calls = dispatch_input.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                batch_id = str(uuid.uuid4())
                for idx, tc in enumerate(tool_calls):
                    name = str(tc.get("name") or "").strip()
                    if not name:
                        continue
                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.tool,
                        tool_name=name,
                        input_data={
                            "arguments": tc.get("arguments", {}),
                            "call_id": str(tc.get("id") or ""),
                            "batch_id": batch_id,
                            "tool_index": idx,
                        },
                        continuation={"batch_id": batch_id},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                    )
                    created_child_ids.append(child.task_id)

        # ---- 单工具 ----
        if dispatch_input.get("tool_call_id") is not None or dispatch_input.get("arguments") is not None:
            batch_id = str(uuid.uuid4())
            child = self._create_task_locked(
                session_id=task.session_id,
                session_generation=task.session_generation,
                kind=TaskKind.tool,
                tool_name=target,
                input_data={
                    "arguments": dispatch_input.get("arguments", {}),
                    "call_id": str(dispatch_input.get("tool_call_id") or ""),
                    "batch_id": batch_id,
                    "tool_index": 0,
                },
                continuation={"batch_id": batch_id},
                source_inbound_seq=task.source_inbound_seq,
                caller_task_id=task.task_id,
            )
            created_child_ids.append(child.task_id)

        # ---- AI 节点委派 ----
        if not created_child_ids:
            # 查找目标节点上一轮的 context_ref，使对话上下文（含工具调用）跨轮次连续
            child_ctx_ref = self._find_last_context_ref_locked(task.session_id, target)
            child = self._create_task_locked(
                session_id=task.session_id,
                session_generation=task.session_generation,
                kind=TaskKind.node,
                node_id=target,
                input_data={
                    "instruction": str(dispatch_input.get("instruction") or "").strip(),
                    "context_ref": child_ctx_ref,
                },
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

    # ---- finish ----

    def _route_finish_locked(self, task: Task, action: dict[str, Any]) -> None:
        # 如果是批量工具中的一个，走批量收集逻辑
        if task.kind == TaskKind.tool:
            batch_id = str(task.input.get("batch_id") or task.continuation.get("batch_id") or "").strip()
            if batch_id:
                self._route_tool_batch_return_locked(task, batch_id)
                return

        result = action.get("result") if isinstance(action.get("result"), dict) else {}
        self._resume_caller_or_output_locked(task, {
            "type": "child_result",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
        }, result)

    # ---- ask ----

    def _route_ask_locked(self, task: Task, action: dict[str, Any]) -> None:
        result = action.get("result") if isinstance(action.get("result"), dict) else {}
        self._resume_caller_or_output_locked(task, {
            "type": "child_ask",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "result": result,
        }, result)

    def _route_tool_batch_return_locked(self, task: Task, batch_id: str) -> None:
        """等待同批次所有工具完成后，恢复 caller 节点。"""
        siblings: list[Task] = []
        for t in self.tasks.values():
            if t.session_id != task.session_id or t.session_generation != task.session_generation or t.kind != TaskKind.tool:
                continue
            tb = str(t.input.get("batch_id") or t.continuation.get("batch_id") or "").strip()
            if tb == batch_id:
                siblings.append(t)
        if not siblings:
            return
        if any(not self._task_terminal(t) for t in siblings):
            return

        # 防止重复创建 resume task
        resume_key = f"tool_batch:{batch_id}"
        for t in self.tasks.values():
            if t.session_id != task.session_id or t.session_generation != task.session_generation or t.kind != TaskKind.node:
                continue
            if str(t.input.get("resume_key") or "") == resume_key:
                return

        # 收集结果
        siblings.sort(key=lambda x: int(x.input.get("tool_index", 0) or 0))
        entries: list[dict[str, Any]] = []
        for t in siblings:
            tr = t.result.get("result") if isinstance(t.result.get("result"), dict) else {}
            entries.append({
                "name": str(t.tool_name or tr.get("tool_name") or ""),
                "args": dict(tr.get("arguments") or t.input.get("arguments") or {}),
                "format": str(tr.get("raw_format") or tr.get("format") or "json"),
                "raw_inline": str(tr.get("raw_inline") or tr.get("text") or ""),
                "truncated": bool(tr.get("truncated", False)),
                "ref": str(tr.get("ref") or ""),
                "summary": str(t.result.get("summary") or tr.get("summary") or ""),
                "attachments": list(tr.get("attachments") or []) if isinstance(tr.get("attachments"), list) else [],
            })

        resume_data = {"type": "tool_results", "tool_results": entries}
        self._resume_caller_or_output_locked(task, resume_data, {"summary": f"批量工具完成 ({len(entries)} 个结果)"})

    # ---- fail ----

    def _route_fail_locked(self, task: Task, action: dict[str, Any]) -> None:
        error = str(action.get("error") or "未知错误").strip()
        self._resume_caller_or_output_locked(task, {
            "type": "child_failed",
            "child_node_id": str(task.node_id or task.tool_name or ""),
            "error": error,
        }, {"text": f"[错误] {error}"})

    # ---- 公共：恢复 caller 或输出给用户 ----

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
                self._event_task_snapshot("task_resumed", caller)
                return

        # 没有 suspended caller → 输出给用户
        text = str(fallback_result.get("text") or fallback_result.get("summary") or "").strip()
        atts = fallback_result.get("attachments") if isinstance(fallback_result.get("attachments"), list) else None
        if text or atts:
            self.append_outbound_message(
                session_id=task.session_id, text=text,
                attachments=atts, source_inbound_seq=task.source_inbound_seq,
            )
