"""文本协议：工具定义注入 system 文本，模型以 <<<TOOL_CALL>>> JSON 块调用；复用引擎 JsonToolFormatter。"""
from __future__ import annotations

from typing import Any

from engine.inference.tool_format import JsonToolFormatter
from providers.base import ProviderResponse

from . import PROTOCOL_TEXT, ToolProtocol, normalize_stored_tool_calls


class TextToolProtocol(ToolProtocol):
    name = PROTOCOL_TEXT

    def __init__(self) -> None:
        self._formatter = JsonToolFormatter()

    def tool_protocol_text(self, tools: list[dict[str, Any]]) -> str | None:
        text, _ = self._formatter.inject_tool_definitions(list(tools or []), "")
        return text.strip()

    def api_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        return None

    def to_llm(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._formatter.message_to_llm(normalize_stored_tool_calls(dict(m))) for m in messages]

    def parse(self, response: ProviderResponse) -> list[dict[str, Any]]:
        if response.tool_calls:
            return [{"id": c.id, "name": c.name, "arguments": dict(c.arguments or {})} for c in response.tool_calls]
        return [{"id": c.id, "name": c.name, "arguments": dict(c.arguments or {})}
                for c in self._formatter.parse_tool_calls(response)]

    def plain_text(self, response: ProviderResponse) -> str:
        return str(self._formatter.get_plain_text(response) or "").strip()
