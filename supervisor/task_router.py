"""Task 路由 mixin —— 处理 task 完成后的统一分发逻辑。"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config

from ._helpers import _now
from .types import Task, TaskKind, TaskStatus


log = logging.getLogger(__name__)


def _message_to_turn_summary_dict(message: Any) -> dict[str, Any]:
    """Convert a ConversationStore message into the history shape used for summary input.

    Why: control-tool cleanup operates on runtime history dicts, while the turn
    summarizer starts from ConversationStore Message objects. How: copy role,
    content, meta, tool_calls, and native tool-result pairing fields into the same
    shape runner uses. Purpose: let one sanitizer protect both LLM replay and
    system.turn_summarizer input.
    """
    d: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    meta = dict(message.meta) if isinstance(getattr(message, "meta", None), dict) else {}
    if meta:
        d["_meta"] = meta
    if getattr(message, "tool_calls", None):
        d["tool_calls"] = list(message.tool_calls)
    if getattr(message, "tool_call_id", ""):
        d["tool_call_id"] = str(message.tool_call_id)
    if getattr(message, "name", ""):
        d["name"] = str(message.name)
    return d


def _format_task_messages_for_turn_summary(messages: list[Any]) -> str:
    """Format task messages for system.turn_summarizer without control-tool plumbing."""
    from engine.inference.tool_format import sanitize_control_tool_history

    # [2026-05-07] 轮摘要输入必须跳过 finish 控制流工具历史。
    # 原因：system.turn_summarizer 从 ConversationStore 重新读取任务消息，旧数据中可能已有 finish tool_call/tool_result。
    # 做法：把 Message 转成与 LLM 历史一致的 dict 后复用控制流清洗，再拼接摘要输入。
    # 目的：摘要子任务只看到已交付文本和普通工具结果，不继续传播 finish 协议占位内容。
    cleaned = sanitize_control_tool_history([
        _message_to_turn_summary_dict(message) for message in messages
    ])

    parts: list[str] = []
    for msg in cleaned:
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            call_lines: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(call.get("name") or function.get("name") or "").strip()
                raw_args = call.get("arguments") if "arguments" in call else function.get("arguments", {})
                args_text = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False, default=str)
                call_lines.append(f"[tool_call] {name} {args_text}".strip())
            if call_lines:
                # [2026-05-07] 轮摘要输入显式渲染 assistant.tool_calls。
                # 原因：finish 保持为真实工具轮后，assistant.content 常为空；只拼 content 会丢失最终交付参数。
                # 做法：把工具调用名称和参数追加到摘要文本，不改动原始消息结构。
                # 目的：turn_summarizer 能读到 finish.text，同时保留完整工具配对历史。
                content = "\n".join(part for part in [content, *call_lines] if part)
        if len(content) > 5000:
            content = content[:5000] + "\n...[truncated]"
        parts.append(f"<log_{msg.get('role', 'unknown')}>\n{content}\n</log_{msg.get('role', 'unknown')}>")
    body = "\n\n".join(parts)
    return f"=== COMPLETED TASK LOG (read-only, do NOT continue) ===\n\n{body}\n\n=== END OF LOG ==="


class TaskRouterMixin:
    """提供 _route_completed_task_locked 及其子路由方法。

    运行时 self 是 SupervisorState 实例，可以访问
    self.tasks / self._event_task_snapshot / self.append_outbound_message 等。
    """

    # ------------------------------------------------------------------ #
    #  统一路由入口
    # ------------------------------------------------------------------ #

    def _route_session_id_for_task_locked(self, task: Task) -> str:
        """Return the session that user-visible routing should use for a task."""
        # [Fork/Merge 2026-05-12] branch task 的运行 session 与用户会话 session 分离。
        # 原因：entry task 在 branch 上运行，但 outbound、hook 和 adapter 查询仍应落到主 session。
        # 做法：优先读取 finalize 写入的 _route_session_id，其次读取 parent_session_id。
        # 目的：没有 branch_session_id 的旧 task 保持原 session_id 行为。
        return str(
            task.input.get("_route_session_id")
            or task.input.get("parent_session_id")
            or task.session_id
            or ""
        )

    def _finalize_branch_task_locked(self, task: Task, *, merge: bool) -> str:
        """Merge and clean an entry branch task once; return the routing session id."""
        parent_session_id = str(task.input.get("parent_session_id") or "").strip()
        branch_session_id = str(task.input.get("branch_session_id") or "").strip()
        if not parent_session_id or not branch_session_id:
            return task.session_id
        task.input["_route_session_id"] = parent_session_id
        if task.input.get("_branch_finalized"):
            return parent_session_id

        merged_count = 0
        if merge:
            try:
                base_count = int(task.input.get("base_count") or 0)
            except Exception:
                base_count = 0
            merged_count = self._merge_branch_locked(parent_session_id, branch_session_id, base_count)
        # [Fork/Merge 2026-05-12] finalize 标记写入 task.input。
        # 原因：完成、取消、僵尸回收等路径都可能尝试收束同一个入口分支。
        # 做法：用 _branch_finalized 做进程内幂等保护，并记录 merged_count 供调试。
        # 目的：避免重复 merge 或重复删除 branch session。
        task.input["_branch_finalized"] = True
        task.input["_branch_merged_count"] = merged_count
        if merge:
            self._cleanup_branch_locked(branch_session_id)
        return parent_session_id

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
        route_session_id = task.session_id
        if act == "preempted":
            self._route_preempted_locked(task, action)
            return  # preempted 不触发记忆提取，也不 merge branch
        elif act == "dispatch":
            self._route_dispatch_locked(task, action)
        elif act in ("finish", "ask"):
            # [Fork/Merge 2026-05-12] finish/ask 是入口分支的用户可见终态。
            # 原因：outbound 必须在主 session 产生，且应先把 branch JSONL 合并回主历史。
            # 做法：路由 finish 前执行幂等 finalize。目的：SDK 按主 conversation_key 收到回复。
            route_session_id = self._finalize_branch_task_locked(task, merge=True)
            self._route_finish_locked(task, action)
        elif act == "fail":
            # [Fork/Merge 2026-05-12] fail 也会结束入口分支。
            # 原因：错误回复应进入主 session，同时保留分支中已产生的上下文。
            # 做法：先 merge/cleanup，再走原 fail 输出。目的：失败路径与成功路径一致。
            route_session_id = self._finalize_branch_task_locked(task, merge=True)
            self._route_fail_locked(task, action)
        elif act == "cancelled":
            # [Fork/Merge 2026-05-12] engine 主动返回 cancelled 时不输出，但仍收束分支。
            # 原因：cancel 是终态，不应遗留 branch session。做法：只 finalize，不调用输出路由。
            # 目的：符合 finish/cancel 时 merge 回主 session 的架构约束。
            route_session_id = self._finalize_branch_task_locked(task, merge=True)
        # cancelled → 不做用户输出

        # Why: automatic memory extraction is now an engine.builtin supervisor
        # hook handler and must not receive SupervisorState directly. How: build a
        # callback-only context that includes the completed task. Purpose: keep
        # TaskRouterMixin focused on routing while eliminating handler cycles.
        self.hook_registry.fire(
            "on_entry_task_complete",
            self._build_supervisor_hook_ctx(task=task, session_id=route_session_id),
        )

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
        compact_target_session_id = ""
        if dispatch_input.get("_compact_dispatch"):
            task.input["_compact_dispatch_pending"] = True
            task.input["_compact_keep_recent"] = int(
                dispatch_input.get("_compact_keep_recent", 6)
            )
            # [AutoC 2026-05-13] Why: compactor results are applied after the
            # caller task is suspended, and caller.session_id may be an entry
            # branch. How: store the explicit parent-first target from engine,
            # falling back to caller parent/session for older actions. Purpose:
            # _apply_compact_result_locked always rewrites the durable session.
            compact_target_session_id = str(
                dispatch_input.get("target_session_id")
                or task.input.get("parent_session_id")
                or task.session_id
                or ""
            ).strip()
            if compact_target_session_id:
                task.input["_compact_target_session_id"] = compact_target_session_id

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
            # [AutoC 2026-05-13] Why: system.compactor is a child node, but its
            # output must target the parent session when the caller is a branch.
            # How: for compact dispatch only, key the child session and child task
            # under compact_target_session_id. Purpose: compactor bookkeeping and
            # result application stay attached to the durable parent session.
            _dispatch_session_id = compact_target_session_id or task.session_id
            _child_sid, _is_new = self.get_or_create_child_session(
                _dispatch_session_id, target, _ctx_key or "", _ctx_mode,
            )
            _single_input["child_session_id"] = _child_sid
            _single_input["context_mode"] = _ctx_mode
            _single_input["use_context"] = False  # child session 模式不走 _fetch_history
            if _ctx_mode == "fork":
                # fork 模式需要告诉 engine 从哪个 session 复制历史
                _single_input["fork_from_session_id"] = _dispatch_session_id

            # 审计报告 Step 1（2026-04-16）：删除此处单节点 dispatch 的 accumulate/fresh
            # 兼容期 fallback。engine/runner.py:514 在 child_session_id 非空时会无条件
            # 清空 context_ref，accumulate/fresh 两个分支写的值永远不会被消费。

            child = self._create_task_locked(
                session_id=_dispatch_session_id,
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
            session_id=self._route_session_id_for_task_locked(task), text=text,
            attachments=atts, source_inbound_seq=task.source_inbound_seq,
            node_id=task.node_id,
        )


    # ------------------------------------------------------------------ #
    #  异步 dispatch 结果注入
    # ------------------------------------------------------------------ #

    def _inject_async_dispatch_result_locked(
        self, task: Task, fallback_result: dict[str, Any],
    ) -> None:
        """异步 dispatch 子任务完成后，将结果作为新 inbound 注入 session。

        [Fork/Merge 2026-05-12] 不再自动 preempt running/suspended 入口 task。
        原因：preempt 只能由 adapter 显式 API 触发；内部结果注入应 fork 新分支并发处理。
        做法：始终走 inbound 创建路径。目的：避免内部事件破坏正在运行的入口分支。

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

        # [Fork/Merge 2026-05-12] 异步 dispatch 结果不再自动 preempt 入口 task。
        # 原因：新架构规定 preempt 只能由 adapter 显式调用 preempt API 触发，普通 inbound
        # 或内部注入都应通过 fork 分支并发处理。做法：移除 running/suspended 入口任务的
        # 自动 preempt 分支，直接创建 inbound。目的：同一 session 下多个入口分支可并行运行。

        # ---- 创建 inbound → 新 branch task ----
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

    def inject_async_result(self, session_id: str, text: str, attachments: list | None = None) -> dict[str, Any]:
        """注入异步工具结果到 session，并创建新的 inbound 分支。

        [Fork/Merge 2026-05-12] 旧的 running/suspended 自动 preempt 回退被移除。
        原因：新架构要求只有 adapter 显式 preempt API 才能抢占任务。
        做法：异步工具结果统一创建 inbound。目的：保持入口分支并发运行。
        """
        with self._lock:
            session_info = self.sessions.get(session_id)
            if not session_info:
                return {"ok": False, "error": "session not found"}

            result_atts = list(attachments or [])

            # [Fork/Merge 2026-05-12] 外部异步结果注入不再自动 preempt。
            # 原因：preempt 语义收窄为 adapter 显式请求；内部注入应像普通 inbound 一样
            # fork 新分支。做法：删除 running/suspended 自动抢占路径。目的：避免新 inbound
            # 破坏正在运行的入口分支。

            # ---- 创建 inbound → 新 branch task ----
            conv_key = session_info.conversation_key
            channel = session_info.channel
            msg_id = f"async_tool_result:{session_id}"

            payload: dict[str, Any] = {
                "channel": channel,
                "conversation_key": conv_key,
                "message_id": msg_id,
                "text": text,
            }
            if result_atts:
                payload["attachments"] = [{"path": p} for p in result_atts]

            evt = self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="inbound_message",
                payload=payload,
            )
            seq = int(evt.get("seq", 0))
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()
            self._create_entry_task_for_inbound_locked(
                inbound_seq=seq, session_id=session_id, payload=payload,
            )
            return {"ok": True, "strategy": "inbound"}

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
        # 1. caller 提供 _compact_target_session_id / parent_session_id → ConversationStore（父 session）
        # 2. caller 无 parent 但 main_session_enabled → ConversationStore（当前 session）
        # 3. 否则 → 旧 snapshot 路径
        from clonoth_runtime import load_runtime_config
        _rc = load_runtime_config(self.workspace_root)
        _main_conv_enabled = bool(
            _rc.get("engine", {}).get("child_session", {}).get("main_session_enabled", True)
        )
        # [AutoC 2026-05-13] Why: child_session_id and branch session ids are
        # temporary copies, so compacting them does not shrink the parent history.
        # How: prefer the explicit target saved during dispatch, then the caller's
        # parent_session_id, and only then the caller session. Purpose: supervisor
        # result application matches engine-side parent-first target selection.
        target_sid_for_conv = str(
            caller.input.get("_compact_target_session_id")
            or caller.input.get("target_session_id")
            or caller.input.get("parent_session_id")
            or ""
        ).strip()
        if not target_sid_for_conv and _main_conv_enabled:
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
                cr = self._apply_compact_via_conv_store_locked(
                    target_sid_for_conv, summary,
                    keep_recent=int(caller.input.get("_compact_keep_recent", 6)),
                )
                if cr["after"] < cr["before"]:
                    self._resume_compact_parent_locked(
                        caller, parent_ctx_ref,
                        compact_result=cr, success=True,
                    )
                    return
                # before == after：消息太少未压缩，静默恢复
                self._resume_compact_parent_locked(
                    caller, parent_ctx_ref,
                    compact_result=cr, success=False,
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
    ) -> dict[str, int]:
        """Step 2（2026-04-16）：在 ConversationStore 层面执行压缩。

        读取 target session 的 JSONL，用 summary 消息替换旧 task segment，保留
        最近 keep_recent 个完整 task segment。与 engine.compact.apply_compact_summary
        的策略一致，但直接操作 Message 对象而非 dict。

        注意 ConversationStore 里没有 system 消息（system prompt 每轮由 ai_step
        的 assemble_initial_messages 重建），所以无需处理 prefix/inner system。

        Returns: dict with keys: before, after, total_segments, kept_segments, compressed_segments
        """
        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import ConversationStore, Message, MessageType

        store = ConversationStore(self.workspace_root / "data" / "conversations")
        msgs = store.load(target_session_id)
        before = len(msgs)

        # [AutoC 2026-05-13] Why: ConversationStore stores Message objects, while
        # engine.compact.apply_compact_summary works on dict history. How: read
        # task ids through Message attributes and meta, then split only when the
        # consecutive source_task_id changes. Purpose: keep recent task segments
        # complete instead of slicing by raw message count.
        segments: list[list[Any]] = []
        cur_seg: list[Any] = []
        cur_tid: str = ""
        for msg in msgs:
            meta = msg.meta if isinstance(getattr(msg, "meta", None), dict) else {}
            tid = str(meta.get("source_task_id", "") or getattr(msg, "source_task_id", "") or "")
            if tid != cur_tid and cur_seg:
                segments.append(cur_seg)
                cur_seg = []
            cur_tid = tid
            cur_seg.append(msg)
        if cur_seg:
            segments.append(cur_seg)

        # [AutoC 2026-05-13] Why: keep_recent now means recent task segments, not
        # recent messages. How: require at least one retained segment and skip the
        # rewrite when the store has no older segments to summarize. Purpose: match
        # the in-memory compaction path and keep the active task intact.
        keep_recent = max(keep_recent, 1)
        if len(segments) <= keep_recent:
            return {"before": before, "after": before, "total_segments": len(segments), "kept_segments": len(segments), "compressed_segments": 0}

        kept_segments = segments[-keep_recent:]
        to_remove_segments = segments[:-keep_recent]

        to_keep: list[Any] = []
        for seg in kept_segments:
            to_keep.extend(seg)

        to_remove: list[Any] = []
        for seg in to_remove_segments:
            to_remove.extend(seg)

        # P6.5 Metadata Preservation: 收集被压缩掉的消息所属的 source_task_id，
        # 存入 summary 消息的 meta 中。L2 snip_history 据此判断哪些 task 已被
        # LLM 压缩过，避免因 ID 丢失而反复 fall through 到 LLM compact。
        # [2026-04-26] 累积继承：旧 compact_summary 被再次压缩时，
        # 继承其 meta.compressed_task_ids，防止历史 ID 丢失。
        # [AutoC 2026-05-13] Why: metadata must describe removed task segments,
        # not an arbitrary message prefix. How: iterate over to_remove from the
        # segment split above and access Message fields by attribute. Purpose:
        # keep compressed_task_ids accurate after task-granular retention.
        _ctid_set: set[str] = set()
        for _rm in to_remove:
            if getattr(_rm, "source_task_id", ""):
                _ctid_set.add(str(_rm.source_task_id))
            _rm_meta = _rm.meta if isinstance(getattr(_rm, "meta", None), dict) else {}
            _old_ctids = _rm_meta.get("compressed_task_ids")
            if isinstance(_old_ctids, list):
                for _ctid in _old_ctids:
                    if _ctid:
                        _ctid_set.add(str(_ctid))
        compressed_task_ids = list(_ctid_set)

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
        after = len(new_messages)
        result = {
            "before": before, "after": after,
            "total_segments": len(segments),
            "kept_segments": len(kept_segments),
            "compressed_segments": len(to_remove_segments),
        }
        if after < before:
            # [AutoC 2026-05-13] Why: active branches forked before parent compact
            # keep their own old prefix and would otherwise resume or merge from an
            # uncompressed copy. How: after the parent rewrite succeeds, mirror a
            # simplified prefix compaction into active branch sessions. Purpose:
            # branch histories stay bounded while preserving branch-local tails.
            self._sync_compact_to_branches(target_session_id, summary_msg, to_keep)
        return result

    def _clone_compact_summary_message(self, summary_msg: Any) -> Any:
        """Clone a compact summary message for a branch session."""
        # [AutoC 2026-05-13] Why: parent and branch JSONL files should not share
        # the same Message id even when they carry the same summary content. How:
        # create a new Message with copied content and metadata. Purpose: future
        # message-id based matching can distinguish parent and branch records.
        from uuid import uuid4
        from datetime import datetime, timezone
        from engine.conversation_store import Message, MessageType

        return Message(
            id=str(uuid4()),
            role=getattr(summary_msg, "role", "user") or "user",
            content=str(getattr(summary_msg, "content", "") or ""),
            message_type=getattr(summary_msg, "message_type", "") or MessageType.SUMMARY,
            created_at=datetime.now(timezone.utc).isoformat(),
            meta=dict(getattr(summary_msg, "meta", {}) or {}),
            source_node_id=getattr(summary_msg, "source_node_id", "") or "",
            source_task_id=getattr(summary_msg, "source_task_id", "") or "compact_summary",
        )

    def _sync_compact_to_branches(
        self, target_session_id: str, summary_msg: Any, to_keep: list[Any],
    ) -> None:
        """Synchronize a successful parent compact into active branch sessions."""
        # [AutoC 2026-05-13] Why: already-forked branch sessions keep the old
        # uncompressed parent prefix after the parent JSONL is compacted. How:
        # find active entry branches for the parent and compact only their forked
        # prefix, while separately compacting ordinary active child sessions under
        # the same parent. Purpose: keep branch stores small without dropping
        # branch-local messages that still need to merge back.
        from engine.conversation_store import ConversationStore

        target = str(target_session_id or "").strip()
        if not target:
            return
        keep_recent = len(to_keep)
        store = ConversationStore(self.workspace_root / "data" / "conversations")

        entry_branches: dict[str, dict[str, Any]] = {}
        child_sessions: set[str] = set()
        for task in list(self.tasks.values()):
            if self._task_terminal(task):
                continue
            parent_sid = str(task.input.get("parent_session_id") or "").strip()
            branch_sid = str(task.input.get("branch_session_id") or "").strip()
            if parent_sid == target and branch_sid:
                info = entry_branches.setdefault(branch_sid, {"tasks": [], "base_count": 0})
                info["tasks"].append(task)
                try:
                    base_count = int(task.input.get("base_count") or 0)
                except Exception:
                    base_count = 0
                if base_count > int(info.get("base_count") or 0):
                    info["base_count"] = base_count

            child_sid = str(task.input.get("child_session_id") or "").strip()
            if task.session_id == target and child_sid:
                child_sessions.add(child_sid)

        for branch_sid, info in entry_branches.items():
            self._sync_entry_branch_compact_locked(
                store,
                branch_sid,
                summary_msg,
                keep_recent=keep_recent,
                base_count=int(info.get("base_count") or 0),
                tasks=list(info.get("tasks") or []),
            )

        for child_sid in child_sessions:
            self._sync_child_session_compact_locked(
                store,
                child_sid,
                summary_msg,
                keep_recent=keep_recent,
            )

    def _sync_entry_branch_compact_locked(
        self,
        store: Any,
        branch_session_id: str,
        summary_msg: Any,
        *,
        keep_recent: int,
        base_count: int,
        tasks: list[Task],
    ) -> None:
        """Compact the forked parent prefix of one active entry branch."""
        # [AutoC 2026-05-13] Why: entry branch merge uses base_count to append only
        # branch-local tail messages back to the parent. How: replace only the
        # forked prefix with summary + recent prefix messages, then update base_count
        # on active tasks. Purpose: branch sync cannot lose or duplicate the branch
        # tail during final merge.
        branch = str(branch_session_id or "").strip()
        if not branch or base_count <= 0:
            return
        try:
            branch_msgs = list(store.load(branch))
            if len(branch_msgs) < base_count:
                log.warning(
                    "compact branch sync skipped for %s: base_count=%d exceeds len=%d",
                    branch,
                    base_count,
                    len(branch_msgs),
                )
                return
            if base_count <= keep_recent + 1:
                return
            branch_prefix = branch_msgs[:base_count]
            branch_tail = branch_msgs[base_count:]
            prefix_keep = branch_prefix[-keep_recent:] if keep_recent > 0 else []
            branch_summary = self._clone_compact_summary_message(summary_msg)
            new_base = [branch_summary] + prefix_keep
            store.replace_all(branch, new_base + branch_tail)
            for task in tasks:
                task.input["base_count"] = len(new_base)
                task.input["base_last_id"] = getattr(prefix_keep[-1], "id", "") if prefix_keep else branch_summary.id
                task.updated_at = _now()
            log.info(
                "compact branch sync: %s base %d → %d, preserved tail=%d",
                branch,
                base_count,
                len(new_base),
                len(branch_tail),
            )
        except Exception as exc:
            log.warning("compact branch sync failed for %s: %s", branch, exc)

    def _sync_child_session_compact_locked(
        self,
        store: Any,
        child_session_id: str,
        summary_msg: Any,
        *,
        keep_recent: int,
    ) -> None:
        """Apply simplified compact sync to one active ordinary child session."""
        # [AutoC 2026-05-13] Why: ordinary child sessions are not merged by
        # base_count, so they can use the same simple summary + recent messages
        # shape as the parent. How: replace the whole child JSONL when doing so
        # shortens it. Purpose: long-lived active child sessions do not keep an
        # outdated uncompressed parent fork.
        child = str(child_session_id or "").strip()
        if not child:
            return
        try:
            child_msgs = list(store.load(child))
            before = len(child_msgs)
            if before <= keep_recent + 1:
                return
            child_keep = child_msgs[-keep_recent:] if keep_recent > 0 else []
            store.replace_all(child, [self._clone_compact_summary_message(summary_msg)] + child_keep)
            log.info(
                "compact child sync: %s %d → %d",
                child,
                before,
                1 + len(child_keep),
            )
        except Exception as exc:
            log.warning("compact child sync failed for %s: %s", child, exc)

    def _resume_compact_parent_locked(
        self, caller: Task, context_ref: str, *,
        compact_result: dict[str, int] | None = None,
        before: int = 0, after: int = 0, success: bool = True,
    ) -> None:
        """恢复等待压缩的父 task。"""
        caller.status = TaskStatus.pending
        caller.waiting_for_task_id = None
        caller.worker_id = None
        caller.lease_expires_at = None
        caller.updated_at = _now()
        if compact_result:
            _rd = {"type": "compact_done", "success": success, **compact_result}
        else:
            _rd = {"type": "compact_done", "success": success, "before": before, "after": after}
        caller.input["resume_data"] = _rd
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
        # 子节点任务（有 caller_task_id）用完即丢，不需要轮摘要
        if task.caller_task_id:
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
        # [Fork/Merge 2026-05-12] 入口分支完成后，摘要任务必须回到主 session。
        # 原因：branch JSONL 已经在 finalize 中 merge 并 cleanup，继续读取 task.session_id
        # 会访问已删除的分支。做法：无 child_session_id 时读取 route_session_id。
        # 目的：轮摘要仍能基于合并后的主 ConversationStore 生成。
        _route_sid = self._route_session_id_for_task_locked(task)
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            _store = ConversationStore(Path(self.workspace_root) / "data" / "conversations")
            _child_sid = task.input.get("child_session_id") or ""
            _load_sid = _child_sid if _child_sid else _route_sid
            _all_msgs = _store.load(_load_sid)
            _task_msgs = [m for m in _all_msgs if m.source_task_id == task.task_id]
            if not _task_msgs:
                return
            _instruction = _format_task_messages_for_turn_summary(_task_msgs)
        except Exception as e:
            log.warning("Failed to format task messages for turn summary: %s", e)
            return

        if not _instruction.strip():
            return

        # Strip tool call blocks to prevent summarizer from mimicking tool calls.
        # The <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>> markers cause flash models to
        # hallucinate tool execution instead of summarizing.
        import re
        _instruction = re.sub(
            r'<<<TOOL_CALL>>>.*?<<<END_TOOL_CALL>>>',
            '[tool call omitted]',
            _instruction,
            flags=re.DOTALL,
        )

        # 截断（保持在 ~30K chars，与原 turn_summary.py 一致）
        if len(_instruction) > 30000:
            _instruction = _instruction[:30000] + "\n...[truncated]"

        summarizer_node = get_str(runtime_cfg, "engine.turn_summary.node_id", "system.turn_summarizer").strip()

        # [2026-05-07] Give turn_summarizer its own child session so its messages
        # don't pollute the parent conversation JSONL.
        # Why: previously used task.session_id directly, causing all summarizer
        # tool_call/tool_result messages to be written into the parent's JSONL,
        # creating orphan tool pairs that Anthropic/Gemini reject with 400.
        _sum_child_sid, _ = self.get_or_create_child_session(
            _route_sid, summarizer_node, "turn_summary", "fresh",
        )
        _summary_generation = self._current_session_generation_locked(_route_sid) or task.session_generation

        self._create_task_locked(
            session_id=_route_sid,
            session_generation=_summary_generation,
            kind=TaskKind.node,
            node_id=summarizer_node,
            input_data={
                "instruction": _instruction,
                "_system_task": True,
                "_turn_summary_dispatch": True,
                "_target_task_id": task.task_id,
                "_target_session_id": _route_sid,
                "child_session_id": _sum_child_sid,
            },
            continuation={},
            source_inbound_seq=None,
            caller_task_id=None,
        )
