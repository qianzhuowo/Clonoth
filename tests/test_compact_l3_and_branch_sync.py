from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


# [AutoC 2026-05-13] Why: the L3 summary-concatenation tests covered a removed
# path. How: keep this file focused on branch synchronization and the new
# ConversationStore task-segment retention checks. Purpose: prevent obsolete
# expectations from reintroducing pure summary concatenation.

def _message(message_id: str, content: str, *, source_task_id: str = ""):
    # Why: branch-sync tests need real ConversationStore messages. How: create a
    # minimal Message with stable ids and task ids. Purpose: keep assertions about
    # compaction behavior independent from JSONL serialization details.
    from engine.conversation_store import Message

    return Message(
        id=message_id,
        role="user",
        content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_task_id=source_task_id,
    )


class _EventLog:
    def __init__(self) -> None:
        # Why: TaskRouterMixin writes task snapshots while resuming compaction.
        # How: record append calls in memory. Purpose: avoid starting a real
        # supervisor event log for a focused unit test.
        self.events: list[dict] = []

    def append(self, **kwargs):
        self.events.append(kwargs)
        return {"seq": len(self.events)}


from supervisor.task_router import TaskRouterMixin


class _RouterHarness(TaskRouterMixin):
    def __init__(self, workspace_root: Path) -> None:
        # Why: we only need the compact methods from TaskRouterMixin. How: attach
        # the minimal supervisor-like state those methods read. Purpose: exercise
        # the production code without booting the full supervisor.
        self.workspace_root = workspace_root
        self.tasks = {}
        self.eventlog = _EventLog()

    def _task_terminal(self, task) -> bool:
        from supervisor.types import TaskStatus

        return task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}

    def _event_task_snapshot(self, event_type: str, task, *, component: str = "supervisor") -> None:
        self.eventlog.append(
            session_id=task.session_id,
            component=component,
            type_=event_type,
            payload=task.model_dump(mode="json"),
        )


def test_conv_store_compact_keeps_recent_complete_task_segments(tmp_path: Path) -> None:
    """ConversationStore compaction should retain whole recent task segments."""
    # Why: _apply_compact_via_conv_store_locked used to keep raw message count,
    # which could preserve only the tail of a multi-message task. How: compact a
    # store with three task segments and keep the last two segments. Purpose:
    # align this path with engine.compact.apply_compact_summary.
    from engine.conversation_store import ConversationStore

    store = ConversationStore(tmp_path / "data" / "conversations")
    old_a = _message("old-a", "old task first", source_task_id="task_old")
    old_b = _message("old-b", "old task second", source_task_id="task_old")
    recent_a = _message("recent-a", "recent task first", source_task_id="task_recent_a")
    recent_b = _message("recent-b", "recent task second", source_task_id="task_recent_a")
    latest = _message("latest", "latest task", source_task_id="task_recent_b")
    old_a.meta = {"compressed_task_ids": ["legacy_old"]}
    store.replace_all("parent", [old_a, old_b, recent_a, recent_b, latest])

    harness = _RouterHarness(tmp_path)
    cr = harness._apply_compact_via_conv_store_locked(
        "parent",
        "compact summary",
        keep_recent=2,
    )

    reloaded = ConversationStore(tmp_path / "data" / "conversations").load("parent")
    assert cr["before"] == 5
    assert cr["after"] == 4
    assert cr["total_segments"] == 3
    assert cr["kept_segments"] == 2
    assert cr["compressed_segments"] == 1
    assert reloaded[0].content.startswith("[以下是之前对话的结构化摘要")
    assert [m.id for m in reloaded[1:]] == ["recent-a", "recent-b", "latest"]
    assert set(reloaded[0].meta["compressed_task_ids"]) == {"task_old", "legacy_old"}


def test_compact_result_targets_parent_session_and_syncs_active_branch(tmp_path: Path) -> None:
    """LLM compact results should rewrite the parent session and preserve branch tails."""
    # Why: entry tasks now run on forked branch sessions, while compaction must
    # reduce the durable parent history. How: simulate a suspended branch caller
    # and a completed compactor child, then apply the compact result. Purpose:
    # verify parent selection, branch prefix sync, and base_count adjustment.
    from engine.conversation_store import ConversationStore
    from supervisor.types import Task, TaskKind, TaskStatus
    from supervisor._helpers import _now

    store = ConversationStore(tmp_path / "data" / "conversations")
    parent_messages = [_message(f"p{i}", f"parent {i}", source_task_id=f"task_{i}") for i in range(8)]
    branch_tail = _message("branch-tail", "branch local tail", source_task_id="branch-task")
    store.replace_all("parent", list(parent_messages))
    store.replace_all("branch_1", list(parent_messages) + [branch_tail])

    harness = _RouterHarness(tmp_path)
    now = _now()
    caller = Task(
        task_id="caller",
        session_id="branch_1",
        session_generation=1,
        kind=TaskKind.node,
        node_id="entry",
        input={
            "parent_session_id": "parent",
            "branch_session_id": "branch_1",
            "base_count": 8,
            "_compact_dispatch_pending": True,
            "_compact_keep_recent": 2,
        },
        status=TaskStatus.suspended,
        created_at=now,
        updated_at=now,
        result={},
    )
    compactor = Task(
        task_id="compactor",
        session_id="parent",
        session_generation=1,
        kind=TaskKind.node,
        node_id="system.compactor",
        input={"_system_task": True},
        caller_task_id="caller",
        status=TaskStatus.completed,
        created_at=now,
        updated_at=now,
        result={
            "action": "finish",
            "result": {"text": "<summary>" + ("compact summary " * 20) + "</summary>"},
        },
    )
    harness.tasks = {"caller": caller, "compactor": compactor}

    harness._apply_compact_result_locked(compactor)

    # Why: the router creates its own ConversationStore instance and replaces
    # JSONL files underneath this test's original store. How: reload through a new
    # store instance. Purpose: assertions observe persisted compact results rather
    # than the pre-test cache.
    reloaded_store = ConversationStore(tmp_path / "data" / "conversations")
    parent_after = reloaded_store.load("parent")
    branch_after = reloaded_store.load("branch_1")
    assert len(parent_after) == 3
    assert parent_after[0].content.startswith("[以下是之前对话的结构化摘要")
    assert [m.id for m in parent_after[1:]] == ["p6", "p7"]
    assert len(branch_after) == 4
    assert branch_after[0].content.startswith("[以下是之前对话的结构化摘要")
    assert [m.id for m in branch_after[1:3]] == ["p6", "p7"]
    assert branch_after[3].id == "branch-tail"
    assert caller.input["base_count"] == 3
    assert caller.status == TaskStatus.pending
