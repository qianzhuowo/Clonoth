"""Tests for attaching approval events to tool executions."""
from __future__ import annotations

import sys
from pathlib import Path


# Why: these tests run from a source checkout rather than an installed package.
# How: prepend the repository root to sys.path. Purpose: import the edited
# supervisor modules directly and validate the event payload contract.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor._helpers import SessionInfo, _now  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel, Task, TaskKind, TaskStatus  # noqa: E402


def _state(tmp_path: Path) -> SupervisorState:
    """Build a supervisor state with the default policy engine."""
    # Why: execute_command requires approval by default. How: use the real default
    # PolicyEngine with a temporary workspace and EventLog. Purpose: exercise the
    # same create_approval path used by /v1/ops/request.
    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="test-run"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


def _attach_task_to_session(
    state: SupervisorState,
    *,
    session_id: str,
    conversation_key: str,
    task_id: str,
) -> None:
    """Register a minimal task/session pair for policy routing tests."""
    # Why: scheduler approval bypass is keyed by the task's owning session. How:
    # store a real SessionInfo and Task in the same in-memory maps that production
    # request_operation reads. Purpose: verify the routing decision without
    # involving the scheduler loop or engine workers.
    now = _now()
    state.sessions[session_id] = SessionInfo(
        session_id=session_id,
        channel="test",
        conversation_key=conversation_key,
        created_at=now,
        updated_at=now,
    )
    state.tasks[task_id] = Task(
        task_id=task_id,
        session_id=session_id,
        session_generation=1,
        kind=TaskKind.node,
        node_id="system.dream",
        input={},
        continuation={},
        source_inbound_seq=None,
        status=TaskStatus.running,
        created_at=now,
        updated_at=now,
    )


def test_request_operation_emits_approval_tool_identity(tmp_path: Path) -> None:
    state = _state(tmp_path)

    out = state.request_operation(
        session_id="session-1",
        op="execute_command",
        parameters={"command": "echo hi"},
        tool_call_id="call-1",
        node_id="node-1",
        task_id="task-1",
    )

    assert out.approval_id
    approval = state.approvals[out.approval_id]
    assert approval.tool_call_id == "call-1"
    assert approval.node_id == "node-1"
    assert approval.task_id == "task-1"

    events = state.eventlog.list_events(session_id="session-1", after_seq=0)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["tool_call_id"] == "call-1"
    assert payload["node_id"] == "node-1"
    assert payload["task_id"] == "task-1"


def test_decide_approval_emits_tool_identity(tmp_path: Path) -> None:
    state = _state(tmp_path)
    out = state.request_operation(
        session_id="session-1",
        op="execute_command",
        parameters={"command": "echo hi"},
        tool_call_id="call-1",
        node_id="node-1",
        task_id="task-1",
    )

    state.decide_approval(approval_id=out.approval_id or "", decision="allow", comment="ok")

    events = state.eventlog.list_events(session_id="session-1", after_seq=0)
    decided = [event for event in events if event["type"] == "approval_decided"]
    assert len(decided) == 1
    payload = decided[0]["payload"]
    assert payload["approval_id"] == out.approval_id
    assert payload["decision"] == "allow"
    assert payload["tool_call_id"] == "call-1"
    assert payload["node_id"] == "node-1"
    assert payload["task_id"] == "task-1"


def test_scheduler_task_execute_command_auto_approves_without_event(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_task_to_session(
        state,
        session_id="session-scheduler",
        conversation_key="scheduler:dream",
        task_id="task-scheduler",
    )

    out = state.request_operation(
        session_id="session-scheduler",
        op="execute_command",
        parameters={"command": "echo hi"},
        tool_call_id="call-scheduler",
        node_id="system.dream",
        task_id="task-scheduler",
    )

    # Why: scheduler-triggered tasks have no Discord approval route. How: the
    # supervisor returns an auto decision instead of creating an approval event.
    # Purpose: scheduled maintenance jobs do not block forever on silent approvals.
    assert out.safety_level == SafetyLevel.auto
    assert out.reason == "auto-approved: scheduler task"
    assert out.approval_id is None
    assert state.approvals == {}
    assert state.eventlog.list_events(session_id="session-scheduler", after_seq=0) == []


def test_scheduler_task_does_not_bypass_denied_command(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_task_to_session(
        state,
        session_id="session-scheduler",
        conversation_key="scheduler:dream",
        task_id="task-scheduler",
    )

    out = state.request_operation(
        session_id="session-scheduler",
        op="execute_command",
        parameters={"command": "rm -rf /"},
        tool_call_id="call-scheduler",
        node_id="system.dream",
        task_id="task-scheduler",
    )

    # Why: the scheduler bypass must only replace human approval, not hard policy
    # denial. How: denied commands return SafetyLevel.deny before scheduler
    # auto-approval is considered. Purpose: destructive commands remain blocked.
    assert out.safety_level == SafetyLevel.deny
    assert out.approval_id is None
    assert state.approvals == {}
