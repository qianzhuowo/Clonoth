"""Tests for the admin active-task summary endpoint.

[AutoC 2026-06-04] These tests are written before the implementation because the
System dashboard needs a small task-summary API rather than the full Task payloads.
They pin authentication, field omission, status filtering, and newest-first order.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Why: tests run from a checkout that is not installed as a package. How: prepend
# the repository root. Purpose: exercise the edited supervisor modules directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import Task, TaskKind, TaskStatus  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    """Create a temporary SupervisorState for admin API tests."""
    # Why: the endpoint reads the real in-memory task registry. How: instantiate
    # SupervisorState with temporary persistence. Purpose: avoid touching live data.
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-active-tasks")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _task(
    task_id: str,
    *,
    status: TaskStatus,
    kind: TaskKind = TaskKind.node,
    created_at: datetime,
    updated_at: datetime | None = None,
    worker_id: str | None = None,
    caller_task_id: str | None = None,
) -> Task:
    """Build a Task containing large fields that the summary API must omit."""
    # Why: the dashboard only needs identifying metadata. How: populate input,
    # continuation, and result with sentinel values. Purpose: prove the API does
    # not return heavy or sensitive task payloads to the task-monitor modal.
    return Task(
        task_id=task_id,
        session_id=f"session-{task_id}",
        kind=kind,
        node_id=f"node-{task_id}",
        input={"large_prompt": "x" * 512},
        continuation={"history": ["hidden"]},
        result={"secret": "must-not-leak"},
        status=status,
        worker_id=worker_id,
        caller_task_id=caller_task_id,
        created_at=created_at,
        updated_at=updated_at or created_at,
    )


def test_admin_active_tasks_returns_summaries_sorted_and_requires_auth(tmp_path: Path, monkeypatch) -> None:
    """The active-task endpoint should expose only running, pending, and suspended summaries."""
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(admin_api, "_admin_token", "")

    state = _make_state(tmp_path)
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
    )
    now = datetime(2026, 6, 4, 2, 58, tzinfo=timezone.utc)
    with state._lock:
        state.tasks = {
            "running-old": _task(
                "running-old",
                status=TaskStatus.running,
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(seconds=5),
                worker_id="worker-running-123456",
            ),
            "pending-new": _task(
                "pending-new",
                status=TaskStatus.pending,
                created_at=now - timedelta(minutes=1),
                caller_task_id="caller-1",
            ),
            "suspended-mid": _task(
                "suspended-mid",
                status=TaskStatus.suspended,
                kind=TaskKind.tool,
                created_at=now - timedelta(minutes=3),
            ),
            "completed-hidden": _task(
                "completed-hidden",
                status=TaskStatus.completed,
                created_at=now,
            ),
        }

    async def _exercise_api() -> tuple[httpx.Response, httpx.Response]:
        # Why: Starlette TestClient is not used in this project. How: drive the
        # FastAPI app through httpx ASGITransport. Purpose: test real routing,
        # authentication, and serialization without opening a network port.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauth = await client.get("/v1/admin/tasks/active")
            authed = await client.get(
                "/v1/admin/tasks/active",
                headers={"Authorization": "Bearer test-admin-token"},
            )
            return unauth, authed

    unauth, authed = asyncio.run(_exercise_api())

    assert unauth.status_code == 401
    assert authed.status_code == 200
    payload = authed.json()
    assert [item["task_id"] for item in payload] == ["pending-new", "suspended-mid", "running-old"]
    assert {item["status"] for item in payload} == {"pending", "suspended", "running"}
    assert payload[1]["kind"] == "tool"
    assert payload[2]["worker_id"] == "worker-running-123456"
    assert payload[0]["caller_task_id"] == "caller-1"
    for item in payload:
        assert set(item) == {
            "task_id",
            "session_id",
            "node_id",
            "status",
            "kind",
            "created_at",
            "updated_at",
            "worker_id",
            "caller_task_id",
        }
        assert "must-not-leak" not in str(item)
