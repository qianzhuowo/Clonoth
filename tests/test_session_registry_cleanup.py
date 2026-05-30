"""Regression tests for physical cleanup of transient session registry entries.

[AutoC 2026-05-30] These tests are written before the implementation because the
reported production failure is persistent-file growth: branch and fresh/fork
child sessions were only marked reset and remained in data/sessions.json.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

# Why: the test checkout is executed directly, not installed as a package.
# How: prepend the repository root to sys.path. Purpose: import the edited
# supervisor modules and verify the real persistence code path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor._helpers import _now  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402


def _make_state(workspace: Path) -> SupervisorState:
    """Create a SupervisorState with only temporary persistence files."""
    # [AutoC 2026-05-30] Why: cleanup behavior must be checked through the same
    # SessionStore path used in production. How: build a full SupervisorState on a
    # pytest tmp_path. Purpose: avoid touching live sessions.json while covering
    # registry, in-memory indexes, and JSONL cleanup together.
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-session-cleanup")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _registry(workspace: Path) -> dict[str, dict]:
    """Read the temporary sessions.json registry."""
    # [AutoC 2026-05-30] Why: the bug is physical records left in sessions.json.
    # How: inspect the file on disk instead of only SupervisorState memory.
    # Purpose: assertions fail if cleanup merely marks reset=true.
    path = workspace / "data" / "sessions.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_session_store_remove_session_physically_deletes_registry_entry(tmp_path: Path) -> None:
    """remove_session should pop the entry rather than mark it reset."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:remove-session")
    child_sid, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")

    state._session_store.remove_session(child_sid)

    # [AutoC 2026-05-30] Why: branch and child cleanup must shrink sessions.json.
    # How: assert the registry key is gone from both memory and disk. Purpose:
    # prevent a regression to reset-marker-only cleanup.
    assert child_sid not in state._session_store._registry
    assert child_sid not in _registry(tmp_path)


def test_cleanup_branch_physically_removes_branch_and_derived_child_entries(tmp_path: Path) -> None:
    """Branch merge cleanup should remove branch and branch-owned child records."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:branch-cleanup")

    with state._lock:
        branch_sid, _ = state._create_entry_branch_locked(parent, inbound_seq=31)
        derived_sid, _ = state.get_or_create_child_session(branch_sid, "child.node", "case", "fresh")
        (tmp_path / "data" / "conversations").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "conversations" / f"{derived_sid}.jsonl").write_text("", encoding="utf-8")
        state._cleanup_branch_locked(branch_sid)

    registry = _registry(tmp_path)
    # [AutoC 2026-05-30] Why: completed branches have no restart value after
    # merge. How: assert both branch and derived child registry rows are absent.
    # Purpose: sessions.json cannot keep growing with branch_* and child_* rows.
    assert branch_sid not in registry
    assert derived_sid not in registry
    assert branch_sid not in state.sessions
    assert derived_sid not in state.sessions


def test_cleanup_stale_sessions_removes_reset_branch_and_old_fresh_fork_but_keeps_accumulate(tmp_path: Path) -> None:
    """The periodic sweep should delete stale transient sessions and keep accumulate children."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_session(channel="test", conversation_key="test:stale-cleanup")
    fresh_sid, _ = state.get_or_create_child_session(parent, "fresh.node", "case", "fresh")
    fork_sid, _ = state.get_or_create_child_session(parent, "fork.node", "case", "fork")
    accumulate_sid, _ = state.get_or_create_child_session(parent, "acc.node", "case", "accumulate")

    old_ts = (_now() - timedelta(hours=25)).isoformat()
    state._session_store._registry[fresh_sid]["last_active_at"] = old_ts
    state._session_store._registry[fork_sid]["last_active_at"] = old_ts
    state._session_store._registry[accumulate_sid]["last_active_at"] = old_ts
    state._session_store._registry["branch_orphan"] = {
        "session_id": "branch_orphan",
        "channel": "internal",
        "conversation_key": "",
        "created_at": old_ts,
        "reset": False,
        "is_child": True,
        "parent_session_id": parent,
        "node_id": "__entry_branch__",
        "context_key": "orphan",
        "context_mode": "branch",
        "last_active_at": old_ts,
    }
    state._session_store._registry["reset_old"] = {
        "session_id": "reset_old",
        "channel": "internal",
        "conversation_key": "",
        "created_at": old_ts,
        "reset": True,
    }
    state.parent_children.setdefault(parent, set()).update({"branch_orphan"})
    state._session_store._flush()

    with state._lock:
        state._cleanup_stale_sessions_locked()

    registry = _registry(tmp_path)
    # [AutoC 2026-05-30] Why: reset=true rows are historical debris and fresh/fork
    # children older than 24 hours are disposable. How: verify each such key is
    # physically absent. Purpose: the background sweep bounds sessions.json size.
    assert "reset_old" not in registry
    assert "branch_orphan" not in registry
    assert fresh_sid not in registry
    assert fork_sid not in registry
    assert accumulate_sid in registry
    assert registry[accumulate_sid]["context_mode"] == "accumulate"
