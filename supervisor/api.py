from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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


# [WS events 2026-05-17] Why: WebSocket clients should keep long-lived event
# streams through proxies. How: send an application-level ping at this cadence
# when no EventLog row is available. Purpose: avoid idle timeout without changing
# the EventLog schema.
_WS_HEARTBEAT_SEC = 30.0

# [WS events 2026-05-17] Why: clients may optionally send {"last_seq": n}
# immediately after connect, but old clients may send nothing. How: wait briefly
# for that first message and then default to zero. Purpose: support both replayed
# and fresh streams without blocking connection setup forever.
_WS_INITIAL_MESSAGE_TIMEOUT_SEC = 0.5


def _parse_ws_initial_last_seq(message: str) -> int:
    """Parse the optional initial WebSocket message into a non-negative seq."""
    # [WS events 2026-05-17] Why: the WebSocket handshake is intentionally loose
    # to keep compatibility with simple clients. How: malformed JSON or invalid
    # values fall back to zero. Purpose: clients can connect without a setup frame.
    try:
        data = json.loads(message or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return max(0, int(data.get("last_seq") or 0))
    except Exception:
        return 0


async def _send_ws_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send one JSON object over a WebSocket as UTF-8 text."""
    # [WS events 2026-05-17] Why: EventLog payloads are plain dicts but may later
    # contain values FastAPI's send_json cannot serialize by default. How: use the
    # same explicit json.dumps path for events and ping frames. Purpose: make the
    # wire shape predictable and resilient to harmless non-string values.
    await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))


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
    async def get_openai_config_secret(request: Request) -> OpenAIConfigSecret:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_secret()

    @app.post("/v1/config/openai", response_model=AppConfigPublic)
    async def update_openai_config(body: OpenAIConfigUpdateIn, request: Request) -> AppConfigPublic:
        verify_admin_token(request)
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
    async def reload_config(request: Request) -> ConfigReloadOut:
        verify_admin_token(request)
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

        # [2026-05-28] 异步 dispatch 统一走 inbound：透传新增的 dispatch 字段到 payload。
        # 为什么：model_dump() 已包含这些字段，但 record_inbound_message_event 依赖
        #   payload dict 来传递给 _create_entry_task_for_inbound_locked。
        # 怎么改：无需额外处理，Pydantic model_dump 已包含新字段。
        # 目的：确保 dispatch_origin/dispatch_context_mode/dispatch_fork_from_session
        #   能通过 event payload 传递到 task 创建逻辑。
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

    @app.post("/v1/sessions/{session_id}/async_tool_result")
    async def session_async_tool_result(session_id: str, request: Request) -> dict[str, Any]:
        """Engine 调用：异步工具完成后注入结果到 session。

        复用子节点三级回退：preempt running → 标记 suspended → 创建 inbound。
        """
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        msg = body.get("message", "")
        atts = body.get("attachment_paths", [])
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        result = st.inject_async_result(session_id, text=msg, attachments=atts)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
        return result

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
        tasks: list[dict[str, Any]] = []
        with st._lock:
            # [Fork/Merge 2026-05-12] running_tasks 查询主 session 时也返回入口分支任务。
            # 原因：adapter 以后需要在多个并发 branch 中选择显式 preempt 目标。
            # 做法：把主 session 与 parent→branches 索引合并为查询集合。
            # 目的：端点仍以主 session_id 调用，但能观察所有活跃分支。
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            # 检查该 session 或任一分支是否有 pending approval
            _has_pending_approval = any(
                a.status == ApprovalStatus.pending and a.session_id in session_ids
                for a in st.approvals.values()
            )
            for task in st.tasks.values():
                if task.session_id not in session_ids:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                # 收割僵尸：running + lease 过期超过 grace period
                # 但如果 session 有 pending approval，跳过回收（等审批是合法阻塞）
                # fix: lease_expires_at 为 None 时也视为僵尸，避免无 lease 的 running 任务永远无法被收割
                if (task.status == TaskStatus.running
                        and (not task.lease_expires_at or task.lease_expires_at + _GRACE < now)
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
                    # [Fork/Merge 2026-05-12] 僵尸回收是 fail 终态，也必须走统一路由。
                    # 原因：入口分支被回收时需要 merge 回主 session，并输出错误事件。
                    # 做法：复用 task_router 的 fail 路由。目的：避免 reaped branch 永久悬挂。
                    st._route_completed_task_locked(task)
                    continue
                _is_async = bool(task.input.get("_async_dispatch"))
                _is_system = bool(task.input.get("_system_task"))
                _is_scheduled = bool(task.input.get("schedule_id"))
                branch_session_id = str(task.input.get("branch_session_id") or "")
                if not branch_session_id and task.session_id != session_id:
                    branch_session_id = task.session_id
                tasks.append({
                    "task_id": task.task_id,
                    "node_id": task.node_id or "",
                    "status": task.status.value,
                    "created_at": task.created_at.isoformat() if task.created_at else "",
                    "caller_task_id": task.caller_task_id or "",
                    "is_user_entry": bool(not task.caller_task_id and not _is_async and not _is_system and not _is_scheduled),
                    "source_inbound_seq": task.source_inbound_seq,
                    "branch_session_id": branch_session_id,
                    "parent_session_id": str(task.input.get("parent_session_id") or (session_id if branch_session_id else "")),
                })
        return {"tasks": tasks}

    # [2026-05-28] 全局按 node_id 查找活跃任务（跨 session）。
    # 为什么：dispatch 到持久节点的任务运行在独立 session 上，调用方不知道目标 session_id。
    # 怎么改：新增端点，遍历所有活跃 task，按 node_id 匹配返回第一个。
    # 目的：支持 preempt_task 跨 session 查找持久节点任务。
    @app.get("/v1/tasks/active-by-node/{node_id}")
    async def global_task_by_node(node_id: str) -> dict[str, Any]:
        """[2026-05-28] 全局查找指定 node_id 的活跃任务（running/pending）。

        遍历所有任务，不限定 session。用于跨 session 定位持久节点任务。
        """
        st: SupervisorState = app.state.state
        with st._lock:
            for task in st.tasks.values():
                if task.node_id != node_id:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                return {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "status": task.status.value,
                }
        raise HTTPException(status_code=404, detail=f"no active task for node '{node_id}'")

    # [2026-05-28] 按 node_id 查找 session 中活跃任务。
    # 为什么：preempt_task 原本只接受 task_id（UUID），调用者需知道精确 ID 才能操作。
    # 怎么改：新增端点，遍历 session 内所有活跃 task，按 node_id 匹配返回第一个。
    # 目的：允许 engine 侧用 node_id（如 "bob"）定位子节点任务再执行 preempt。
    @app.get("/v1/sessions/{session_id}/tasks/by-node/{node_id}")
    async def session_task_by_node(session_id: str, node_id: str) -> dict[str, Any]:
        """按 node_id 查找 session 中活跃（running/pending）的 task。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        with st._lock:
            # 与 running_tasks 端点一致，查询主 session 及其入口分支
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            for task in st.tasks.values():
                if task.session_id not in session_ids:
                    continue
                if task.node_id != node_id:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                return {"task_id": task.task_id, "status": task.status.value}
        raise HTTPException(status_code=404, detail=f"no active task for node '{node_id}'")

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
        Direction 2: triggers cleanup of orphaned tasks from previous generations.
        """
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        generation_id = str(body.get("generation_id") or "").strip()
        if not worker_id or not generation_id:
            raise HTTPException(status_code=400, detail="worker_id and generation_id required")
        result = st.register_engine(worker_id, generation_id)
        return result

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
        limit: int = Query(5000, ge=1, le=5000),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.list_events(session_id=session_id, after_seq=after_seq)
        out: list[Event] = []
        for e in evts:
            # Why: session event payloads can include large task snapshots. How:
            # stop conversion once the requested page size is reached. Purpose:
            # keep per-session polling from serializing the whole memory window.
            if len(out) >= limit:
                break
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

    @app.websocket("/v1/sessions/{session_id}/ws")
    async def session_ws(websocket: WebSocket, session_id: str) -> None:
        """Stream durable EventLog rows for one session over WebSocket."""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            await websocket.close(code=4004, reason="session not found")
            return
        await websocket.accept()

        last_seq = 0
        try:
            initial_message = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_WS_INITIAL_MESSAGE_TIMEOUT_SEC,
            )
            last_seq = _parse_ws_initial_last_seq(initial_message)
        except asyncio.TimeoutError:
            last_seq = 0
        except WebSocketDisconnect:
            return
        except Exception:
            last_seq = 0

        # [WS events 2026-05-17] Why: subscribing only after replay creates a race
        # where an event appended between list_events() and subscribe() is lost.
        # How: subscribe first, replay EventLog rows next, then skip queued rows at
        # or below the highest sent seq. Purpose: clients still observe catch-up
        # before live delivery while the server closes the replay/live gap.
        queue = st.eventlog.subscribe(session_id)
        sent_seq = last_seq
        receive_task: asyncio.Task | None = None
        try:
            for evt in st.list_events(session_id=session_id, after_seq=last_seq):
                await _send_ws_json(websocket, evt)
                try:
                    sent_seq = max(sent_seq, int(evt.get("seq", 0) or 0))
                except Exception:
                    pass

            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task},
                    timeout=_WS_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    await _send_ws_json(websocket, {"type": "ping"})
                    continue

                if event_task in done:
                    evt = event_task.result()
                    try:
                        evt_seq = int(evt.get("seq", 0) or 0)
                    except Exception:
                        evt_seq = 0
                    if evt_seq > sent_seq:
                        await _send_ws_json(websocket, evt)
                        sent_seq = evt_seq
                else:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task

                if receive_task in done:
                    try:
                        receive_task.result()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    # [WS events 2026-05-17] Why: clients may send harmless control
                    # frames after the initial last_seq. How: consume and ignore the
                    # text, then wait for the next client frame. Purpose: a normal
                    # client message does not terminate the event stream.
                    receive_task = asyncio.create_task(websocket.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            st.eventlog.unsubscribe(session_id, queue)

    @app.websocket("/v1/ws")
    async def global_ws(websocket: WebSocket) -> None:
        """Stream durable EventLog rows for all sessions over WebSocket."""
        st: SupervisorState = app.state.state
        await websocket.accept()

        last_seq = 0
        try:
            initial_message = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_WS_INITIAL_MESSAGE_TIMEOUT_SEC,
            )
            last_seq = _parse_ws_initial_last_seq(initial_message)
        except asyncio.TimeoutError:
            last_seq = 0
        except WebSocketDisconnect:
            return
        except Exception:
            last_seq = 0

        # [WS events 2026-05-19] Why: the global endpoint must not lose events
        # appended between replay and live subscription. How: subscribe globally
        # before replaying list_all_events(), then skip live rows at or below the
        # highest sent seq. Purpose: give /v1/ws the same replay/live race safety
        # as the existing per-session WebSocket endpoint.
        queue = st.eventlog.subscribe_global()
        sent_seq = last_seq
        receive_task: asyncio.Task | None = None
        try:
            for evt in st.eventlog.list_all_events(after_seq=last_seq):
                await _send_ws_json(websocket, evt)
                try:
                    sent_seq = max(sent_seq, int(evt.get("seq", 0) or 0))
                except Exception:
                    pass

            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task},
                    timeout=_WS_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    await _send_ws_json(websocket, {"type": "ping"})
                    continue

                if event_task in done:
                    evt = event_task.result()
                    try:
                        evt_seq = int(evt.get("seq", 0) or 0)
                    except Exception:
                        evt_seq = 0
                    if evt_seq > sent_seq:
                        await _send_ws_json(websocket, evt)
                        sent_seq = evt_seq
                else:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task

                if receive_task in done:
                    try:
                        receive_task.result()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    # [WS events 2026-05-19] Why: clients may send control frames
                    # after the initial last_seq. How: consume and ignore each text
                    # frame, then wait for another. Purpose: keep global streaming
                    # behavior aligned with the per-session WebSocket endpoint.
                    receive_task = asyncio.create_task(websocket.receive_text())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            st.eventlog.unsubscribe_global(queue)

    @app.get("/v1/events", response_model=list[Event])
    async def global_events(
        after_seq: int = Query(0, ge=0),
        types: str = Query("", description="comma-separated event types to filter"),
        limit: int = Query(5000, ge=1, le=5000),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.eventlog.list_all_events(after_seq=after_seq)
        type_filter = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        out: list[Event] = []
        for e in evts:
            if type_filter and str(e.get("type")) not in type_filter:
                continue
            if len(out) >= limit:
                break
            try:
                # Why: /v1/events may contain large task snapshots even with the
                # in-memory ring bounded. How: honor the explicit page limit
                # during conversion instead of materializing every cached Event.
                # Purpose: prevent polling/debug requests from serializing many
                # megabytes when the caller asks for a small page.
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

        transient = ev.type in {"stream_delta", "stream_end", "tool_call_delta"}
        # [tool-stream 2026-05-19] tool_call_delta 是实时展示事件，不写入 JSONL。
        # 原因：参数片段可能很碎，持久化会膨胀事件日志且与 stream_delta 语义一致。
        # 做法：把它加入 supervisor transient 类型集合。
        # 目的：WebSocket 继续实时广播，但磁盘事件日志只保留稳定状态事件。
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

    @app.get("/v1/sessions")
    async def list_sessions(
        channel: str = Query("", description="Filter by channel (e.g. 'web')"),
        limit: int = Query(50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by channel."""
        st: SupervisorState = app.state.state
        results = []
        for sid, si in st.sessions.items():
            if channel and si.channel != channel:
                continue
            results.append({
                "session_id": si.session_id,
                "conversation_key": si.conversation_key,
                "channel": si.channel,
                "created_at": si.created_at.isoformat() if si.created_at else "",
                "updated_at": si.updated_at.isoformat() if si.updated_at else "",
            })
        # Sort by updated_at desc, most recent first
        results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return results[:limit]

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        """Delete a session and its conversation store."""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        si = st.sessions[session_id]
        # Remove from sessions and conversation_map
        with st._lock:
            del st.sessions[session_id]
            conv_key = si.conversation_key
            if conv_key and st.conversation_map.get(conv_key) == session_id:
                del st.conversation_map[conv_key]
        # Delete ConversationStore JSONL
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            conv_store = ConversationStore(Path(st.workspace_root) / "data" / "conversations")
            conv_store.delete(session_id)
        except Exception:
            pass
        # Clean up node contexts
        try:
            from engine.context_store import cleanup_session_contexts
            cleanup_session_contexts(st.workspace_root, session_id)
        except Exception:
            pass
        # Mark as reset in sessions.json
        try:
            st._session_store.on_session_reset(session_id)
        except Exception:
            pass
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = Query(50, ge=0, le=500)) -> list[dict[str, Any]]:
        st: SupervisorState = app.state.state
        return st.session_messages(session_id=session_id, limit=limit)

    @app.get("/v1/sessions/{session_id}/history")
    async def session_history(session_id: str, limit: int = Query(200, ge=0, le=1000)) -> list[dict[str, Any]]:
        """Structured message history from ConversationStore (for web frontend)."""
        st: SupervisorState = app.state.state
        return st.session_history_structured(session_id=session_id, limit=limit)

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
        session_id: str,
        exclude_task_id: str = Query(""),
        # [2026-05-28] 可选 node_id 过滤：只取消指定节点的活跃任务。
        # 为什么：有时只想取消某个子节点的任务，而非 session 内全部。
        # 怎么改：新增 query param，透传到 cancel_active_tasks 方法。
        # 目的：更细粒度的任务取消控制。
        node_id: str = Query(""),
    ) -> dict[str, Any]:
        """取消 session 中所有活跃 task。供 AI 工具调用。"""
        st: SupervisorState = app.state.state
        with st._lock:
            # [Fork/Merge 2026-05-17] Why: this endpoint can still be called with
            # a branch id from ToolContext in older workers. How: normalize entry
            # branches to the parent before cancellation. Purpose: sibling branches
            # under the same user conversation are included.
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
        return st.cancel_active_tasks(
            route_session_id, exclude_task_id=exclude_task_id,
            node_id=node_id or None,
        )

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
        with st._lock:
            # [Fork/Merge 2026-05-17] Why: context_usage events are emitted on the
            # parent route session while branch sessions are temporary storage.
            # How: normalize entry branches to the parent for this read endpoint.
            # Purpose: get_context_window reports real session usage.
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
        return st.get_session_context_usage(route_session_id)

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
    async def admin_state(request: Request) -> AdminStateOut:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        return st.admin_state()

    @app.post("/v1/admin/restart", response_model=RestartOut)
    async def admin_restart(inp: RestartIn, request: Request) -> RestartOut:
        verify_admin_token(request)
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

        # 2026.4.28: restart engine 暂时禁用，统一走 restart all
        # 原因：engine 子进程终止时会向 supervisor 泄露信号（SIGINT/SIGTERM），
        # 导致 uvicorn 触发优雅退出，supervisor 跟着一起死。
        # 根因是 uvicorn.run() 会覆盖手动设置的信号 handler，
        # 目前未找到稳定的隔离方案，暂时所有重启统一走 restart all。
        if inp.target == "engine_DISABLED":
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
            # Deferred engine restart: return HTTP 200 first, then kill+restart.
            # This ensures the tool call in the dying engine receives its response
            # and can shadow_write the tool_result before being terminated.
            _restart_session_id = inp.session_id
            _restart_target = inp.target

            def _deferred_engine_restart() -> None:
                time.sleep(1)  # let HTTP response reach engine first
                pm._restarting_engine = True  # 抑制信号 handler 退出
                try:
                    pm.stop_engine()
                    time.sleep(1)  # 等待延迟信号消散
                    pm.start_engine()
                    # Brief health check: verify engine process is alive
                    time.sleep(0.5)
                    _alive = any(p.popen.poll() is None for p in pm.engines)
                    if not _alive:
                        st.eventlog.append(
                            session_id=_restart_session_id or "__system__",
                            component="supervisor",
                            type_="outbound_message",
                            payload={"text": "❌ Engine 重启失败：进程启动后立即退出。"},
                        )
                        return
                except Exception as exc:
                    pm._restarting_engine = False
                    st.eventlog.append(
                        session_id=_restart_session_id or "__system__",
                        component="supervisor",
                        type_="outbound_message",
                        payload={"text": f"❌ Engine 重启失败：{exc}"},
                    )
                    return
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
                    payload={"target": _restart_target, "ts": _now().isoformat()},
                )
                # Defer restart notification — will be injected in register_engine()
                # after orphan cleanup, so the task won't be reaped.
                if _restart_session_id:
                    st._pending_restart_notify = _restart_session_id
                pm._restarting_engine = False  # 清除信号抑制

            threading.Thread(target=_deferred_engine_restart, daemon=True, name="restart-engine").start()
            return RestartOut(scheduled=True, target=_restart_target)

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
                    pm.stop_all()  # stop engine + shell, with wait
                except Exception:
                    pass
                # Double-check: wait for all engine processes to be reaped
                for _eng in getattr(pm, 'engines', []):
                    try:
                        _eng.popen.wait(timeout=5)
                    except Exception:
                        pass
            import traceback as _tb
            _msg = f'[DIAG] os._exit(75) called from api.py! stack:\n{"" .join(_tb.format_stack())}'
            print(_msg, flush=True)
            import sys as _sys; _sys.stdout.flush(); _sys.stderr.flush()
            import time as _t; _t.sleep(0.5)  # 确保日志写出
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
        # [Fork/Merge 2026-05-17] Why: newer engine workers include the parent
        # route session when an async dispatch is requested from a branch. How:
        # read it as a fallback for branch index recovery. Purpose: async children
        # are anchored to the durable conversation even if branch indexes are stale.
        parent_session_id = str(body.get("parent_session_id") or "").strip()
        # [2026-04-22] 读取父节点传来的附件列表，透传到 input_data 供 runner.py 消费
        attachments = body.get("attachments")

        if not session_id or not node_id:
            raise HTTPException(status_code=400, detail="session_id and node_id required")
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        input_data: dict[str, Any] = {
            "instruction": instruction,
            "_async_dispatch": True,
            "_caller_node_id": caller_node_id,
        }
        # [2026-04-22] 将附件列表注入 input_data，runner.py L594 已支持读取 input_data["attachments"]
        if attachments and isinstance(attachments, list):
            input_data["attachments"] = attachments
        if context_key:
            input_data["_context_key"] = context_key

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                src_seq = int(source_inbound_seq)
            except (ValueError, TypeError):
                pass

        with st._lock:
            # [2026-05-14] 异步子任务应挂到 parent session 而非 caller 的 branch。
            # 问题：caller 跑在 entry branch 上，caller finish 后 branch 被清理，
            # 导致异步子任务被连带 cancelled。
            # 修复：如果 session_id 是 entry branch，追溯到 parent session。
            # 子任务完成后 _inject_async_dispatch_result_locked 会往 parent 注入
            # inbound 并 fork 新 branch 处理结果，路径不受影响。
            st._ensure_entry_branch_indexes_locked()
            _task_session_id = session_id
            _task_generation = session_generation
            _parent_of_branch = st.entry_branch_parents.get(session_id)
            if not _parent_of_branch and parent_session_id:
                # [Fork/Merge 2026-05-17] Why: a restarted supervisor may have to
                # infer branch ancestry from the request payload or sessions.json.
                # How: trust parent_session_id only when the supplied session is an
                # entry branch for that parent. Purpose: avoid misrouting ordinary
                # child sessions while recovering branch async dispatch routing.
                if st._is_entry_branch_session_locked(session_id, parent_session_id=parent_session_id):
                    _parent_of_branch = parent_session_id
            if _parent_of_branch:
                _task_session_id = _parent_of_branch
                _task_generation = st._current_session_generation_locked(_task_session_id) or 1

            # Child Session 隔离（Phase B）：async dispatch 也走 child session
            _child_sid, _is_new = st.get_or_create_child_session(
                _task_session_id, node_id, context_key or "", context_mode,
            )
            input_data["child_session_id"] = _child_sid
            input_data["context_mode"] = context_mode
            input_data["use_context"] = False
            if context_mode == "fork":
                input_data["fork_from_session_id"] = _task_session_id
            # 审计报告 Step 1（2026-04-16）：删除 async dispatch 的 accumulate fallback。
            # engine/runner.py:514 在 child_session_id 非空时会无条件清空 context_ref，
            # 此 fallback 注入永远不会被消费，属于兼容期死代码。

            task = st._create_task_locked(
                session_id=_task_session_id,
                session_generation=_task_generation,
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

    # 2026-05-14: admin assets moved from public/admin to platform/admin as
    # part of the platform/ consolidation. The static route now serves the
    # moved directory so the web console keeps working after public/ removal.
    admin_dir = state.workspace_root / "platform" / "admin"
    admin_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")

    # [2026-05-16] Web chat frontend
    web_dist = state.workspace_root / "platform" / "web" / "frontend" / "dist"
    if web_dist.is_dir():
        app.mount("/web", StaticFiles(directory=str(web_dist), html=True), name="web")
        print(f"[web] 前端地址: http://{{host}}:{{port}}/web/", flush=True)

    # 启动时打印 token 并写入共享文件供 engine 读取
    token = get_admin_token()
    _token_file = state.workspace_root / "data" / ".admin_token"
    _token_file.parent.mkdir(parents=True, exist_ok=True)
    _token_file.write_text(token, encoding="utf-8")
    print(f"[admin] 管理界面地址: http://{{host}}:{{port}}/admin/", flush=True)
    print(f"[admin] 管理 Token: {token}", flush=True)
    print(f"[admin] Token 已写入: {_token_file}", flush=True)

    return app
