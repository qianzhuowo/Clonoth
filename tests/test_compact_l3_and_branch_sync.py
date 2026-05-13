from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


_TASK_SUMMARY_MARKER = "[Task summary — original messages snipped]"
_AGGREGATED_MARKER = "[Aggregated task summaries — condensed]"


def test_compress_summaries_merges_oldest_summaries_and_inherits_ids() -> None:
    """L3 should merge the oldest snip summaries and retain their task ids."""
    # Why: L2 snip summaries can themselves fill the context over a long session.
    # How: build more than the default allowed summary count, then merge the
    # oldest five into one aggregate block. Purpose: protect the new L3 behavior
    # before wiring it into the automatic compact path.
    from engine.task_record import compress_summaries

    messages: list[dict] = [{"role": "user", "content": "prefix"}]
    for i in range(11):
        messages.append({
            "role": "user",
            "content": f"{_TASK_SUMMARY_MARKER}\nsummary {i}",
            "_meta": {
                "source_task_id": f"task_{i}",
                "compressed_task_ids": [f"legacy_{i}"],
            },
        })

    compacted, merged_count = compress_summaries(messages, [], max_summary_count=10, merge_count=5)

    assert merged_count == 5
    assert len(compacted) == len(messages) - 4
    aggregate = compacted[1]
    assert aggregate["content"].startswith(_AGGREGATED_MARKER)
    assert "summary 0" in aggregate["content"]
    assert "summary 4" in aggregate["content"]
    assert "summary 5" not in aggregate["content"]
    assert aggregate["_meta"]["source_task_id"] == "aggregated_task_summaries"
    assert set(aggregate["_meta"]["compressed_task_ids"]) >= {
        "task_0",
        "task_1",
        "task_2",
        "task_3",
        "task_4",
        "legacy_0",
        "legacy_4",
    }
    assert compacted[2]["content"].startswith(_TASK_SUMMARY_MARKER)
    assert "summary 5" in compacted[2]["content"]


def test_snip_history_treats_l3_aggregate_ids_as_already_compressed() -> None:
    """L2 should not snip task ids already covered by an L3 aggregate summary."""
    # Why: after L3 replaces individual snip messages, their source_task_id markers
    # disappear from the visible message list. How: keep the inherited
    # compressed_task_ids metadata on the aggregate and verify L2 honors it.
    # Purpose: prevent repeated snipping attempts for task chains already covered
    # by an aggregate summary.
    from engine.task_record import TaskRecord, snip_history

    records = [
        TaskRecord(task_id=f"task_{i}", session_id="parent", node_id="node", summary=f"summary {i}")
        for i in range(5)
    ]
    messages = [
        {
            "role": "user",
            "content": f"{_AGGREGATED_MARKER}\nsummary 0",
            "_meta": {"compressed_task_ids": ["task_0"]},
        },
        {"role": "assistant", "content": "leftover", "_meta": {"source_task_id": "task_0"}},
    ]

    compacted, snip_count, snipped_ids = snip_history(
        messages,
        records,
        keep_recent_tasks=1,
        max_snip=5,
    )

    assert compacted == messages
    assert snip_count == 0
    assert snipped_ids == set()


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
