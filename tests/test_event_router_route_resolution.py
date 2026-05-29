"""Tests for structured dispatch route metadata in EventRouter.

[2026-05-29] Why: dispatch child sessions used to infer their parent channel
from agent:-prefixed conversation_key strings, and approval de-duplication ran
before adapter ownership was known. How: these tests exercise the SDK router
with structured parent_conversation_key metadata, unowned approval events, and
unowned child progress events. Purpose: lock in scheme C step 1 before changing
routing code.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

# Why: the test runner uses the source checkout directly. How: prepend the
# repository root to sys.path. Purpose: import clonoth_sdk without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_sdk.config import BotConfig  # noqa: E402
from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.state import ChildTaskState, SessionState, TriggerInfo  # noqa: E402
from clonoth_sdk.types import Event  # noqa: E402


class _FakeClient:
    """Small client double used by approval tests."""

    def __init__(self) -> None:
        # Why: auto approval is not the target of these tests. How: record calls
        # if a regression reaches approve unexpectedly. Purpose: keep failures
        # focused on route ownership rather than network behavior.
        self.approved: list[tuple[str, str, str]] = []

    async def approve(self, approval_id: str, *, decision: str, comment: str = "") -> bool:
        self.approved.append((approval_id, decision, comment))
        return True


class _FakeCallbacks:
    """Adapter callback double that records only calls relevant to routing."""

    def __init__(self) -> None:
        # Why: route ownership is observable through whether SDK calls adapter
        # callbacks. How: store callback arguments. Purpose: assert unowned events
        # are skipped and owned events use the parent conversation_key.
        self.approvals: list[dict[str, Any]] = []
        self.child_creations: list[dict[str, Any]] = []
        self.child_updates: list[str] = []

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        self.approvals.append({
            "approval_id": approval_id,
            "operation": operation,
            "details": details,
            "conversation_key": conversation_key,
            "session_id": session_id,
        })

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: TriggerInfo | None = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        self.child_creations.append({
            "task_key": task_key,
            "lines": list(state.lines),
            "trigger": trigger,
            "conversation_key": conversation_key,
            "session_id": session_id,
        })

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        self.child_updates.append(task_key)

    async def update_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_reply(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_intermediate_reply(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_to_channel(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def delete_status_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def edit_status_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def finalize_child_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def refresh_typing(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_created(self, *args: Any, **kwargs: Any) -> None:
        return None


def _event(event_type: str, *, session_id: str, payload: dict[str, Any], seq: int = 1) -> Event:
    return Event(
        seq=seq,
        event_id=f"evt-{seq}",
        ts="2026-05-29T00:00:00Z",
        run_id="run-test",
        session_id=session_id,
        component="supervisor",
        type=event_type,
        payload=payload,
    )


def _router(state: SessionState, callbacks: _FakeCallbacks | None = None) -> EventRouter:
    return EventRouter(
        _FakeClient(),
        state,
        callbacks or _FakeCallbacks(),
        BotConfig(
            base_url="http://127.0.0.1:8765",
            entry_node_id="ereuna_main",
            conversation_key_prefix="discord",
            workspace_root=Path(__file__).resolve().parents[1],
            auto_approve_internal=False,
        ),
    )


def test_dispatch_child_session_uses_structured_parent_conversation_key() -> None:
    state = SessionState()
    state.register_session("discord:1491668801836548166", "parent-session")
    state.register_session("agent:coder:agent:scout:discord:wrong", "child-session")
    router = _router(state)

    payload = {
        "task_id": "task-child",
        "session_id": "branch-session",
        "input": {
            "parent_session_id": "child-session",
            "branch_session_id": "branch-session",
            "_dispatch_origin": {
                "parent_session_id": "parent-session",
                "caller_node_id": "scout",
                "parent_conversation_key": "discord:1491668801836548166",
                "context_mode": "accumulate",
            },
            "task_context": {
                "conversation_key": "agent:coder:agent:scout:discord:wrong",
                "route_conversation_key": "discord:1491668801836548166",
                "dispatch_context_mode": "accumulate",
            },
        },
    }

    # Why: old code could keep child-session mapped to the agent:-prefixed key.
    # How: register the task_created payload with structured route metadata.
    # Purpose: approvals and progress from child sessions resolve to the parent channel.
    router._register_dispatch_child_session(_event("task_created", session_id="branch-session", payload=payload), payload)

    assert state.get_conversation_key("child-session") == "discord:1491668801836548166"
    assert state.get_conversation_key("branch-session") == "discord:1491668801836548166"


def test_unowned_approval_is_not_marked_handled() -> None:
    state = SessionState()
    state.session_conv_map["qq-child-session"] = "qq:12345"
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    event = _event(
        "approval_requested",
        session_id="qq-child-session",
        payload={
            "approval_id": "approval-other-adapter",
            "session_id": "qq-child-session",
            "operation": "execute_command",
            "details": {"tool_name": "execute_command"},
        },
    )

    asyncio.run(router._handle_approval_requested(event))

    assert not router._approval.is_handled("approval-other-adapter")
    assert callbacks.approvals == []


def test_unowned_child_progress_is_skipped_before_state_creation() -> None:
    state = SessionState()
    state.session_conv_map["qq-child-session"] = "qq:12345"
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    event = _event(
        "handoff_progress",
        session_id="qq-child-session",
        payload={
            "task_id": "task-qq-child",
            "session_id": "qq-child-session",
            "node_id": "ereuna_slave1",
            "message": "working",
        },
    )

    asyncio.run(router._handle_handoff_progress(event))

    assert state.get_child_state("task-qq-child") is None
    assert callbacks.child_creations == []


def test_owned_child_progress_uses_parent_conversation_key() -> None:
    state = SessionState()
    state.session_conv_map["child-session"] = "discord:1491668801836548166"
    callbacks = _FakeCallbacks()
    router = _router(state, callbacks)
    event = _event(
        "handoff_progress",
        session_id="child-session",
        payload={
            "task_id": "task-owned-child",
            "session_id": "child-session",
            "node_id": "ereuna_slave1",
            "message": "working",
        },
    )

    asyncio.run(router._handle_handoff_progress(event))

    assert callbacks.child_creations[0]["conversation_key"] == "discord:1491668801836548166"
    assert callbacks.child_creations[0]["session_id"] == "child-session"
