"""工具协议抽象：运行时消息（存储格式）⇄ 模型请求 / 响应。

存储格式（与引擎 ConversationStore 一致）：
- assistant: {"role":"assistant","content":str,"tool_calls":[{"id","name","arguments":dict}]}
- tool:      {"role":"tool","tool_call_id":str,"name":str,"content":str}
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from providers.base import ProviderResponse

PROTOCOL_NATIVE = "native"
PROTOCOL_TEXT = "text"
PROTOCOLS: tuple[str, ...] = (PROTOCOL_NATIVE, PROTOCOL_TEXT)


def normalize_stored_tool_calls(message: dict[str, Any]) -> dict[str, Any]:
    """把 API 形状的 tool_calls（{id,type,function:{name,arguments:str}}）归一为存储格式。"""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return message
    stored: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        if "function" in call and isinstance(call["function"], dict):
            fn = call["function"]
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except ValueError:
                    args = {"_raw": raw_args}
            else:
                args = raw_args if isinstance(raw_args, dict) else {}
            stored.append({"id": str(call.get("id") or ""), "name": str(fn.get("name") or ""), "arguments": args})
        else:
            args = call.get("arguments", {})
            stored.append({"id": str(call.get("id") or ""), "name": str(call.get("name") or ""),
                           "arguments": args if isinstance(args, dict) else {}})
    out = dict(message)
    out["tool_calls"] = stored
    return out


class ToolProtocol(ABC):
    name: str = ""

    @abstractmethod
    def tool_protocol_text(self, tools: list[dict[str, Any]]) -> str | None:
        """文本协议：替换 TOOL_PROTOCOL_MARKER 的工具说明；原生协议返回 None（标记整段删除）。"""

    @abstractmethod
    def api_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """随请求发送的 tools 参数；文本协议为 None。"""

    @abstractmethod
    def to_llm(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """存储格式 → 模型请求消息。"""

    @abstractmethod
    def parse(self, response: ProviderResponse) -> list[dict[str, Any]]:
        """响应 → [{id, name, arguments(dict|str)}]。"""

    def assistant_message(self, response: ProviderResponse, calls: list[dict[str, Any]]) -> dict[str, Any]:
        text = self.plain_text(response)
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [
                {"id": c["id"], "name": c["name"],
                 "arguments": c["arguments"] if isinstance(c["arguments"], dict) else {"_raw": c["arguments"]}}
                for c in calls
            ]
        return message

    def plain_text(self, response: ProviderResponse) -> str:
        return str(response.text or "").strip()

    @staticmethod
    def tool_result_message(call: dict[str, Any], content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": str(call.get("id") or ""), "name": str(call.get("name") or ""),
                "content": content}


def create_tool_protocol(name: str | None) -> ToolProtocol:
    key = str(name or PROTOCOL_NATIVE).strip().lower()
    if key == PROTOCOL_TEXT:
        from .text import TextToolProtocol
        return TextToolProtocol()
    from .native import NativeToolProtocol
    return NativeToolProtocol()
