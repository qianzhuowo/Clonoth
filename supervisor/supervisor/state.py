"""Supervisor 核心状态 —— 组合类，继承三个 mixin。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ._helpers import SessionInfo, _now
from .eventlog import EventLog, SYSTEM_SESSION_ID
from engine.builtin.loader import auto_discover_and_register
from engine.hooks import HookRegistry
from .policy import PolicyEngine
from .session import SessionMixin
from .session_store import SessionStore
from .task_router import TaskRouterMixin
from .task_store import TaskStoreMixin
from .types import (
    AdminStateOut,
    Approval,
    ApprovalStatus,
    OpRequestOut,
    SafetyLevel,
    Task,
    TaskKind,
    TaskStatus,
)


class SupervisorState(SessionMixin, TaskStoreMixin, TaskRouterMixin):
    """Supervisor 核心状态。管理会话、inbound 队列、task 队列与审批。"""

    def __init__(self, *, workspace_root: Path, eventlog: EventLog, policy: PolicyEngine) -> None:
        self.workspace_root = workspace_root
        self.eventlog = eventlog
        self.policy = policy
        self.started_at = _now()

        self._lock = threading.RLock()

        self.sessions: dict[str, SessionInfo] = {}
        self.conversation_map: dict[str, str] = {}
        self.session_generations: dict[str, int] = {}

        self.approvals: dict[str, Approval] = {}

        self.tasks: dict[str, Task] = {}
        self._task_order: list[str] = []

        self._engine_last_seen_at: datetime | None = None
        self._engine_last_worker_id: str | None = None

        self._inbound_order: list[int] = []
        self._inbound_events: dict[int, dict[str, Any]] = {}
        self._inbound_processed: set[int] = set()
        self._inbound_routed: dict[int, dict[str, Any]] = {}
        self._inbound_cursor: int = 0

        @dataclass
        class _InboundLease:
            worker_id: str
            expires_at: datetime

        self._InboundLease = _InboundLease
        self._inbound_leases: dict[int, _InboundLease] = {}

        self._cancelled_sessions: set[str] = set()
        self._tools_reload_seq: int = 0
        self.session_entry_overrides: dict[str, str] = {}  # session_id -> node_id (AI switch)
        self._session_context_usage: dict[str, dict[str, Any]] = {}  # session_id -> latest usage
        self._engine_generations: dict[str, str] = {}  # worker_id -> generation_id (Direction 2)
        self._pending_restart_notify: str | None = None  # session_id to notify after engine registers

        # Why: supervisor feature side effects should be registered handlers rather
        # than hard-coded router or scheduler branches. How: create one unified
        # HookRegistry per SupervisorState and auto-discover built-ins before event
        # replay. Purpose: inbound replay can notify handlers without storing their
        # mutable state on SupervisorState or using a separate supervisor registry.
        self.hook_registry = HookRegistry()
        # Why: built-in supervisor handlers now live under engine.builtin, where
        # they cannot import SupervisorState or other supervisor internals. How:
        # register every PLUGIN_META declaration through the auto loader and retain
        # the returned instances for diagnostics and existing tests. Purpose:
        # preserve handler-owned timer/cursor state while removing cyclic imports
        # and hard-coded registration.
        _builtin_handlers = auto_discover_and_register(self.hook_registry)
        self._memory_extract_handler = _builtin_handlers["memory_extract"]
        self._dream_handler = _builtin_handlers["dream"]

        # External plugins: same mechanism as engine, scan plugins/ directory
        from engine.hooks.loader import load_external_plugins
        _plugins_dir = workspace_root / "plugins"
        _ext_count = load_external_plugins(self.hook_registry, _plugins_dir)
        if _ext_count:
            logger.info("Loaded %d external plugin(s) for supervisor hooks", _ext_count)

        # ---- Child Session 隔离（Phase A）：映射表 ----
        # (parent_session_id, node_id, context_key) → child_session_id
        self.child_session_map: dict[tuple[str, str, str], str] = {}
        # 反向索引：parent_session_id → set of child_session_ids，用于 clear 时快速查找
        self.parent_children: dict[str, set[str]] = {}

        # ---- 方案 A: 独立 session 持久化 ----
        self._session_store = SessionStore(workspace_root / "data" / "sessions.json")
        loaded_sessions, loaded_conv_map, loaded_child_map, loaded_parent_children = self._session_store.load()
        if loaded_sessions:
            self.sessions.update(loaded_sessions)
            self.conversation_map.update(loaded_conv_map)
        # Child Session 隔离（Phase A）：从 sessions.json 恢复 child session 映射
        if loaded_child_map:
            self.child_session_map.update(loaded_child_map)
        if loaded_parent_children:
            for psid, children in loaded_parent_children.items():
                self.parent_children.setdefault(psid, set()).update(children)

        self.rebuild_from_events(eventlog.events)

        # BUG FIX (2026-04-29): 重启后清空 session_entry_overrides。
        # rebuild_from_events 会回放 node_switch 事件，恢复旧的入口节点覆盖。
        # 但重启后所有任务已死，覆盖指向的节点（如 bootstrap.cmd_reviewer）
        # 的上下文已丢失。如果不清空，用户消息会被路由到错误的节点。
        # 例：ereuna_main 曾 switch_node 到 cmd_reviewer，但 cmd_reviewer
        # 完成后未切回默认节点，重启后该覆盖从事件回放中恢复，导致用户
        # 在 discord 频道发消息时被错误地路由到 cmd_reviewer。
        self.session_entry_overrides.clear()

    def _build_supervisor_hook_ctx(self, **extra: Any) -> dict[str, Any]:
        """Build the callback-only context passed to built-in supervisor hooks."""
        # Why: engine.builtin handlers must not import or keep a reference to
        # SupervisorState. How: expose the small set of required supervisor
        # operations as callbacks and plain values. Purpose: make hook handlers
        # cycle-free while preserving the previous behavior at each fire point.
        ctx: dict[str, Any] = {
            "workspace_root": self.workspace_root,
            "session_messages": lambda sid, limit=0: self.session_messages(session_id=sid, limit=limit),
            "create_task": self._create_task_from_hook_locked,
            "acquire_lock": self._lock,
            "format_transcript": self._format_transcript_for_extract_callback,
            "current_session_generation": lambda sid: self._current_session_generation_locked(sid),
            "session_count": lambda: len(self.sessions),
        }
        ctx.update(extra)
        return ctx

    def _create_task_from_hook_locked(self, **kwargs: Any) -> Task:
        """Create a task for a built-in hook using callback-friendly arguments."""
        # Why: relocated supervisor handlers cannot import TaskKind or call session
        # helpers directly. How: normalize string/enum kind values and optionally
        # create a session from channel plus conversation_key before delegating to
        # _create_task_locked. Purpose: keep handler code callback-only while still
        # using the existing task creation path and event snapshot behavior.
        data = dict(kwargs)
        conversation_key = str(data.pop("conversation_key", "") or "").strip()
        channel = str(data.pop("channel", "") or "").strip()
        if not str(data.get("session_id") or "").strip():
            if not conversation_key:
                raise ValueError("session_id or conversation_key is required")
            if not channel:
                channel = conversation_key.split(":", 1)[0] if ":" in conversation_key else "system"
            data["session_id"] = self.get_or_create_session(channel=channel, conversation_key=conversation_key)

        sid = str(data.get("session_id") or "").strip()
        generation = self._to_positive_int(data.get("session_generation"))
        if generation is None:
            generation = self._current_session_generation_locked(sid) or 1
        data["session_generation"] = generation
        if sid and not self.session_generations.get(sid):
            self.session_generations[sid] = generation

        kind = data.get("kind") or TaskKind.node
        if not isinstance(kind, TaskKind):
            value = getattr(kind, "value", kind)
            try:
                kind = TaskKind(str(value or "node"))
            except Exception:
                kind = TaskKind.node
        data["kind"] = kind

        input_data = data.get("input_data")
        if isinstance(input_data, dict):
            task_context = input_data.get("task_context")
            if isinstance(task_context, dict):
                # Why: hooks that create tasks directly bypass inbound processing,
                # which used to add these identifiers. How: fill missing values in
                # the provided task_context. Purpose: preserve system-task metadata
                # for engine-side checks without requiring supervisor imports.
                task_context.setdefault("session_id", sid)
                task_context.setdefault("session_generation", generation)

        data.setdefault("continuation", {})
        data.setdefault("source_inbound_seq", None)
        data.setdefault("caller_task_id", None)
        return self._create_task_locked(**data)

    @staticmethod
    def _format_transcript_for_extract_callback(messages: list[dict[str, Any]], *, max_chars: int = 12000) -> str:
        """Format conversation messages into transcript text for extraction hooks."""
        # Why: MemoryExtractHandler now receives formatting as an injected callback
        # rather than importing supervisor code. How: keep the previous pure
        # formatter on SupervisorState and pass it through ctx. Purpose: retain the
        # fallback transcript format while removing handler-side supervisor imports.
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

    # ------------------------------------------------------------------ #
    #  事件回放
    # ------------------------------------------------------------------ #

    def rebuild_from_events(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            et = e.get("type")
            session_id = str(e.get("session_id") or "")
            payload = e.get("payload") or {}

            try:
                seq = int(e.get("seq", 0))
            except Exception:
                seq = 0

            if et == "session_created":
                self._apply_session_created(session_id, payload)
            elif et == "inbound_message":
                conv = payload.get("conversation_key")
                if isinstance(conv, str) and session_id:
                    self.conversation_map[conv] = session_id
                    # 方案 B: 若 session 记录缺失，从 inbound 事件恢复最小 SessionInfo
                    if session_id not in self.sessions:
                        self.sessions[session_id] = SessionInfo(
                            session_id=session_id,
                            channel=payload.get("channel", ""),
                            conversation_key=conv,
                            created_at=_now(),
                            updated_at=_now(),
                        )
                self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "inbound_processed":
                self._apply_inbound_processed(payload)
            elif et == "outbound_message":
                self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            elif et == "approval_requested":
                self._apply_approval_requested(payload)
            elif et == "approval_decided":
                self._apply_approval_decided(payload)
            elif et in {"task_created", "task_started", "task_completed", "task_cancelled", "task_cancel_requested", "task_requeued", "task_suspended", "task_resumed"}:
                self._apply_task_snapshot(payload)
            elif et == "context_reset":
                conv = payload.get("conversation_key")
                if isinstance(conv, str):
                    self.conversation_map.pop(conv, None)
            elif et == "cancel_requested":
                self._apply_cancel_requested(session_id, payload)
            elif et == "node_switch":
                self._apply_node_switch(session_id, payload)
            elif et == "engine_registered":
                wid = str(payload.get("worker_id") or "").strip()
                gid = str(payload.get("generation_id") or "").strip()
                if wid and gid:
                    self._engine_generations[wid] = gid

        self._advance_inbound_cursor()
    # ------------------------------------------------------------------ #
    #  节点切换
    # ------------------------------------------------------------------ #

    def _apply_node_switch(self, session_id: str, payload: dict[str, Any]) -> None:
        target = str(payload.get("target_node_id") or "").strip()
        if session_id and target:
            self.session_entry_overrides[session_id] = target
        elif session_id and not target:
            self.session_entry_overrides.pop(session_id, None)

    def switch_session_node(self, session_id: str, target_node_id: str) -> dict[str, Any]:
        """设置或清除 session 级入口节点覆盖。"""
        sid = (session_id or "").strip()
        target = (target_node_id or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id required"}
        with self._lock:
            if sid not in self.sessions:
                return {"ok": False, "error": "session not found"}
            default_node = self._default_entry_node()
            if target and target != default_node:
                self.session_entry_overrides[sid] = target
            else:
                self.session_entry_overrides.pop(sid, None)
                target = ""  # 清除覆盖
            self.eventlog.append(
                session_id=sid,
                component="engine",
                type_="node_switch",
                payload={
                    "target_node_id": target,
                    "default_node_id": default_node,
                    "ts": _now().isoformat(),
                },
            )
            return {
                "ok": True,
                "session_id": sid,
                "target_node_id": target or default_node,
                "is_override": bool(target),
            }

    def get_session_active_node(self, session_id: str) -> dict[str, Any]:
        """获取 session 当前实际使用的入口节点。"""
        sid = (session_id or "").strip()
        with self._lock:
            override = self.session_entry_overrides.get(sid, "").strip()
            default_node = self._default_entry_node()
            return {
                "node_id": override or default_node,
                "is_override": bool(override),
                "default_node_id": default_node,
            }

    # ------------------------------------------------------------------ #
    #  Child Session 隔离（Phase A + B）
    # ------------------------------------------------------------------ #

    def get_or_create_child_session(
        self,
        parent_session_id: str,
        node_id: str,
        context_key: str,
        context_mode: str,
    ) -> tuple[str, bool]:
        """根据 context_mode 获取或创建 child session。

        Child Session 隔离核心方法。三种 context_mode：
        - accumulate：复用已有 child session（如存在且未过期），否则新建
        - fresh：强制创建新 child session，旧的不删除（等 TTL 过期清理）
        - fork：创建新 child session，调用方需额外做 ConversationStore.fork()

        Returns:
            (child_session_id, is_new) — is_new 表示是否新建了 session
        """
        map_key = (parent_session_id, node_id, context_key)

        if context_mode == "accumulate":
            existing_cid = self.child_session_map.get(map_key)
            if existing_cid and not self._is_child_session_expired(existing_cid):
                # 复用现有 child session，更新 last_active_at
                self._session_store.update_last_active(existing_cid)
                return existing_cid, False
            # 已过期或不存在：新建（过期的旧 session 留给 TTL 清理）
            return self._create_child_session(parent_session_id, node_id, context_key), True

        elif context_mode == "fresh":
            # 强制新建，旧映射更新为新 ID
            return self._create_child_session(parent_session_id, node_id, context_key), True

        elif context_mode == "fork":
            # fork 与 fresh 类似：总是新建
            return self._create_child_session(parent_session_id, node_id, context_key), True

        else:
            # 未知模式，按 accumulate 处理
            logger.warning("unknown context_mode '%s', falling back to accumulate", context_mode)
            return self.get_or_create_child_session(
                parent_session_id, node_id, context_key, "accumulate",
            )

    def _create_child_session(
        self, parent_session_id: str, node_id: str, context_key: str,
    ) -> str:
        """生成新 child session 并持久化映射。

        Child Session 隔离（Phase A）：生成 child_{uuid4().hex[:12]} 格式的 ID，
        写入 child_session_map、parent_children、sessions.json。
        """
        child_sid = f"child_{uuid.uuid4().hex[:12]}"
        map_key = (parent_session_id, node_id, context_key)

        # 更新内存映射
        self.child_session_map[map_key] = child_sid
        self.parent_children.setdefault(parent_session_id, set()).add(child_sid)

        # 持久化到 sessions.json
        self._session_store.on_child_session_created(
            child_session_id=child_sid,
            parent_session_id=parent_session_id,
            node_id=node_id,
            context_key=context_key,
        )

        logger.info(
            "child session created: %s (parent=%s, node=%s, key=%s)",
            child_sid, parent_session_id, node_id, context_key,
        )
        return child_sid

    def _is_child_session_expired(self, child_session_id: str) -> bool:
        """检查 child session 是否已超过 TTL。

        Child Session 隔离（Phase A）：从 sessions.json 的 last_active_at 字段
        判定是否过期。如果记录不存在或已 reset，视为过期。
        """
        from clonoth_runtime import get_int, load_runtime_config
        runtime_cfg = load_runtime_config(self.workspace_root)
        ttl_hours = get_int(runtime_cfg, "engine.child_session.ttl_hours", 24, min_value=1)

        entry = self._session_store._registry.get(child_session_id)
        if entry is None or entry.get("reset"):
            return True
        last_active_str = entry.get("last_active_at", "")
        if not last_active_str:
            return True
        try:
            last_active = datetime.fromisoformat(last_active_str)
            age_hours = (_now() - last_active).total_seconds() / 3600
            return age_hours > ttl_hours
        except (ValueError, TypeError):
            return True

    def _expire_child_session(self, child_session_id: str) -> None:
        """删除一个过期的 child session。

        Child Session 隔离（Phase A）：删除 JSONL 文件、从映射表移除、标记 reset。
        """
        # 1. 删除 JSONL 文件
        conv_path = self.workspace_root / "data" / "conversations" / f"{child_session_id}.jsonl"
        if conv_path.exists():
            conv_path.unlink()

        # 2. 从映射表移除
        key_to_remove = None
        for key, cid in self.child_session_map.items():
            if cid == child_session_id:
                key_to_remove = key
                break
        if key_to_remove:
            del self.child_session_map[key_to_remove]
            parent_sid = key_to_remove[0]
            if parent_sid in self.parent_children:
                self.parent_children[parent_sid].discard(child_session_id)

        # 3. 标记 session 为 reset
        self._session_store.on_session_reset(child_session_id)

        logger.info("child session expired: %s", child_session_id)

    # ------------------------------------------------------------------ #
    #  审批
    # ------------------------------------------------------------------ #

    def _apply_approval_requested(self, payload: dict[str, Any]) -> None:
        try:
            a = Approval.model_validate(payload)
        except Exception:
            return
        self.approvals[a.approval_id] = a

    def _apply_approval_decided(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("approval_id")
        if approval_id in self.approvals:
            a = self.approvals[approval_id]
            decision = payload.get("decision")
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = payload.get("comment")
            a.decided_at = _now()

    def create_approval(self, *, session_id: str, operation: str, details: dict[str, Any]) -> Approval:
        now = _now()
        fingerprint_src = json.dumps({"operation": operation, "details": details}, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:12]

        approval = Approval(
            approval_id=str(uuid.uuid4()),
            session_id=session_id,
            operation=operation,
            details=details,
            fingerprint=fingerprint,
            status=ApprovalStatus.pending,
            decision=None,
            comment=None,
            requested_at=now,
            decided_at=None,
        )

        with self._lock:
            # 同指纹的 pending 审批不再重复创建
            for a in self.approvals.values():
                if a.fingerprint == fingerprint and a.status == ApprovalStatus.pending:
                    return a

            self.approvals[approval.approval_id] = approval
            self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="approval_requested",
                payload=approval.model_dump(mode="json"),
            )
        return approval

    def decide_approval(self, *, approval_id: str, decision: str, comment: str | None = None) -> Approval | None:
        with self._lock:
            a = self.approvals.get(approval_id)
            if a is None or a.status != ApprovalStatus.pending:
                return a
            a.status = ApprovalStatus.allowed if decision == "allow" else ApprovalStatus.denied
            a.decision = decision
            a.comment = comment
            a.decided_at = _now()
            self.eventlog.append(
                session_id=a.session_id,
                component="supervisor",
                type_="approval_decided",
                payload={
                    "approval_id": a.approval_id,
                    "decision": decision,
                    "comment": comment,
                    "ts": a.decided_at.isoformat(),
                },
            )
            return a

    def request_operation(self, *, session_id: str, op: str, parameters: dict[str, Any]) -> OpRequestOut:
        decision = self.policy.evaluate(op=op, parameters=parameters)
        if decision.safety_level == SafetyLevel.auto:
            return OpRequestOut(safety_level=SafetyLevel.auto, reason=decision.reason, approval_id=None)
        approval = self.create_approval(session_id=session_id, operation=op, details=parameters)
        return OpRequestOut(safety_level=SafetyLevel.approval_required, reason=decision.reason, approval_id=approval.approval_id)

    # ------------------------------------------------------------------ #
    #  Admin / 杂项
    # ------------------------------------------------------------------ #

    def list_events(self, *, session_id: str, after_seq: int) -> list[dict[str, Any]]:
        return self.eventlog.list_events(session_id=session_id, after_seq=after_seq)

    def admin_state(self) -> AdminStateOut:
        approval_counts = {s.value: 0 for s in ApprovalStatus}
        pending: list[Approval] = []
        for a in self.approvals.values():
            approval_counts[a.status.value] = approval_counts.get(a.status.value, 0) + 1
            if a.status == ApprovalStatus.pending:
                pending.append(a)

        task_counts = {s.value: 0 for s in TaskStatus}
        for t in self.tasks.values():
            task_counts[t.status.value] = task_counts.get(t.status.value, 0) + 1

        engine_last_seen_at, engine_worker_id = self.engine_seen_snapshot()

        return AdminStateOut(
            sessions=len(self.sessions),
            approvals=approval_counts,
            tasks=task_counts,
            pending_approvals=pending,
            engine_runtime={
                "worker_id": engine_worker_id,
                "last_seen_at": engine_last_seen_at,
                "workers": sorted({t.worker_id for t in self.tasks.values() if t.worker_id}),
            },
        )

    # ------------------------------------------------------------------ #
    #  Direction 2: Engine Generation ID
    # ------------------------------------------------------------------ #

    def register_engine(self, worker_id: str, generation_id: str) -> dict[str, Any]:
        """Register an engine worker with its generation ID.

        Cancels any running/pending tasks from the same worker_id with a
        different generation (orphans from a previous engine instance).
        """
        wid = (worker_id or "").strip()
        gid = (generation_id or "").strip()
        if not wid or not gid:
            return {"ok": False, "error": "worker_id and generation_id required"}

        with self._lock:
            old_gen = self._engine_generations.get(wid)
            self._engine_generations[wid] = gid

            orphan_count = 0
            if old_gen and old_gen != gid:
                # Same worker restarted with new generation — cancel its old tasks
                orphan_count = self._cancel_worker_orphans_locked(wid)
            elif not old_gen:
                # First registration of this worker — also clean up any tasks
                # from a previous run where this worker_id was used
                orphan_count = self._cancel_worker_orphans_locked(wid)

            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="engine",
                type_="engine_registered",
                payload={
                    "worker_id": wid,
                    "generation_id": gid,
                    "previous_generation_id": old_gen or "",
                    "orphans_cancelled": orphan_count,
                    "ts": _now().isoformat(),
                },
            )

            # ---- Deferred restart notification ----
            # Injected here (after orphan cleanup) so the new task gets the
            # current generation and won't be reaped as an orphan.
            if self._pending_restart_notify:
                _notify_sid = self._pending_restart_notify
                self._pending_restart_notify = None  # consume once
                _si = self.sessions.get(_notify_sid)
                if _si:
                    _restart_evt = self.eventlog.append(
                        session_id=_notify_sid,
                        component="supervisor",
                        type_="inbound_message",
                        payload={
                            "channel": _si.channel,
                            "conversation_key": _si.conversation_key,
                            "text": "[系统通知] Engine 重启已完成，新代码已生效。",
                        },
                    )
                    self.record_inbound_message_event(_restart_evt)

            return {"ok": True, "orphans_cancelled": orphan_count, "generation_id": gid}

    def _cancel_worker_orphans_locked(self, worker_id: str) -> int:
        """Cancel all non-terminal tasks assigned to a specific worker_id."""
        now = _now()
        count = 0
        for task in self.tasks.values():
            if self._task_terminal(task):
                continue
            if task.worker_id != worker_id:
                continue
            task.cancel_requested = True
            task.status = TaskStatus.cancelled
            task.updated_at = now
            task.lease_expires_at = None
            if task.waiting_for_task_id:
                task.waiting_for_task_id = None
            task.result = {"action": "cancelled", "error": f"engine worker {worker_id} restarted (generation mismatch)"}
            self._event_task_snapshot("task_cancelled", task)
            count += 1
        return count

    def write_boot_event(self) -> dict[str, Any]:
        prev = self.eventlog.last_boot_run_id()
        payload = {
            "previous_run_id": prev,
            "restarted": prev is not None,
            "ts": _now().isoformat(),
        }
        return self.eventlog.append(session_id=SYSTEM_SESSION_ID, component="supervisor", type_="boot", payload=payload)
