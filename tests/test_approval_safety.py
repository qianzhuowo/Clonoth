"""Regression coverage for explicit QQ approval intent and terminal decisions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from _onebot_runtime import load_runtime as _load_runtime
from clonoth_sdk.client import ClonothClient


APPROVAL_ID = "924b900b-4e92-4c3e-9af4-ecc058b615b2"


def _state(tmp_path):
    """Supervisor state with the default policy engine (was test_approval_tool_identity._state)."""
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState

    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="test-run"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


_SUPERVISOR_TERMINAL_GAP = (
    "supervisor-side (not adapter): NEW SupervisorState.decide_approval "
    "(supervisor/state.py) has no require_pending guard and POST /v1/approvals/{id} "
    "(supervisor/api.py approval_decide) returns 200 with the already-decided approval "
    "instead of 409"
)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    return _load_runtime(monkeypatch, tmp_path)


@pytest.mark.parametrize("text", [
    "red eyes", "hair between eyes", "token", "通过测试了吗", "允许你起个名字",
    "？怎么同意审批，这是什么审批？我名字呢", "同意吗", "不允许", "不同意这件事",
    "yes please", "ok?", "审批同意以后再说", "y e s", "",
])
def test_chat_is_not_approval(runtime, text):
    assert runtime.approval.parse_approval_reply_verb(text) is None
    assert runtime.approval.parse_approval_command(text) is None


@pytest.mark.parametrize(("text", "decision"), [
    ("同意", "allow"), ("审批同意", "allow"), ("审批 同意", "allow"),
    (" YES ", "allow"), ("approval approve", "allow"), ("ok", "allow"),
    ("拒绝", "deny"), ("不同意", "deny"), ("approval NO", "deny"),
])
def test_exact_reply_commands(runtime, text, decision):
    assert runtime.approval.parse_approval_reply_verb(text) == decision


@pytest.mark.parametrize("text", [
    f"审批 同意 {APPROVAL_ID}", f"approve {APPROVAL_ID}",
    f"APPROVAL YES {APPROVAL_ID.upper()}",
])
def test_explicit_id_commands(runtime, text):
    assert runtime.approval.parse_approval_command(text) == ("allow", APPROVAL_ID)


@pytest.mark.parametrize("text", [
    f"审批 同意 {APPROVAL_ID} 吗", f"同意 {APPROVAL_ID} 但先别执行",
    "审批 同意 9", "同意 起这个名字", f"eyes {APPROVAL_ID}",
])
def test_command_requires_complete_syntax(runtime, text):
    assert runtime.approval.parse_approval_command(text) is None


@pytest.mark.parametrize("reply_id", [None, 0, "0", ""])
def test_no_reply_never_selects_unique_pending(runtime, monkeypatch, reply_id):
    runtime.state.BOOK.pending[APPROVAL_ID] = {}
    get_reply = AsyncMock()
    monkeypatch.setattr(runtime.message_parse, "get_reply_message", get_reply)
    result = asyncio.run(runtime.callbacks._resolve_approval_id_by_reply_fallback(
        SimpleNamespace(self_id="123"), object(), reply_id,
    ))
    assert result is None
    get_reply.assert_not_awaited()


@pytest.mark.parametrize(("sender_id", "text", "expected"), [
    ("123", f"ID: {APPROVAL_ID}", APPROVAL_ID),
    ("456", f"ID: {APPROVAL_ID}", None),
    ("123", "普通聊天", None),
    (None, f"ID: {APPROVAL_ID}", None),
])
def test_fallback_requires_bot_authored_matching_reply(runtime, monkeypatch, sender_id, text, expected):
    runtime.state.BOOK.pending[APPROVAL_ID] = {}
    monkeypatch.setattr(runtime.message_parse, "get_reply_message", AsyncMock(return_value={
        "sender": {"user_id": sender_id}, "raw_message": text,
    }))
    assert asyncio.run(runtime.callbacks._resolve_approval_id_by_reply_fallback(
        SimpleNamespace(self_id="123"), object(), 42,
    )) == expected


def test_decided_event_cleans_pending_and_reply_mapping(runtime):
    runtime.state.BOOK.pending.update({APPROVAL_ID: {"operation": "execute_command"}, "other": {}})
    runtime.state.BOOK.remember_message(42, APPROVAL_ID)
    runtime.state.BOOK.remember_message(43, "other")
    asyncio.run(runtime.callbacks._handle_approval_raw_event(SimpleNamespace(
        type="approval_decided", payload={"approval_id": APPROVAL_ID, "decision": "deny"},
    )))
    assert APPROVAL_ID not in runtime.state.BOOK.pending
    assert runtime.state.BOOK.resolve_by_reply(42) is None
    assert runtime.state.BOOK.resolve_by_reply(43) == "other"


class _Finished(Exception):
    pass


@pytest.mark.parametrize(("text", "reply_id", "expected"), [
    ("red eyes", None, False), ("同意", None, False),
    ("同意", 99, False), ("red eyes", 42, False), ("同意", 42, True),
    (f"审批 同意 {APPROVAL_ID}", None, True),
])
def test_private_handler_only_submits_explicit_approval(runtime, monkeypatch, text, reply_id, expected):
    runtime.state.BOOK.pending[APPROVAL_ID] = {}
    runtime.state.BOOK.remember_message(42, APPROVAL_ID)
    monkeypatch.setattr(runtime.state, "_client", object())
    monkeypatch.setattr(runtime.state, "_session_state", object())
    monkeypatch.setattr(runtime.state.REPLY_MSGS, "remember", lambda e: None)
    monkeypatch.setattr(
        runtime.state, "forward_expander",
        lambda bot: SimpleNamespace(to_text=AsyncMock(return_value=text)),
    )
    monkeypatch.setattr(runtime.message_parse, "extract_reply_message_id", lambda *a: reply_id)
    monkeypatch.setattr(runtime.access, "is_admin_user", lambda u: True)
    monkeypatch.setattr(runtime.message_parse, "get_reply_message", AsyncMock(return_value=None))
    decision = AsyncMock(side_effect=_Finished)
    monkeypatch.setattr(runtime.pkg, "_finish_approval_decision", decision)
    # Stop as soon as the message reaches ordinary chat/command handling.
    monkeypatch.setattr(runtime.commands, "_maybe_handle_clear_group_memory_command", AsyncMock(side_effect=_Finished))
    event = SimpleNamespace(user_id=1, get_message=lambda: [], raw_message=text)
    with pytest.raises(_Finished):
        asyncio.run(runtime.pkg._handle_private_agent(SimpleNamespace(self_id="123"), event))
    assert decision.await_count == int(expected)
    if expected:
        decision.assert_awaited_once_with(1, APPROVAL_ID, "allow")


@pytest.mark.parametrize(("status", "body", "expected"), [
    (200, {"approval_id": APPROVAL_ID, "status": "allowed", "decision": "allow"}, True),
    (200, {"approval_id": APPROVAL_ID, "status": "denied", "decision": "deny"}, False),
    (200, {"approval_id": APPROVAL_ID, "status": "pending", "decision": None}, False),
    (200, {"approval_id": "other", "status": "allowed", "decision": "allow"}, False),
    (200, {}, False), (200, [], False), (409, {"detail": "already decided"}, False),
    (404, {}, False), (500, {}, False),
])
def test_sdk_validates_actual_decision(status, body, expected):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json=body),
        )) as http:
            client = ClonothClient("http://test")
            client._client = http
            assert await client.approve(APPROVAL_ID, decision="allow") is expected
    asyncio.run(run())


def test_sdk_rejects_non_json_success():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="not an approval"),
        )) as http:
            client = ClonothClient("http://test")
            client._client = http
            assert not await client.approve(APPROVAL_ID, decision="allow")
    asyncio.run(run())


@pytest.mark.xfail(reason=_SUPERVISOR_TERMINAL_GAP, strict=True)
@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_expired_approval_cannot_be_resubmitted(tmp_path, decision):
    state = _state(tmp_path)
    approval = state.create_approval(session_id="s", operation="execute_command", details={})
    state.decide_approval(approval_id=approval.approval_id, decision="deny", comment="timed out")
    with pytest.raises(ValueError, match="already decided"):
        state.decide_approval(approval_id=approval.approval_id, decision=decision, require_pending=True)
    assert approval.decision == "deny"
    assert approval.comment == "timed out"
    events = state.eventlog.list_events(session_id="s", after_seq=0)
    assert sum(event["type"] == "approval_decided" for event in events) == 1


def test_failed_decision_never_reports_success(runtime, monkeypatch):
    runtime.state.BOOK.pending[APPROVAL_ID] = {"operation": "execute_command"}
    monkeypatch.setattr(runtime.state, "_client", SimpleNamespace(approve=AsyncMock(return_value=False)))
    finish = AsyncMock(side_effect=_Finished)
    monkeypatch.setattr(runtime.pkg._private_matcher, "finish", finish, raising=False)
    with pytest.raises(_Finished):
        asyncio.run(runtime.pkg._finish_approval_decision(1, APPROVAL_ID, "allow"))
    assert "未生效" in finish.call_args.args[0]
    assert "已同意" not in finish.call_args.args[0]


@pytest.mark.xfail(reason=_SUPERVISOR_TERMINAL_GAP, strict=True)
@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_api_rejects_terminal_approval_and_sdk_reports_failure(tmp_path, monkeypatch, decision):
    import supervisor.admin_api as admin_api
    from supervisor.api import create_app
    from supervisor.config_store import ConfigStore

    # NEW requires the admin token on POST /v1/approvals/{id}; authenticate the
    # test client so the request reaches the decision logic under test.
    monkeypatch.setattr(admin_api, "get_admin_token", lambda: "test-admin-token")
    monkeypatch.setattr(admin_api, "get_web_auth", lambda: None)
    state = _state(tmp_path)
    approval = state.create_approval(session_id="s", operation="execute_command", details={})
    state.decide_approval(approval_id=approval.approval_id, decision="deny", comment="timed out")
    app = create_app(state=state, process_manager=None, config_store=ConfigStore(path=tmp_path / "config.yaml"))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer test-admin-token"},
        ) as http:
            response = await http.post(f"/v1/approvals/{approval.approval_id}", json={"decision": decision})
            assert response.status_code == 409
            client = ClonothClient("http://test")
            client._client = http
            assert not await client.approve(approval.approval_id, decision=decision)
            current = (await http.get(f"/v1/approvals/{approval.approval_id}")).json()
            assert current["status"] == "denied"
            assert current["comment"] == "timed out"
            pending = state.create_approval(session_id="s", operation="execute_command", details={})
            assert await client.approve(pending.approval_id, decision=decision)
            assert pending.decision == decision
    asyncio.run(run())


def test_decision_event_during_submit_does_not_lose_operation(runtime, monkeypatch):
    runtime.state.BOOK.pending[APPROVAL_ID] = {"operation": "execute_command"}

    async def approve(*args, **kwargs):
        await runtime.callbacks._handle_approval_raw_event(SimpleNamespace(
            type="approval_decided", payload={"approval_id": APPROVAL_ID},
        ))
        return True

    monkeypatch.setattr(runtime.state, "_client", SimpleNamespace(approve=approve))
    finish = AsyncMock(side_effect=_Finished)
    monkeypatch.setattr(runtime.pkg._private_matcher, "finish", finish, raising=False)
    with pytest.raises(_Finished):
        asyncio.run(runtime.pkg._finish_approval_decision(1, APPROVAL_ID, "allow"))
    assert "execute_command" in finish.call_args.args[0]
    assert APPROVAL_ID not in runtime.state.BOOK.pending
