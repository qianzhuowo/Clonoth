"""Session 管理 mixin —— 会话创建、inbound/outbound 队列、消息记录。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from ._helpers import SessionInfo, _now
from .eventlog import SYSTEM_SESSION_ID
from .types import ApprovalStatus, TaskStatus


# ---------------------------------------------------------------------------
#  多模态消息构建（内联版本，避免 supervisor → engine 依赖）
# ---------------------------------------------------------------------------

_ALLOWED_ATT_PREFIX = "data/attachments/"


def _build_multimodal_content(
    text: str, attachments: list[dict[str, Any]],
) -> list[dict[str, Any]] | str:
    """将文本和附件合并为多模态消息内容。

    图片附件生成 file:// image_url 引用；
    文本文件附件生成元数据文本引用（不读内容）。
    """
    parts: list[dict[str, Any]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        path = str(att.get("path") or "").strip()
        if not path or not path.replace("\\", "/").lstrip("/").startswith(_ALLOWED_ATT_PREFIX):
            continue
        att_type = str(att.get("type") or "").strip()
        if att_type == "file":
            # Text file: metadata-only reference
            from pathlib import Path as _Path
            name = att.get("name") or _Path(path).name
            mime = att.get("mime_type") or "text/plain"
            parts.append({"type": "text", "text": f"[Attached file: {name} | type: {mime} | path: {path}]"})
        else:
            # Image: file:// reference
            parts.append({"type": "image_url", "image_url": {"url": f"file://{path}"}})
    if not parts:
        return text
    return [{"type": "text", "text": text}] + parts


class SessionMixin:
    """提供会话管理与 inbound/outbound 消息队列方法。

    运行时 self 是 SupervisorState 实例。
    """

    # ---- session generation ----

    def _next_session_generation_locked(self, session_id: str) -> int:
        cur = int(self.session_generations.get(session_id, 0) or 0) + 1
        self.session_generations[session_id] = cur
        return cur

    def _current_session_generation_locked(self, session_id: str) -> int:
        return int(self.session_generations.get(session_id, 0) or 0)

    # ---- 引擎心跳 ----

    def mark_engine_seen(self, *, worker_id: str) -> None:
        with self._lock:
            self._engine_last_seen_at = _now()
            self._engine_last_worker_id = worker_id

    def engine_seen_snapshot(self) -> tuple[datetime | None, str | None]:
        with self._lock:
            return self._engine_last_seen_at, self._engine_last_worker_id

    # ---- 工具热重载信号 ----

    def bump_tools_reload(self) -> int:
        with self._lock:
            self._tools_reload_seq += 1
            return self._tools_reload_seq

    def tools_reload_seq(self) -> int:
        with self._lock:
            return self._tools_reload_seq

    # ---- inbound 游标 ----

    def _advance_inbound_cursor(self) -> None:
        while self._inbound_cursor < len(self._inbound_order):
            seq = self._inbound_order[self._inbound_cursor]
            if seq in self._inbound_processed or seq in self._inbound_routed:
                self._inbound_cursor += 1
                continue
            break

    # ---- 事件 apply（rebuild 用） ----

    def _apply_session_created(self, session_id: str, payload: dict[str, Any]) -> None:
        if not session_id:
            return
        created_at = _now()
        info = SessionInfo(
            session_id=str(session_id or ""),
            channel=str(payload.get("channel") or ""),
            conversation_key=str(payload.get("conversation_key") or ""),
            created_at=created_at,
            updated_at=created_at,
        )
        self.sessions[info.session_id] = info
        conv = info.conversation_key
        if conv:
            self.conversation_map[conv] = info.session_id

    def _apply_inbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        if not isinstance(seq, int) or seq <= 0 or not session_id:
            return
        if seq not in self._inbound_events:
            self._inbound_events[seq] = {"session_id": session_id, "payload": payload}
            self._inbound_order.append(seq)

    def _apply_inbound_processed(self, payload: dict[str, Any]) -> None:
        try:
            inbound_seq = int(payload.get("inbound_seq", 0))
        except Exception:
            return
        if inbound_seq > 0:
            self._inbound_processed.add(inbound_seq)

    def _apply_outbound_message(self, *, seq: int, session_id: str, payload: dict[str, Any]) -> None:
        if not session_id or not isinstance(payload, dict):
            return
        src = payload.get("source_inbound_seq")
        try:
            inbound_seq = int(src) if src is not None else 0
        except Exception:
            inbound_seq = 0
        if inbound_seq > 0 and inbound_seq not in self._inbound_routed:
            self._inbound_routed[inbound_seq] = {
                "action": "reply",
                "session_id": session_id,
                "event_seq": int(seq or 0),
            }

    # ---- 公开方法：会话操作 ----

    def cancel_session(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        with self._lock:
            if sid not in self.sessions:
                return False
            generation = self._next_session_generation_locked(sid)
            self._cancelled_sessions.add(sid)
            self._cancel_session_tasks_locked(sid)
            self.eventlog.append(
                session_id=sid,
                component="supervisor",
                type_="cancel_requested",
                payload={"session_id": sid, "ts": _now().isoformat(), "session_generation": generation},
            )
            return True

    def is_cancelled(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._cancelled_sessions

    def clear_cancelled(self, session_id: str) -> None:
        with self._lock:
            self._cancelled_sessions.discard(session_id)

    def is_task_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get((task_id or "").strip())
            if task is None:
                return True
            current_gen = self._current_session_generation_locked(task.session_id)
            if current_gen and task.session_generation != current_gen:
                return True
            return task.cancel_requested or task.status == TaskStatus.cancelled

    def record_inbound_message_event(self, evt: dict[str, Any]) -> None:
        if not isinstance(evt, dict) or evt.get("type") != "inbound_message":
            return
        try:
            seq = int(evt.get("seq", 0))
        except Exception:
            seq = 0
        session_id = str(evt.get("session_id") or "")
        payload = evt.get("payload") or {}
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._apply_inbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()
            if session_id and session_id in self.sessions:
                self._create_entry_task_for_inbound_locked(inbound_seq=seq, session_id=session_id, payload=payload)

    def record_outbound_message_event(self, evt: dict[str, Any]) -> None:
        if not isinstance(evt, dict) or evt.get("type") != "outbound_message":
            return
        try:
            seq = int(evt.get("seq", 0))
        except Exception:
            seq = 0
        session_id = str(evt.get("session_id") or "")
        payload = evt.get("payload") or {}
        if not session_id or not isinstance(payload, dict):
            return
        with self._lock:
            self._apply_outbound_message(seq=seq, session_id=session_id, payload=payload)
            self._advance_inbound_cursor()

    # ---- inbound 队列 ----

    def assign_next_inbound(self, *, worker_id: str, lease_sec: float = 30.0) -> dict[str, Any] | None:
        wid = (worker_id or "").strip()
        if not wid:
            return None
        with self._lock:
            now = _now()
            lease_val = max(5.0, min(lease_sec, 120.0))
            for i in range(self._inbound_cursor, len(self._inbound_order)):
                seq = self._inbound_order[i]

                if seq in self._inbound_processed or seq in self._inbound_routed:
                    if i == self._inbound_cursor:
                        self._inbound_cursor += 1
                    self._inbound_leases.pop(seq, None)
                    continue

                lease = self._inbound_leases.get(seq)
                if lease is not None and lease.expires_at > now:
                    continue

                evt = self._inbound_events.get(seq)
                if not isinstance(evt, dict):
                    continue

                session_id = str(evt.get("session_id") or "")
                payload = evt.get("payload")
                if not session_id or not isinstance(payload, dict):
                    continue

                self._inbound_leases[seq] = self._InboundLease(
                    worker_id=wid,
                    expires_at=now + timedelta(seconds=lease_val),
                )
                return {"inbound_seq": seq, "session_id": session_id, **payload}

            return None

    def ack_inbound(self, *, inbound_seq: int, worker_id: str) -> bool:
        wid = (worker_id or "").strip()
        if not wid:
            return False
        try:
            seq = int(inbound_seq)
        except Exception:
            seq = 0
        if seq <= 0:
            return False

        with self._lock:
            if seq in self._inbound_processed:
                return True
            if seq not in self._inbound_events:
                return False
            self.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="shell",
                type_="inbound_processed",
                payload={"inbound_seq": seq, "worker_id": wid, "ts": _now().isoformat()},
            )
            self._inbound_processed.add(seq)
            self._inbound_leases.pop(seq, None)
            self._advance_inbound_cursor()
            return True

    # ---- outbound / session 创建 ----

    def get_or_create_session(self, *, channel: str, conversation_key: str) -> str:
        with self._lock:
            # 方案 C: 检查映射指向的 session 是否真实存在，清除幽灵映射
            existing_sid = self.conversation_map.get(conversation_key)
            if existing_sid is not None:
                if existing_sid in self.sessions:
                    return existing_sid
                # 幽灵映射：conversation_map 有记录但 sessions 中无对应条目，清除
                self.conversation_map.pop(conversation_key, None)

            session_id = str(uuid.uuid4())
            created_at = _now()

            info = SessionInfo(
                session_id=session_id,
                channel=channel,
                conversation_key=conversation_key,
                created_at=created_at,
                updated_at=created_at,
            )
            self.sessions[session_id] = info
            self.conversation_map[conversation_key] = session_id

            # 方案 A: 持久化到 sessions.json
            self._session_store.on_session_created(info)

            self.eventlog.append(
                session_id=session_id,
                component="supervisor",
                type_="session_created",
                payload={
                    "session_id": session_id,
                    "channel": channel,
                    "conversation_key": conversation_key,
                    "created_at": created_at.isoformat(),
                },
            )
            return session_id

    def append_outbound_message(
        self,
        *,
        session_id: str,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        source_inbound_seq: int | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        text_clean = str(text or "").strip()
        if not text_clean and not attachments:
            return {"ok": False, "reason": "empty"}

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                v = int(source_inbound_seq)
            except Exception:
                v = 0
            if v > 0:
                src_seq = v

        with self._lock:
            if session_id not in self.sessions:
                raise KeyError("session not found")

            if src_seq is not None:
                inbound_evt = self._inbound_events.get(src_seq)
                if not isinstance(inbound_evt, dict):
                    raise ValueError(f"source_inbound_seq not found: {src_seq}")
                if str(inbound_evt.get("session_id") or "") != session_id:
                    raise ValueError("source_inbound_seq session mismatch")

                existing = self._inbound_routed.get(src_seq)
                if isinstance(existing, dict) and existing.get("action") == "reply":
                    return {"ok": True, "deduped": True, "route": existing}

            payload: dict[str, Any] = {"text": text_clean}
            if attachments:
                payload["attachments"] = list(attachments)
            if src_seq is not None:
                payload["source_inbound_seq"] = src_seq
            if node_id:
                payload["node_id"] = node_id

            evt = self.eventlog.append(
                session_id=session_id,
                component="shell",
                type_="outbound_message",
                payload=payload,
            )

            if src_seq is not None and src_seq not in self._inbound_routed:
                self._inbound_routed[src_seq] = {
                    "action": "reply",
                    "session_id": session_id,
                    "event_seq": int(evt.get("seq", 0) or 0),
                }

            return {"ok": True, "deduped": False, "event_seq": int(evt.get("seq", 0) or 0)}

    def reset_conversation(self, *, conversation_key: str) -> dict[str, Any]:
        """Reset a conversation by removing the conversation_map entry.

        Next inbound message with this conversation_key will create a fresh session.
        Also cleans up node_contexts for the old session.

        Child Session 隔离（Phase C）：级联清理所有关联的 child session，
        包括 JSONL 文件、映射表条目、sessions.json 标记。
        """
        with self._lock:
            old_session_id = self.conversation_map.pop(conversation_key, None)
            if not old_session_id:
                return {"ok": False, "error": f"conversation not found: {conversation_key}"}

            # 方案 A: 标记 session 为已重置
            self._session_store.on_session_reset(old_session_id)

            # Child Session 隔离（Phase C）：级联清理所有关联的 child session
            cleared_children = 0
            conv_dir = self.workspace_root / "data" / "conversations"

            # 删除主 session 的 JSONL
            main_jsonl = conv_dir / f"{old_session_id}.jsonl"
            if main_jsonl.exists():
                try:
                    main_jsonl.unlink()
                except Exception:
                    pass

            # 删除所有关联 child session 的 JSONL 并标记 reset
            child_ids = self.parent_children.pop(old_session_id, set())
            for child_sid in child_ids:
                child_jsonl = conv_dir / f"{child_sid}.jsonl"
                if child_jsonl.exists():
                    try:
                        child_jsonl.unlink()
                    except Exception:
                        pass
                self._session_store.on_session_reset(child_sid)
                cleared_children += 1

            # 清理 child_session_map 中所有以 old_session_id 为 parent 的条目
            keys_to_remove = [k for k in self.child_session_map if k[0] == old_session_id]
            for k in keys_to_remove:
                del self.child_session_map[k]

            return {"ok": True, "old_session_id": old_session_id,
                    "conversation_key": conversation_key,
                    "cleared_children": cleared_children}

    def session_messages(self, *, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        tool_records: list[str] = []
        for e in self.eventlog.events:
            if e.get("session_id") != session_id:
                continue
            et = e.get("type")
            payload = e.get("payload") or {}
            if et == "inbound_message":
                tool_records.clear()
                text = payload.get("text")
                if isinstance(text, str):
                    inbound_atts = payload.get("attachments")
                    if isinstance(inbound_atts, list) and inbound_atts:
                        msgs.append({"role": "user", "content": _build_multimodal_content(text, inbound_atts)})
                    else:
                        msgs.append({"role": "user", "content": text})
            elif et == "handoff_progress":
                prog_msg = str(payload.get("message") or "")
                # 跳过流式文本/思考块，只保留工具调用进度
                kind = payload.get("kind")
                if kind in ("text", "thinking"):
                    continue
                if prog_msg:
                    tool_records.append(prog_msg)
            elif et == "outbound_message":
                text = payload.get("text")
                outbound_atts = payload.get("attachments")
                if isinstance(text, str) or (isinstance(outbound_atts, list) and outbound_atts):
                    text_str = str(text or "")
                    # 将本轮工具调用记录注入助手消息，供后续轮次上下文使用
                    if tool_records:
                        tool_section = "\n".join(tool_records)
                        text_str = f"[本轮工具调用]\n{tool_section}\n\n{text_str}"
                    if isinstance(outbound_atts, list) and outbound_atts:
                        msgs.append({"role": "assistant", "content": _build_multimodal_content(text_str, outbound_atts)})
                    else:
                        msgs.append({"role": "assistant", "content": text_str})
                    tool_records.clear()
        if limit > 0:
            msgs = msgs[-limit:]
        return msgs

    # ---- 上下文窗口用量 ----

    def update_context_usage(self, session_id: str, payload: dict[str, Any]) -> None:
        """更新 session 的上下文窗口用量（由 engine 上报）。"""
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        if not session_id or not usage:
            return
        with self._lock:
            self._session_context_usage[session_id] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "node_id": str(payload.get("node_id") or ""),
                "task_id": str(payload.get("task_id") or ""),
                "updated_at": _now().isoformat(),
            }

    def get_session_context_usage(self, session_id: str) -> dict[str, Any]:
        """获取 session 的上下文窗口用量，包含实际值、估算值和利用率。"""
        from clonoth_runtime import get_int, load_runtime_config

        with self._lock:
            usage = dict(self._session_context_usage.get(session_id) or {})

        msgs = self.session_messages(session_id=session_id, limit=0)
        estimated = self._estimate_tokens_from_messages(msgs)

        runtime_cfg = load_runtime_config(self.workspace_root)
        compact_threshold = get_int(
            runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0,
        )

        prompt_tokens = usage.get("prompt_tokens")
        source = "llm_usage" if prompt_tokens is not None else "estimate"
        effective_tokens = prompt_tokens if prompt_tokens is not None else estimated
        utilization = (
            round(effective_tokens / compact_threshold, 4)
            if compact_threshold > 0
            else 0.0
        )

        return {
            "session_id": session_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_tokens": estimated,
            "effective_tokens": effective_tokens,
            "source": source,
            "node_id": usage.get("node_id", ""),
            "task_id": usage.get("task_id", ""),
            "compact_threshold": compact_threshold,
            "utilization": utilization,
            "updated_at": usage.get("updated_at"),
            "message_count": len(msgs),
        }

    @staticmethod
    def _estimate_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
        """从消息列表估算 token 数（约 3 字符/token，适用于中英混合内容）。"""
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total_chars += len(part["text"])
        return total_chars // 3
