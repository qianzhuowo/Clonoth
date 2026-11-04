"""qq_forward target_type=self in group chats resolves the per-task sender."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("nonebot")

# The package __init__ is the NoneBot plugin; register a bare package so submodules
# import without running it (same pattern as the other onebot tests).
_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

state = importlib.import_module("adapters.onebot.state")
forward_bridge = importlib.import_module("adapters.onebot.forward_bridge")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(state, "_task_senders", type(state._task_senders)())
    monkeypatch.setattr(state, "_session_targets", {"sess-g": {"type": "group", "group_id": 8888889}})
    monkeypatch.setattr(state, "_persisted_session_targets", {})


def _resolve(task_id: str):
    return asyncio.run(forward_bridge._forward_bridge_resolve_target(
        None, "sess-g", "self", "", parent_session_id="sess-g", runtime_session_id="branch_1", task_id=task_id,
    ))


def test_group_self_without_task_sender_fails_as_before():
    target, err = _resolve("")
    assert target is None and "无法确定当前用户" in err


def test_group_self_uses_task_sender():
    state.remember_task_sender("task-a", 7654321)
    target, err = _resolve("task-a")
    assert err == "" and target.target_type == "private" and target.target_id == 7654321


def test_concurrent_senders_in_same_group_do_not_overwrite():
    state.remember_task_sender("task-a", 7654321)
    state.remember_task_sender("task-b", 7654322)
    assert _resolve("task-a")[0].target_id == 7654321
    assert _resolve("task-b")[0].target_id == 7654322


def test_task_sender_map_is_bounded(monkeypatch):
    monkeypatch.setattr(state, "_TASK_SENDERS_MAX", 3)
    for i in range(5):
        state.remember_task_sender(f"t{i}", 1000 + i)
    assert state.task_sender("t0") is None and state.task_sender("t4") == 1004
    assert len(state._task_senders) == 3


def test_invalid_sender_ignored():
    state.remember_task_sender("t", "not-a-number")
    state.remember_task_sender("", 123)
    assert len(state._task_senders) == 0
