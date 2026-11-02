"""Channel context is consumed by delivered replies, not accepted inbounds."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.discord import callbacks, context
from adapters.discord.messaging import _record_bot_reply
from clonoth_sdk import SessionState, TriggerInfo


def runtime():
    return SimpleNamespace(
        session_state=SessionState(), channel_history={}, history_seq_counter=0,
        transient_msg_ids=deque(maxlen=1000),
        history_max_len=100, key_roles=set(), superusers=set(), restart_signal="<restart>",
        discord_voice_reply=False, discord_voice_reply_on_voice_input=False,
    )


def test_parallel_question_remains_until_reply_then_preserves_later_messages(monkeypatch):
    rt = runtime()
    monkeypatch.setattr(context, "_format_identity", lambda *a, **kw: "User")
    member = SimpleNamespace(id=1)
    context._push_history(rt, 42, "A asks first")
    snapshot_a = rt.history_seq_counter - 1
    # SDK acceptance has nothing registered to consume in the Discord adapter.
    rt.session_state.accept_watermark(100)
    context._push_history(rt, 42, "B asks concurrently")
    snapshot_b = rt.history_seq_counter - 1
    before = context._build_context_text(rt, 42, member, "B", exclude_last=True)
    assert "A asks first" in before
    assert "【群聊上下文记录】" in before

    _record_bot_reply(rt, 42, "Answer A", context_watermark=snapshot_a)
    after = context._build_context_text(rt, 42, member, "C")
    assert "A asks first" not in after
    assert "B asks concurrently" in after
    assert "Answer A" not in after

    _record_bot_reply(rt, 42, "Answer B", context_watermark=snapshot_b)
    _record_bot_reply(rt, 42, "Late A", context_watermark=snapshot_a)
    assert rt.session_state.get_high_watermark(42) == snapshot_b


def test_dm_context_keeps_private_header(monkeypatch):
    rt = runtime()
    monkeypatch.setattr(context, "_format_identity", lambda *a, **kw: "User")
    context._push_history(rt, 42, "Private question")
    result = context._build_context_text(rt, 42, SimpleNamespace(id=1), "Next", is_dm=True)
    assert "【私聊上下文记录】" in result
    assert "【群聊上下文记录】" not in result


@pytest.mark.parametrize("outcome", ["text", "attachment", "failure", "empty", "voice"])
def test_only_successful_final_delivery_commits_snapshot(monkeypatch, outcome):
    rt = runtime()
    sender = AsyncMock(return_value=[SimpleNamespace(id=123)])
    if outcome == "failure":
        sender.side_effect = RuntimeError("Discord unavailable")
    elif outcome == "empty":
        sender.return_value = []
    monkeypatch.setattr(callbacks, "_send_split_text", sender)
    monkeypatch.setattr(callbacks, "_atts_to_discord_files", lambda *args: [])
    monkeypatch.setattr(callbacks, "_extract_reactions", lambda rt, text: (text, []))
    voice_sender = AsyncMock(return_value=True)
    monkeypatch.setattr(callbacks, "_send_discord_tts_segments", voice_sender)
    monkeypatch.setattr(callbacks, "prepare_discord_tts_reply", lambda *args: ("", [{"text": "Spoken"}]) if outcome == "voice" else (args[1], []))
    trigger = TriggerInfo(
        inbound_seq=100, conversation_key="discord:42", session_id="s", is_dm=False,
        platform_data={"message": SimpleNamespace(channel=object()), "channel_id": 42, "_context_watermark": 7},
    )
    cb = callbacks.DiscordCallbacks(rt)
    attachments = [{"path": "file"}] if outcome == "attachment" else []
    asyncio.run(cb.send_reply(trigger, "" if attachments else "Answer", attachments))
    assert rt.session_state.get_high_watermark(42) == (-1 if outcome in {"failure", "empty"} else 7)
    if outcome == "voice":
        voice_sender.assert_awaited_once()


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("react_ok", [False, True])
def test_pure_reaction_commits_watermark_without_history(monkeypatch, intermediate, react_ok):
    rt = runtime()
    context._push_history(rt, 42, "Question")
    reaction = AsyncMock()
    if not react_ok:
        reaction.side_effect = RuntimeError("Reaction unavailable")
    msg = SimpleNamespace(channel=object(), add_reaction=reaction)
    monkeypatch.setattr(callbacks, "_extract_reactions", lambda *args: ("", ["👍"]))
    sender = AsyncMock()
    monkeypatch.setattr(callbacks, "_send_split_text", sender)
    trigger = TriggerInfo(
        inbound_seq=105, conversation_key="discord:42", session_id="s", is_dm=False,
        platform_data={"message": msg, "channel_id": 42, "_context_watermark": 9},
    )
    cb = callbacks.DiscordCallbacks(rt)
    send = cb.send_intermediate_reply if intermediate else cb.send_reply
    asyncio.run(send(trigger, "[REACT:👍]", []))
    assert rt.session_state.get_high_watermark(42) == (9 if react_ok else -1)
    sender.assert_not_awaited()
    assert len(rt.channel_history[42]) == 1
    assert context._previous_history_text(rt, 42) == "Question"
    if react_ok:
        trigger.platform_data["_context_watermark"] = 5
        asyncio.run(send(trigger, "[REACT:👍]", []))
        assert rt.session_state.get_high_watermark(42) == 9


def test_transient_bot_reference_is_filtered_and_reversible(monkeypatch):
    rt = runtime()
    rt.dc_client = SimpleNamespace(user=SimpleNamespace(id=999))
    ref = SimpleNamespace(id=123, author=SimpleNamespace(id=999), content="⏳ 处理中...")
    monkeypatch.setattr(context.discord, "Message", SimpleNamespace)
    context.register_transient_msg(rt, ref)
    msg = SimpleNamespace(reference=SimpleNamespace(resolved=ref, message_id=123))
    assert asyncio.run(context._build_reply_context(rt, msg)) == ""
    assert not context._is_transient_bot_message(rt, 124)
    context.unregister_transient_msg(rt, ref)
    assert not context._is_transient_bot_message(rt, 123)
    assert rt.transient_msg_ids.maxlen == 1000


def test_preempt_context_uses_branch_watermark(monkeypatch):
    rt = runtime()
    monkeypatch.setattr(context, "_format_identity", lambda *args, **kw: "User")
    context._push_history(rt, 42, "Already injected")
    first_seq = rt.history_seq_counter - 1
    context._push_history(rt, 42, "New branch context")
    rt.session_state.last_ctx_seq[42] = 100
    text = context._build_context_text(rt, 42, SimpleNamespace(id=1), "Next", min_seq_override=first_seq)
    assert "Already injected" not in text
    assert "New branch context" in text
