"""Regression tests for scheduled Dream lock updates."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# [AutoC 2026-06-01] Why: this standalone regression test is outside tests/
# because this repository ignores most local test files under that directory.
# How: add the workspace root to sys.path before importing the built-in handler.
# Purpose: keep the Dream lock behavior covered by a commit-tracked test file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.builtin.dream import DreamHandler  # noqa: E402


def test_dream_completed_task_writes_lock(tmp_path: Path) -> None:
    """A completed final Dream task should update data/memory/.dream-lock."""
    handler = DreamHandler()
    completed_at = datetime(2026, 6, 1, 2, 53, tzinfo=timezone.utc)
    handler._dream_pending = {
        "now_key": "2026-06-01 02:52",
        "extractor_task_ids": [],
        "topology_json": "{}",
        "session_ids": [],
        "dream_task_id": "dream-task-1",
        "dream_created_at": "2026-06-01T02:52:00+00:00",
    }

    # [AutoC 2026-06-01] Why: the production scheduler polls task_snapshots on
    # every tick after the final Dream task is created. How: provide the minimal
    # callback response for a completed task. Purpose: verify the supervisor hook,
    # not the Dream node, persists the lock after success.
    handler.on_tick(
        {
            "schedule_type": "dream",
            "workspace_root": tmp_path,
            "now": completed_at,
            "now_key": completed_at.strftime("%Y-%m-%d %H:%M"),
            "task_snapshots": lambda task_ids: {
                "dream-task-1": {"status": "completed", "result_text": "done", "error": ""}
            },
        }
    )

    lock_path = tmp_path / "data" / "memory" / ".dream-lock"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload == {
        "last_run": completed_at.astimezone().isoformat(timespec="seconds"),
        "note": "Dream cycle completed via automated pipeline",
    }
    assert handler._dream_pending is None
