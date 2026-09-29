"""One reply must not prepend a second @ to a model-authored mention."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from test_onebot_send_contract import _load_runtime


class _GroupBot:
    self_id = "999"

    def __init__(self):
        self.sent = []

    async def send_group_msg(self, *, group_id, message):
        self.sent.append(message)
        return {"message_id": len(self.sent)}


def _patch_group_helpers(monkeypatch, runtime):
    async def passthrough(bot, group_id, message):
        return message
    monkeypatch.setattr(runtime, "_sanitize_group_at_segments", passthrough)


@pytest.mark.parametrize(("segments", "expected_mentions"), [
    ([{"type": "at", "qq": "123"}, {"type": "text", "content": "你好"}], ["123"]),
    ([{"type": "text", "content": "你好"}], ["123"]),
    ([{"type": "at", "qq": "456"}, {"type": "text", "content": "你好"}], ["123", "456"]),
    ([{"type": "at", "qq": 123}, {"type": "at", "qq": "123"}, {"type": "text", "content": "你好"}], ["123"]),
    ([{"type": "at", "qq": "456"}, {"type": "at", "qq": "123"}, {"type": "text", "content": "你好"}], ["456", "123"]),
    ([{"type": "image", "url": "https://example.test/emoji.png"}, {"type": "at", "qq": "123"}, {"type": "text", "content": "你好"}], ["123"]),
])
def test_group_reply_mentions_sender_once(monkeypatch, tmp_path, segments, expected_mentions):
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_group_helpers(monkeypatch, runtime)
    monkeypatch.setattr(runtime, "process_emojis", AsyncMock(return_value=segments))
    monkeypatch.setattr(runtime.asyncio, "sleep", AsyncMock())
    bot = _GroupBot()

    async def send(bot, target, message, **kwargs):
        return await runtime._send_qq_message_once(bot, target, message)

    monkeypatch.setattr(runtime, "_send_qq_message", send)
    target = {"type": "group", "group_id": 1, "reply_message_id": 42, "reply_sender_id": 123}
    assert asyncio.run(runtime._send_split_text(bot, target, "test"))
    mentions = [str(seg.data["qq"]) for msg in bot.sent for seg in msg if seg.type == "at"]
    assert mentions == expected_mentions
    assert bot.sent[0][0].type == "reply"
    assert sum(seg.type == "reply" for msg in bot.sent for seg in msg) == 1
    assert any(seg.type == "text" and seg.data["text"] == "你好" for msg in bot.sent for seg in msg)


def test_final_group_dedupe_runs_after_alias_resolution(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    seg = runtime.MessageSegment

    async def resolved(*args):
        # Different input aliases were resolved to the same QQ member.
        return runtime.Message([seg.reply(42), seg.at(123), seg.at("123"), seg.at("456"), seg.text("正文")])

    monkeypatch.setattr(runtime, "_sanitize_group_at_segments", resolved)
    bot = _GroupBot()
    asyncio.run(runtime._send_qq_message_once(bot, {"type": "group", "group_id": 1}, runtime.Message()))
    assert [str(s.data["qq"]) for s in bot.sent[0] if s.type == "at"] == ["123", "456"]
    assert bot.sent[0][0].type == "reply"
    assert bot.sent[0][-1].data["text"] == "正文"


def test_private_reply_does_not_add_group_mention(monkeypatch, tmp_path):
    runtime = _load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "process_emojis", AsyncMock(return_value=[{"type": "text", "content": "你好"}]))
    send = AsyncMock(return_value="100")
    monkeypatch.setattr(runtime, "_send_qq_message", send)
    target = {"type": "private", "user_id": 123, "reply_message_id": 42, "reply_sender_id": 123}
    assert asyncio.run(runtime._send_split_text(object(), target, "test"))
    message = send.call_args.args[2]
    assert [s.type for s in message] == ["reply", "text"]
