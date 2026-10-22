"""Tests for QQ admin/non-admin command authorization in request_operation.

覆盖 supervisor.state.request_operation 中与 QQ 平台身份相关的判定：
- 非管理员群友：execute_command 默认 deny；但安全的 curl 纯读取直接 auto 放行。
- 管理员：普通/非敏感命令免审批(auto)；敏感命令(下载/装包等)仍需审批。
- deny 硬拦对任何人都生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor._helpers import SessionInfo, _now  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel, Task, TaskKind, TaskStatus  # noqa: E402


def _state(tmp_path: Path) -> SupervisorState:
    (tmp_path / "data").mkdir(exist_ok=True)
    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="test-run"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


def _attach_qq_task(
    state: SupervisorState,
    *,
    session_id: str = "session-qq",
    conversation_key: str = "qq_private:10001",
    task_id: str = "task-qq",
) -> None:
    """注册一个 QQ 会话+任务，使 request_operation 识别为 QQ 平台。"""
    now = _now()
    state.sessions[session_id] = SessionInfo(
        session_id=session_id,
        channel="qq",
        conversation_key=conversation_key,
        created_at=now,
        updated_at=now,
    )
    state.tasks[task_id] = Task(
        task_id=task_id,
        session_id=session_id,
        session_generation=1,
        kind=TaskKind.node,
        node_id="qq.agent",
        input={},
        continuation={},
        source_inbound_seq=1,
        status=TaskStatus.running,
        created_at=now,
        updated_at=now,
    )


def _request(state: SupervisorState, command: str, *, is_admin: bool):
    return state.request_operation(
        session_id="session-qq",
        op="execute_command",
        parameters={"command": command, "platform_auth": {"platform": "qq", "is_admin": is_admin}},
        tool_call_id="call-qq",
        node_id="qq.agent",
        task_id="task-qq",
    )


# --------------------------------------------------------------------------- #
# 非管理员群友
# --------------------------------------------------------------------------- #
def test_non_admin_safe_curl_read_is_auto_allowed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_qq_task(state)

    out = _request(state, "curl https://example.com", is_admin=False)

    assert out.safety_level == SafetyLevel.auto
    assert out.approval_id is None
    # 直接放行，不产生审批事件。
    assert state.approvals == {}


@pytest.mark.parametrize(
    "command",
    [
        "curl http://127.0.0.1/admin",   # SSRF：本地
        "curl -O https://example.com/f",  # 下载落盘
        "ls -la",                          # 普通非 curl 命令
        "pip install requests",           # 装包
    ],
)
def test_non_admin_non_safe_command_is_denied(tmp_path: Path, command: str) -> None:
    state = _state(tmp_path)
    _attach_qq_task(state)

    out = _request(state, command, is_admin=False)

    assert out.safety_level == SafetyLevel.deny
    assert out.approval_id is None
    assert state.approvals == {}


# --------------------------------------------------------------------------- #
# 管理员
# --------------------------------------------------------------------------- #
def test_admin_normal_command_is_auto_approved(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_qq_task(state)

    out = _request(state, "curl https://example.com", is_admin=True)

    assert out.safety_level == SafetyLevel.auto
    assert out.approval_id is None
    assert state.approvals == {}


@pytest.mark.parametrize(
    "command",
    [
        "curl -O https://example.com/tool.bin",
        "pip install requests",
        "chmod +x ./tool",
    ],
)
def test_admin_sensitive_command_still_requires_approval(tmp_path: Path, command: str) -> None:
    state = _state(tmp_path)
    _attach_qq_task(state)

    out = _request(state, command, is_admin=True)

    assert out.safety_level == SafetyLevel.approval_required
    assert out.approval_id is not None
    # 敏感命令必须落到审批事件里，由管理员亲自决定。
    assert out.approval_id in state.approvals


def test_admin_dangerous_command_is_hard_denied(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _attach_qq_task(state)

    out = _request(state, "rm -rf /", is_admin=True)

    assert out.safety_level == SafetyLevel.deny
    assert out.approval_id is None
    assert state.approvals == {}
