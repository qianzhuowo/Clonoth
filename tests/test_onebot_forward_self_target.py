"""qq_forward target_type=self in group chats resolves the triggering sender.

Chain: task_context.platform_auth.user_id -> ToolContext.platform_user_id ->
CLONOTH_PLATFORM_USER_ID (script tool env) -> qq_forward payload origin_user_id
-> forward bridge self target.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.context import platform_user_id_from_task_context
from toolbox.registry import _script_tool_env

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
def _group_session(monkeypatch):
    monkeypatch.setattr(state, "_session_targets", {"sess-g": {"type": "group", "group_id": 8888889}})
    monkeypatch.setattr(state, "_persisted_session_targets", {})


def _resolve(origin_user_id: str):
    return asyncio.run(forward_bridge._forward_bridge_resolve_target(
        None, "sess-g", "self", "",
        parent_session_id="sess-g", runtime_session_id="branch_1", origin_user_id=origin_user_id,
    ))


def test_task_context_sender_extraction():
    assert platform_user_id_from_task_context({"platform_auth": {"user_id": "7654321"}}) == "7654321"
    assert platform_user_id_from_task_context({"platform_auth": {}}) == ""
    assert platform_user_id_from_task_context({}) == ""
    assert platform_user_id_from_task_context(None) == ""


def test_script_env_carries_platform_user(monkeypatch):
    monkeypatch.setenv("CLONOTH_PLATFORM_USER_ID", "stale")
    env = _script_tool_env(SimpleNamespace(platform_user_id="7654321"))
    assert env["CLONOTH_PLATFORM_USER_ID"] == "7654321"
    # A missing value never leaks the engine process's stale identity.
    assert "CLONOTH_PLATFORM_USER_ID" not in _script_tool_env(SimpleNamespace(platform_user_id=""))


def test_group_self_uses_origin_user():
    target, err = _resolve("7654321")
    assert err == "" and target.target_type == "private" and target.target_id == 7654321


def test_group_self_without_origin_user_fails():
    target, err = _resolve("")
    assert target is None and "无法确定当前用户" in err
    target, err = _resolve("not-a-number")
    assert target is None


def test_qq_forward_payload_sends_origin_user(monkeypatch):
    qq_forward = importlib.import_module("tools.qq_forward")
    src = Path(qq_forward.__file__).read_text(encoding="utf-8")
    assert '"origin_user_id": _env_first("CLONOTH_PLATFORM_USER_ID"' in src
