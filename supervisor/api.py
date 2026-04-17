from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response

from .config_store import ConfigStore
from .process_manager import ProcessManager
from .state import SupervisorState
from .types import (
    AdminStateOut,
    AppConfigPublic,
    Approval,
    ApprovalDecisionIn,
    ApprovalRequestIn,
    ApprovalStatus,
    ConfigReloadOut,
    Event,
    HandoffEventIn,
    HealthOut,
    InboundAckIn,
    InboundAckOut,
    InboundMessageIn,
    InboundMessageOut,
    InboundWorkItem,
    OpenAIConfigPublic,
    OpenAIConfigSecret,
    OpenAIConfigUpdateIn,
    OpRequestIn,
    OpRequestOut,
    OutboundMessageIn,
    OutboundMessageOut,
    RestartIn,
    RestartOut,
    Task,
    TaskCompleteIn,
    TaskKind,
    TaskStatus,
)
from .admin_api import create_admin_router
from .admin_api import get_admin_token, verify_admin_token


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    *,
    state: SupervisorState,
    process_manager: ProcessManager | None,
    config_store: ConfigStore,
) -> FastAPI:
    app = FastAPI(title="Clonoth Supervisor", version="0.1.0")
    app.state.state = state
    app.state.process_manager = process_manager
    app.state.config_store = config_store

    @app.get("/v1/health", response_model=HealthOut)
    async def health() -> HealthOut:
        st: SupervisorState = app.state.state
        uptime = (_now() - st.started_at).total_seconds()
        return HealthOut(
            run_id=st.eventlog.run_id, started_at=st.started_at,
            uptime_seconds=uptime,
            workspace_root=str(st.workspace_root),
        )

    @app.get("/v1/config", response_model=AppConfigPublic)
    async def get_config() -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_public()

    @app.get("/v1/config/openai", response_model=OpenAIConfigPublic)
    async def get_openai_config_public() -> OpenAIConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_public()

    @app.get("/v1/config/openai/secret", response_model=OpenAIConfigSecret)
    async def get_openai_config_secret() -> OpenAIConfigSecret:
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_secret()

    @app.post("/v1/config/openai", response_model=AppConfigPublic)
    async def update_openai_config(body: OpenAIConfigUpdateIn) -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        out = cs.update_openai(body)
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="config_updated",
            payload={
                "provider": out.provider,
                "openai": out.openai.model_dump(mode="json"),
                "ts": _now().isoformat(),
            },
        )
        return out

    @app.post("/v1/config/reload", response_model=ConfigReloadOut)
    async def reload_config() -> ConfigReloadOut:
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        cs.reload()
        out = cs.get_public()
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="config_reloaded",
            payload={"ts": _now().isoformat()},
        )
        return ConfigReloadOut(ok=True, config=out)

    @app.post("/v1/inbound", response_model=InboundMessageOut)
    async def inbound(msg: InboundMessageIn) -> InboundMessageOut:
        st: SupervisorState = app.state.state
        session_id = st.get_or_create_session(channel=msg.channel, conversation_key=msg.conversation_key)

        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_="inbound_message",
            payload=msg.model_dump(),
        )
        st.record_inbound_message_event(evt)
        inbound_seq = int(evt.get("seq", 0) or 0)
        return InboundMessageOut(session_id=session_id, inbound_seq=inbound_seq, accepted=True)

    @app.get("/v1/inbound/next", response_model=InboundWorkItem)
    async def inbound_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(30.0, ge=1.0, le=600.0),
    ) -> InboundWorkItem:
        st: SupervisorState = app.state.state
        st.mark_engine_seen(worker_id=worker_id)
        item = st.assign_next_inbound(worker_id=worker_id, lease_sec=float(lease_sec))
        if item is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return InboundWorkItem.model_validate(item)

    @app.post("/v1/inbound/{inbound_seq}/ack", response_model=InboundAckOut)
    async def inbound_ack(inbound_seq: int, body: InboundAckIn) -> InboundAckOut:
        st: SupervisorState = app.state.state
        ok = st.ack_inbound(inbound_seq=int(inbound_seq), worker_id=body.worker_id)
        if not ok:
            raise HTTPException(status_code=404, detail="inbound item not found")
        return InboundAckOut(ok=True)

    @app.get("/v1/tasks/next", response_model=Task)
    async def task_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(120.0, ge=1.0, le=3600.0),
    ) -> Task:
        st: SupervisorState = app.state.state
        st.mark_engine_seen(worker_id=worker_id)
        item = st.assign_next_task(worker_id=worker_id, lease_sec=float(lease_sec))
        if item is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return Task.model_validate(item)

    @app.post("/v1/tasks/{task_id}/complete")
    async def task_complete(task_id: str, body: TaskCompleteIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        task = st.complete_task(task_id=task_id, worker_id=body.worker_id, result=dict(body.result or {}))
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"ok": True, "task_id": task.task_id, "status": task.status.value}

    @app.get("/v1/tasks/{task_id}/cancelled")
    async def task_cancelled(task_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_task_cancelled(task_id)}

    @app.post("/v1/tasks/{task_id}/preempt")
    async def task_preempt(task_id: str, request: Request) -> dict[str, Any]:
        """Bot 调用：标记单个 task 为 preempt_requested。"""
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        msg = body.get("message", "")
        atts = body.get("attachments", [])
        st: SupervisorState = app.state.state
        ok = st.preempt_task(task_id, message=msg, attachments=atts)
        if not ok:
            raise HTTPException(status_code=404, detail="task not found or not active")
        return {"ok": True, "task_id": task_id}

    @app.get("/v1/tasks/{task_id}/preempted")
    async def task_preempted(task_id: str) -> dict[str, Any]:
        """Engine 查询：task 是否被请求 preempt。"""
        st: SupervisorState = app.state.state
        return st.is_task_preempted(task_id)

    @app.post("/v1/tasks/{task_id}/preempt_consumed")
    async def task_preempt_consumed(task_id: str) -> dict[str, Any]:
        """Engine 读取完 preempt message 后调用，清空 message 防止重复注入。"""
        st: SupervisorState = app.state.state
        result = st.consume_preempt_message(task_id)
        return {"ok": True, **result}

    @app.get("/v1/sessions/{session_id}/running_tasks")
    async def session_running_tasks(session_id: str) -> dict[str, Any]:
        """Bot 查询当前 session 中 running/pending 状态的 task 列表。
        自动收割 lease 过期超过 grace period 的僵尸 task。
        跳过 session 中存在 pending approval 的 task（等审批不算僵尸）。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        now = _now()
        _GRACE = timedelta(seconds=180)
        # 检查该 session 是否有 pending approval
        _has_pending_approval = any(
            a.status == ApprovalStatus.pending and a.session_id == session_id
            for a in st.approvals.values()
        )
        tasks: list[dict[str, Any]] = []
        for task in st.tasks.values():
            if task.session_id != session_id:
                continue
            if task.status not in (TaskStatus.running, TaskStatus.pending):
                continue
            # 收割僵尸：running + lease 过期超过 grace period
            # 但如果 session 有 pending approval，跳过回收（等审批是合法阻塞）
            if (task.status == TaskStatus.running and task.lease_expires_at
                    and task.lease_expires_at + _GRACE < now
                    and not _has_pending_approval):
                task.status = TaskStatus.failed
                task.updated_at = now
                task.lease_expires_at = None
                task.result = {"action": "fail", "error": "lease expired (zombie reaped)"}
                # 写事件，使 events.jsonl 与内存状态一致
                st.eventlog.append(
                    session_id=task.session_id,
                    component="supervisor",
                    type_="task_completed",
                    payload=task.model_dump(mode="json"),
                )
                continue
            _is_async = bool(task.input.get("_async_dispatch"))
            _is_system = bool(task.input.get("_system_task"))
            tasks.append({
                "task_id": task.task_id,
                "node_id": task.node_id or "",
                "status": task.status.value,
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "caller_task_id": task.caller_task_id or "",
                "is_user_entry": bool(not task.caller_task_id and not _is_async and not _is_system),
                "source_inbound_seq": task.source_inbound_seq,
            })
        return {"tasks": tasks}

    @app.post("/v1/tasks/{task_id}/renew_lease")
    async def renew_lease(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        lease_sec = float(body.get("lease_sec", 120.0))
        ok = st.renew_lease(task_id, worker_id, lease_sec)
        return {"ok": ok}

    @app.post("/v1/engine/register")
    async def engine_register(body: dict[str, Any]) -> dict[str, Any]:
        """Engine worker registers itself with a generation ID on startup.

        Direction 2: triggers cleanup of orphaned tasks from previous generations.
        """
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        generation_id = str(body.get("generation_id") or "").strip()
        if not worker_id or not generation_id:
            raise HTTPException(status_code=400, detail="worker_id and generation_id required")
        return st.register_engine(worker_id, generation_id)

    @app.get("/v1/tools/reload-seq")
    async def tools_reload_seq() -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"seq": st.tools_reload_seq()}

    @app.post("/v1/tools/reload")
    async def tools_reload_trigger() -> dict[str, Any]:
        st: SupervisorState = app.state.state
        seq = st.bump_tools_reload()
        return {"ok": True, "seq": seq}

    @app.post("/v1/sessions/{session_id}/outbound", response_model=OutboundMessageOut)
    async def session_outbound(session_id: str, body: OutboundMessageIn) -> OutboundMessageOut:
        st: SupervisorState = app.state.state
        try:
            st.append_outbound_message(
                session_id=session_id,
                text=str(body.text or ""),
                attachments=body.attachments,
                source_inbound_seq=body.source_inbound_seq,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "bad request")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e) or "conflict")
        return OutboundMessageOut(ok=True)

    @app.get("/v1/sessions/{session_id}/events", response_model=list[Event])
    async def session_events(
        session_id: str,
        after_seq: int = Query(0, ge=0),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.list_events(session_id=session_id, after_seq=after_seq)
        out: list[Event] = []
        for e in evts:
            try:
                out.append(
                    Event(
                        schema_version=int(e.get("schema_version", 1)),
                        seq=int(e.get("seq", 0)),
                        event_id=str(e.get("event_id")),
                        ts=datetime.fromisoformat(str(e.get("ts"))),
                        run_id=str(e.get("run_id")),
                        session_id=str(e.get("session_id")),
                        component=str(e.get("component")),
                        type=str(e.get("type")),
                        payload=dict(e.get("payload") or {}),
                    )
                )
            except Exception:
                continue
        return out

    @app.get("/v1/events", response_model=list[Event])
    async def global_events(
        after_seq: int = Query(0, ge=0),
        types: str = Query("", description="comma-separated event types to filter"),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.eventlog.list_all_events(after_seq=after_seq)
        type_filter = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        out: list[Event] = []
        for e in evts:
            if type_filter and str(e.get("type")) not in type_filter:
                continue
            try:
                out.append(Event(
                    schema_version=int(e.get("schema_version", 1)),
                    seq=int(e.get("seq", 0)),
                    event_id=str(e.get("event_id")),
                    ts=datetime.fromisoformat(str(e.get("ts"))),
                    run_id=str(e.get("run_id")),
                    session_id=str(e.get("session_id")),
                    component=str(e.get("component")),
                    type=str(e.get("type")),
                    payload=dict(e.get("payload") or {}),
                ))
            except Exception:
                continue
        return out

    @app.post("/v1/sessions/{session_id}/events")
    async def session_event(session_id: str, ev: HandoffEventIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        transient = ev.type in {"stream_delta", "stream_end"}
        if ev.type == "context_usage":
            transient = True
        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_=ev.type,
            payload=dict(ev.payload or {}),
            transient=transient,
        )
        if ev.type == "outbound_message":
            st.record_outbound_message_event(evt)
        if ev.type == "context_usage":
            st.update_context_usage(session_id, dict(ev.payload or {}))
        return {"ok": True}

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = Query(50, ge=0, le=500)) -> list[dict[str, Any]]:
        st: SupervisorState = app.state.state
        return st.session_messages(session_id=session_id, limit=limit)

    @app.post("/v1/sessions/{session_id}/cancel")
    async def session_cancel(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        ok = st.cancel_session(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/cancelled")
    async def session_cancelled(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_cancelled(session_id)}

    @app.post("/v1/sessions/{session_id}/cancel/clear")
    async def session_cancel_clear(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        st.clear_cancelled(session_id)
        return {"ok": True}

    @app.post("/v1/conversations/reset")
    async def conversation_reset(body: dict[str, Any]) -> dict[str, Any]:
        """Reset a conversation, forcing next message to create a new session."""
        st: SupervisorState = app.state.state
        conv_key = str(body.get("conversation_key") or "").strip()
        if not conv_key:
            raise HTTPException(status_code=400, detail="conversation_key required")
        result = st.reset_conversation(conversation_key=conv_key)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "not found"))
        # emit context_reset 事件，通知 bot 侧重置高水位
        old_sid = result.get("old_session_id", "")
        if old_sid:
            st.eventlog.append(
                session_id=old_sid,
                component="supervisor",
                type_="context_reset",
                payload={"conversation_key": conv_key, "reason": "clear"},
            )
        # Also clean up node_contexts for old session
        if old_sid:
            from engine.context_store import cleanup_session_contexts
            try:
                cleaned = cleanup_session_contexts(st.workspace_root, old_sid)
                result["context_files_cleaned"] = cleaned
            except Exception:
                pass
        return result

    @app.post("/v1/tasks/{task_id}/cancel")
    async def task_cancel(task_id: str) -> dict[str, Any]:
        """取消单个 task 及其所有子任务链。"""
        st: SupervisorState = app.state.state
        result = st.cancel_single_task(task_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "cancel failed"))
        return result

    @app.post("/v1/sessions/{session_id}/cancel_active_tasks")
    async def session_cancel_active_tasks(
        session_id: str, exclude_task_id: str = Query(""),
    ) -> dict[str, Any]:
        """取消 session 中所有活跃 task。供 AI 工具调用。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        return st.cancel_active_tasks(session_id, exclude_task_id=exclude_task_id)

    @app.post("/v1/sessions/{session_id}/switch_node")
    async def session_switch_node(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """AI 或外部调用：设置/清除 session 级入口节点覆盖。"""
        st: SupervisorState = app.state.state
        target = str(body.get("target_node_id") or "").strip()
        return st.switch_session_node(session_id, target)

    @app.get("/v1/sessions/{session_id}/active_node")
    async def session_active_node(session_id: str) -> dict[str, Any]:
        """查询 session 当前实际使用的入口节点。"""
        st: SupervisorState = app.state.state
        return st.get_session_active_node(session_id)

    @app.get("/v1/sessions/{session_id}/context_window")
    async def session_context_window(session_id: str) -> dict[str, Any]:
        """获取 session 当前上下文窗口的 token 用量信息。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        return st.get_session_context_usage(session_id)

    @app.post("/v1/approvals/request", response_model=Approval)
    async def approval_request(inp: ApprovalRequestIn) -> Approval:
        st: SupervisorState = app.state.state
        return st.create_approval(session_id=inp.session_id, operation=inp.operation, details=inp.details)

    @app.get("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_get(approval_id: str) -> Approval:
        st: SupervisorState = app.state.state
        if approval_id not in st.approvals:
            raise HTTPException(status_code=404, detail="approval not found")
        return st.approvals[approval_id]

    @app.post("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_decide(approval_id: str, body: ApprovalDecisionIn) -> Approval:
        st: SupervisorState = app.state.state
        a = st.decide_approval(approval_id=approval_id, decision=body.decision, comment=body.comment)
        if a is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return a

    @app.post("/v1/ops/request", response_model=OpRequestOut)
    async def ops_request(inp: OpRequestIn) -> OpRequestOut:
        st: SupervisorState = app.state.state
        return st.request_operation(session_id=inp.session_id, op=inp.op, parameters=inp.parameters)

    @app.get("/v1/admin/state", response_model=AdminStateOut)
    async def admin_state() -> AdminStateOut:
        st: SupervisorState = app.state.state
        return st.admin_state()

    @app.post("/v1/admin/restart", response_model=RestartOut)
    async def admin_restart(inp: RestartIn) -> RestartOut:
        pm: ProcessManager | None = app.state.process_manager
        st: SupervisorState = app.state.state

        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="restart_requested",
            payload={
                "target": inp.target,
                "reason": inp.reason,
                "approval_id": inp.approval_id,
                "ts": _now().isoformat(),
            },
        )

        if inp.target == "engine":
            if pm is None:
                raise HTTPException(status_code=409, detail="process manager not enabled")
            # 重新加载 .env，确保修改后的环境变量在 supervisor 进程中生效
            try:
                from dotenv import load_dotenv
                load_dotenv(override=True)
            except Exception:
                pass
            # 先注入 outbound 让 handle_agent 正常闭合（log embed 有终态）
            if inp.session_id:
                # 找到当前 session 活跃任务的 source_inbound_seq，
                # 确保 Bot 端 poller 能匹配到 trigger 并正确关闭 status_msg
                _restart_src_seq = None
                for _rt in st.tasks.values():
                    if (_rt.session_id == inp.session_id
                            and _rt.status in (TaskStatus.running, TaskStatus.pending)
                            and _rt.source_inbound_seq):
                        _restart_src_seq = _rt.source_inbound_seq
                        break
                _restart_outbound_payload: dict[str, Any] = {"text": "✅ 已触发 Engine 重启，正在执行..."}
                if _restart_src_seq:
                    _restart_outbound_payload["source_inbound_seq"] = _restart_src_seq
                st.eventlog.append(
                    session_id=inp.session_id,
                    component="supervisor",
                    type_="outbound_message",
                    payload=_restart_outbound_payload,
                )
            pm.restart_engine()
            # ---- 清理旧 engine 遗留的孤儿 task ----
            _orphan_count = st.cancel_orphaned_tasks()
            if _orphan_count:
                st.eventlog.append(
                    session_id="__system__",
                    component="supervisor",
                    type_="orphan_cleanup",
                    payload={
                        "count": _orphan_count,
                        "trigger": "engine_restart",
                        "ts": _now().isoformat(),
                    },
                )
            st.eventlog.append(
                session_id="__system__",
                component="supervisor",
                type_="restart_completed",
                payload={"target": inp.target, "ts": _now().isoformat()},
            )
            if inp.session_id:
                _si = st.sessions.get(inp.session_id)
                if _si:
                    _restart_evt = st.eventlog.append(
                        session_id=inp.session_id,
                        component="supervisor",
                        type_="inbound_message",
                        payload={
                            "channel": _si.channel,
                            "conversation_key": _si.conversation_key,
                            "text": "[系统通知] Engine 重启已完成，新代码已生效。",
                        },
                    )
                    st.record_inbound_message_event(_restart_evt)
            return RestartOut(scheduled=True, target=inp.target)

        # --no-shell 模式下没有 _watch_shell 线程，需要直接退出
        # 先停 engine，再延迟退出让 HTTP 响应发出去
        if inp.session_id:
            _pending_path = Path(st.workspace_root) / "data" / "restart_pending.json"
            _si = st.sessions.get(inp.session_id)
            try:
                _pending_path.write_text(json.dumps({
                    "session_id": inp.session_id,
                    "target": "all",
                    "conversation_key": _si.conversation_key if _si else "",
                    "channel": _si.channel if _si else "",
                    "ts": _now().isoformat(),
                }), encoding="utf-8")
            except Exception:
                pass
            # 找 source_inbound_seq
            _all_restart_src_seq = None
            for _art in st.tasks.values():
                if (_art.session_id == inp.session_id
                        and _art.status in (TaskStatus.running, TaskStatus.pending)
                        and _art.source_inbound_seq):
                    _all_restart_src_seq = _art.source_inbound_seq
                    break
            _all_restart_payload: dict[str, Any] = {"text": "✅ 已触发全量重启，系统即将重启..."}
            if _all_restart_src_seq:
                _all_restart_payload["source_inbound_seq"] = _all_restart_src_seq
            st.eventlog.append(
                session_id=inp.session_id,
                component="supervisor",
                type_="outbound_message",
                payload=_all_restart_payload,
            )

        def _deferred_exit() -> None:
            time.sleep(1)
            if pm is not None:
                try:
                    pm.stop_engine()
                except Exception:
                    pass
            os._exit(75)  # main.py 外层循环检测到 75 会重启

        if pm is not None:
            pm._restart_pending = True
        threading.Thread(target=_deferred_exit, daemon=True, name="restart-all").start()
        return RestartOut(scheduled=True, target="all")

    # ---- 异步委派 API ----
    @app.post("/v1/tasks/dispatch-async")
    async def dispatch_async(request: Request) -> dict[str, Any]:
        """异步委派子节点：创建子任务后立即返回 task_id，父任务不挂起。"""
        st: SupervisorState = app.state.state
        body = await request.json()

        session_id = str(body.get("session_id") or "").strip()
        session_generation = int(body.get("session_generation", 1))
        node_id = str(body.get("node_id") or "").strip()
        instruction = str(body.get("instruction") or "").strip()
        context_mode = str(body.get("context_mode") or "accumulate").strip()
        context_key = str(body.get("context_key") or "").strip() or None
        source_inbound_seq = body.get("source_inbound_seq")
        caller_node_id = str(body.get("caller_node_id") or "").strip()

        if not session_id or not node_id:
            raise HTTPException(status_code=400, detail="session_id and node_id required")
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        input_data: dict[str, Any] = {
            "instruction": instruction,
            "_async_dispatch": True,
            "_caller_node_id": caller_node_id,
        }
        if context_key:
            input_data["_context_key"] = context_key

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                src_seq = int(source_inbound_seq)
            except (ValueError, TypeError):
                pass

        with st._lock:
            # Child Session 隔离（Phase B）：async dispatch 也走 child session
            _child_sid, _is_new = st.get_or_create_child_session(
                session_id, node_id, context_key or "", context_mode,
            )
            input_data["child_session_id"] = _child_sid
            input_data["context_mode"] = context_mode
            input_data["use_context"] = False
            if context_mode == "fork":
                input_data["fork_from_session_id"] = session_id
            # 审计报告 Step 1（2026-04-16）：删除 async dispatch 的 accumulate fallback。
            # engine/runner.py:514 在 child_session_id 非空时会无条件清空 context_ref，
            # 此 fallback 注入永远不会被消费，属于兼容期死代码。

            task = st._create_task_locked(
                session_id=session_id,
                session_generation=session_generation,
                kind=TaskKind.node,
                node_id=node_id,
                input_data=input_data,
                continuation={},
                source_inbound_seq=src_seq,
                caller_task_id=None,
            )

        return {"ok": True, "task_id": task.task_id}
    admin_router = create_admin_router(workspace_root=state.workspace_root)
    app.include_router(admin_router, prefix="/v1/admin/config")

    # 认证校验端点：前端用来验证 token 是否正确
    @app.get("/v1/admin/auth/check")
    async def admin_auth_check(request: Request) -> dict[str, Any]:
        try:
            verify_admin_token(request)
        except HTTPException:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"ok": True}

    from fastapi import Request as _Req  # noqa: already imported above

    admin_dir = state.workspace_root / "public" / "admin"
    admin_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")

    # 启动时打印 token
    token = get_admin_token()
    print(f"[admin] 管理界面地址: http://{{host}}:{{port}}/admin/", flush=True)
    print(f"[admin] 管理 Token: {token}", flush=True)

    return app
