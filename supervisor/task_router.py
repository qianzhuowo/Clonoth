"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import uuid
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config

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
        # ---- 批量 task：优先走统一批量收集 ----
        if task.batch_id:
            self._try_complete_batch_locked(task)
            # 后置记忆提取不对 batch 子 task 触发
            return

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

        # 后置触发：检查是否需要创建记忆提取任务
        self._maybe_trigger_memory_extract_locked(task)

    # ------------------------------------------------------------------ #
    #  dispatch
    # ------------------------------------------------------------------ #

    def _route_dispatch_locked(self, task: Task, action: dict[str, Any]) -> None:
        target = str(action.get("target_node") or "").strip()
        dispatch_input = action.get("dispatch_input") if isinstance(action.get("dispatch_input"), dict) else {}
        dispatch_batch = action.get("dispatch_batch")

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
                    child = self._create_task_locked(
                        session_id=task.session_id,
                        session_generation=task.session_generation,
                        kind=TaskKind.node,
                        node_id=item_target,
                        input_data={
                            "instruction": str(item.get("instruction") or "").strip(),
                        },
                        continuation={},
                        source_inbound_seq=task.source_inbound_seq,
                        caller_task_id=task.task_id,
                        batch_id=batch_id,
                        batch_index=idx,
                    )
                created_child_ids.append(child.task_id)

        # ---- 单节点委派 ----
        if not created_child_ids and target:
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

    def _route_ask_locked(self, task: Task, action: dict[str, Any]) -> None:
        result = action.get("result") if isinstance(action.get("result"), dict) else {}
        self._resume_caller_or_output_locked(task, {
            "type": "child_ask",
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

        # 没有 suspended caller → 输出给用户
        # 系统内部任务（如记忆提取）静默完成，不输出给用户
        if task.input.get("_system_task"):
            return
        text = str(fallback_result.get("text") or fallback_result.get("summary") or "").strip()
        atts = fallback_result.get("attachments") if isinstance(fallback_result.get("attachments"), list) else None
        if text or atts:
            self.append_outbound_message(
                session_id=task.session_id, text=text,
                attachments=atts, source_inbound_seq=task.source_inbound_seq,
            )


    # ------------------------------------------------------------------ #
    #  后置记忆提取
    # ------------------------------------------------------------------ #

    def _maybe_trigger_memory_extract_locked(self, task: Task) -> None:
        """入口节点 finish 后，检查是否需要创建记忆提取后置任务。"""
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

        # 构建对话摘要作为 instruction
        transcript = self._format_transcript_for_extract(msgs)
        if not transcript.strip():
            return

        # 更新游标
        self._memory_extract_msg_counts[task.session_id] = current_count

        # 创建后置任务
        extractor_node = get_str(runtime_cfg, "memory.auto_extract.node_id", "system.memory_extractor").strip()
        self._create_task_locked(
            session_id=task.session_id,
            session_generation=task.session_generation,
            kind=TaskKind.node,
            node_id=extractor_node,
            input_data={
                "instruction": transcript,
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
