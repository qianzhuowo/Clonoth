"""Tests for attaching approval events to tool executions."""
from __future__ import annotations

import sys
from pathlib import Path

# Why: these tests run from a source checkout rather than an installed package.
# How: prepend the repository root to sys.path. Purpose: import the edited
# supervisor modules directly and validate the event payload contract.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402


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
