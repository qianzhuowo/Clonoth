"""QQ /清空上下文 command: matching, permission, reset + local cleanup."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from _onebot_runtime import load_runtime as _load_runtime
from clonoth_sdk.client import ClonothClient

CONV = "qq_group:abc"


class _Group(SimpleNamespace):
    pass


class _Private(SimpleNamespace):
    pass


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    rt = _load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(rt.commands, "GroupMessageEvent", _Group)
    monkeypatch.setattr(rt.access, "is_admin_user", lambda uid: str(uid) == "1")
    return rt


def _group_event(user_id=2, role="member", group_id=100):
    return _Group(user_id=user_id, group_id=group_id, sender=SimpleNamespace(role=role))


def _run(rt, event, text):
    return asyncio.run(rt.commands._maybe_handle_clear_context_command(
        event=event, user_text=text, conversation_key=CONV,
    ))


@pytest.mark.parametrize("text", [
    "/清空上下文", "清空上下文", "／上下文清空", "!重置上下文", " /新对话 ", "/reset", "/CLEAR", "/new",
])
def test_command_aliases_match(runtime, text):
    assert runtime.commands._is_clear_context_command(text)


@pytest.mark.parametrize("text", [
    "", "reset", "clear", "清空上下文吧", "帮我清空上下文然后说你好", "/清空群记忆", "新对话怎么开",
])
def test_ordinary_chat_does_not_match(runtime, text):
    assert not runtime.commands._is_clear_context_command(text)
    assert _run(runtime, _group_event(), text) is None


def test_group_member_is_rejected(runtime, monkeypatch):
    client = SimpleNamespace(reset_conversation=AsyncMock())
    monkeypatch.setattr(runtime.state, "_client", client)
    reply = _run(runtime, _group_event(role="member"), "/清空上下文")
    assert "群主" in reply
    client.reset_conversation.assert_not_awaited()


@pytest.mark.parametrize(("user_id", "role"), [(2, "owner"), (2, "admin"), (1, "member")])
def test_group_reset_clears_session_history_and_queue(runtime, monkeypatch, user_id, role):
    client = SimpleNamespace(reset_conversation=AsyncMock(return_value={"ok": True, "old_session_id": "s1"}))
    monkeypatch.setattr(runtime.state, "_client", client)
    runtime.state._group_history[100].append("[10:34] Bot: 🧠thought ...")
    runtime.state._group_content_records[100].append(object())
    queued = SimpleNamespace(stable_conversation_key=CONV)
    other = SimpleNamespace(stable_conversation_key="qq_group:other")
    runtime.state._qq_queue.extend([queued, other])
    runtime.state._qq_queue_by_key.update({CONV: queued, "qq_group:other": other})

    reply = _run(runtime, _group_event(user_id=user_id, role=role), "/清空上下文")

    assert "已清空本群上下文" in reply
    client.reset_conversation.assert_awaited_once_with(CONV)
    assert 100 not in runtime.state._group_history
    assert 100 not in runtime.state._group_content_records
    assert list(runtime.state._qq_queue) == [other]
    assert CONV not in runtime.state._qq_queue_by_key


def test_private_user_may_reset_own_conversation(runtime, monkeypatch):
    client = SimpleNamespace(reset_conversation=AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(runtime.state, "_client", client)
    reply = _run(runtime, _Private(user_id=5), "/reset")
    assert "当前私聊" in reply
    client.reset_conversation.assert_awaited_once_with(CONV)


def test_reset_failure_is_reported(runtime, monkeypatch):
    client = SimpleNamespace(reset_conversation=AsyncMock(side_effect=RuntimeError("401")))
    monkeypatch.setattr(runtime.state, "_client", client)
    runtime.state._group_history[100].append("line")
    reply = _run(runtime, _group_event(user_id=1), "/清空上下文")
    assert reply.startswith("❌") and "401" in reply
    assert 100 in runtime.state._group_history


def test_client_reset_conversation_treats_404_as_clean():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.read()))
        return httpx.Response(404, json={"detail": "conversation not found"})

    client = ClonothClient("http://sup")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(client.reset_conversation(CONV)) == {"ok": True, "already_clean": True}
    assert seen[0][0] == "/v1/conversations/reset"
    assert b'"conversation_key"' in seen[0][1]
