"""原生 function calling 协议：保留 assistant.tool_calls 与 role=tool 配对。"""
from __future__ import annotations

from typing import Any

from engine.inference.tool_format import NativeToolFormatter
from providers.base import ProviderResponse

from . import PROTOCOL_NATIVE, ToolProtocol, normalize_stored_tool_calls


class NativeToolProtocol(ToolProtocol):
    name = PROTOCOL_NATIVE

    def __init__(self) -> None:
        self._formatter = NativeToolFormatter()

    def tool_protocol_text(self, tools: list[dict[str, Any]]) -> str | None:
        return None

    def api_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        return list(tools) if tools else None

    def to_llm(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._formatter.message_to_llm(normalize_stored_tool_calls(dict(m))) for m in messages]

    def parse(self, response: ProviderResponse) -> list[dict[str, Any]]:
        return [{"id": c.id, "name": c.name, "arguments": dict(c.arguments or {})}
                for c in self._formatter.parse_tool_calls(response)]
