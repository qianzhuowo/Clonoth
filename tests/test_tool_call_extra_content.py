"""Gemini (OpenAI-compat) thought signatures on tool calls must round-trip verbatim."""
from __future__ import annotations

from providers.base import ProviderResponse, ToolCall
from providers.openai import OpenAIProvider
from engine.inference.tool_format import NativeToolFormatter

SIG = {"google": {"thought_signature": "sig-abc"}}


def test_assistant_message_stores_and_replays_extra_content():
    fmt = NativeToolFormatter()
    resp = ProviderResponse(ok=True, text="", tool_calls=[])
    calls = [ToolCall(id="call_1", name="read_media", arguments={"path": "x.png"}, extra_content=SIG),
             ToolCall(id="call_2", name="search_web", arguments={"query": "q"})]
    stored = fmt.build_assistant_message(resp, "", calls)
    assert stored["tool_calls"][0]["extra_content"] == SIG
    assert "extra_content" not in stored["tool_calls"][1]

    api = fmt.message_to_llm(stored)
    assert api["tool_calls"][0]["extra_content"] == SIG
    assert api["tool_calls"][0]["function"]["name"] == "read_media"
    assert "extra_content" not in api["tool_calls"][1]

    sent = OpenAIProvider._prepare_messages([api])
    assert sent[0]["tool_calls"][0]["extra_content"] == SIG


def test_messages_without_extra_content_are_unchanged():
    fmt = NativeToolFormatter()
    stored = fmt.build_assistant_message(
        ProviderResponse(ok=True, text="", tool_calls=[]), "",
        [ToolCall(id="c", name="t", arguments={})],
    )
    assert stored["tool_calls"] == [{"id": "c", "name": "t", "arguments": {}}]
