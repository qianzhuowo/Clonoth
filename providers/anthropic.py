"""Anthropic native API provider for Clonoth.

Implements the BaseProvider interface for Claude models via the Anthropic
Messages API. Handles format conversion between Clonoth's internal OpenAI-style
messages and Anthropic's native format.

Created: 2026-05-01
Reason: Clonoth previously only supported OpenAI-compatible providers. This adds
        native Anthropic Claude API support with proper message format conversion,
        streaming, tool use, and thinking/reasoning block handling.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers: message format conversion (OpenAI -> Anthropic)
# ---------------------------------------------------------------------------


def _is_anthropic_domain(base_url: str) -> bool:
    """Check if the base_url points to Anthropic's official API.
    Used to decide authentication header style.
    """
    return "anthropic.com" in base_url.lower()


def _convert_image_part(part: dict) -> dict:
    """Convert an OpenAI vision image_url part to Anthropic image source format.

    OpenAI format:
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC..."}}
    Anthropic format:
      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC..."}}
    """
    url = part.get("image_url", {}).get("url", "")
    # Parse data URI: data:<media_type>;base64,<data>
    m = re.match(r"data:([^;]+);base64,(.+)", url, re.DOTALL)
    if m:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": m.group(1),
                "data": m.group(2),
            },
        }
    # If it's a plain URL (not data URI), use url source type
    if url:
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": url,
            },
        }
    # Fallback: return as text describing the issue
    return {"type": "text", "text": "[image could not be converted]"}


def _content_to_blocks(content: Any) -> list[dict]:
    """Convert OpenAI message content (string or list) to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks = []
        for part in content:
            ptype = part.get("type", "")
            if ptype == "text":
                if part.get("text"):
                    blocks.append({"type": "text", "text": part["text"]})
            elif ptype == "image_url":
                blocks.append(_convert_image_part(part))
            else:
                # Unknown part type — pass text representation
                blocks.append({"type": "text", "text": str(part)})
        return blocks
    # Fallback for None or other types
    return []


def _convert_tools(tools: list[dict] | None) -> list[dict]:
    """Convert OpenAI function-calling tool definitions to Anthropic tool format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    """
    if not tools:
        return []
    result = []
    for t in tools:
        fn = t.get("function", {})
        converted = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        }
        result.append(converted)
    return result


def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert OpenAI-format messages to Anthropic format.

    Returns (system_text, converted_messages).

    Key transformations:
    1. system messages -> extracted into a single system string
    2. tool result messages (role=tool) -> grouped into user messages with tool_result blocks
    3. assistant messages with tool_calls -> content blocks with tool_use entries
    4. Consecutive same-role messages are merged (Anthropic requires strict alternation)
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            # Extract system messages into a single string
            if content:
                text = content if isinstance(content, str) else str(content)
                system_parts.append(text)
            i += 1
            continue

        if role == "tool":
            # Collect consecutive tool result messages into one user message
            # Anthropic represents tool results as user messages with tool_result content blocks
            tool_results: list[dict] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                tool_result_content = tm.get("content", "")
                # tool result content can be string or structured
                if isinstance(tool_result_content, str):
                    tr_content = tool_result_content
                else:
                    tr_content = json.dumps(tool_result_content, ensure_ascii=False)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id", ""),
                    "content": tr_content,
                })
                i += 1
            converted.append({"role": "user", "content": tool_results})
            continue

        if role == "assistant":
            blocks: list[dict] = []

            # [2026-05-01] 从 _meta.metadata.anthropic.thinking_blocks 中恢复
            # 上一轮 LLM 返回的 thinking/redacted_thinking blocks（含 signature），
            # 插入到 assistant content 最前面，满足 Anthropic extended thinking
            # 要求每个 assistant 消息的 thinking blocks 必须原样回传。
            _meta = msg.get("_meta")
            if isinstance(_meta, dict):
                _anth_meta = _meta.get("metadata", {}).get("anthropic", {})
                _saved_blocks = _anth_meta.get("thinking_blocks", [])
                for tb in _saved_blocks:
                    tb_type = tb.get("type", "")
                    if tb_type == "thinking":
                        blocks.append({
                            "type": "thinking",
                            "thinking": tb.get("thinking", ""),
                            "signature": tb.get("signature", ""),
                        })
                    elif tb_type == "redacted_thinking":
                        blocks.append({
                            "type": "redacted_thinking",
                            "data": tb.get("data", ""),
                        })

            # Add text content if present
            if content:
                text_blocks = _content_to_blocks(content)
                blocks.extend(text_blocks)
            # Convert tool_calls to tool_use blocks
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {})
                args_str = fn.get("arguments", "{}")
                try:
                    args_parsed = json.loads(args_str)
                except (json.JSONDecodeError, TypeError):
                    args_parsed = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args_parsed,
                })
            # Anthropic requires non-empty content; if empty, add a placeholder
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            converted.append({"role": "assistant", "content": blocks})
            i += 1
            continue

        if role == "user":
            blocks = _content_to_blocks(content)
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            converted.append({"role": "user", "content": blocks})
            i += 1
            continue

        # Unknown role — treat as user
        log.warning("Unknown message role %r, treating as user", role)
        blocks = _content_to_blocks(content) or [{"type": "text", "text": str(content)}]
        converted.append({"role": "user", "content": blocks})
        i += 1

    # Merge consecutive same-role messages (Anthropic requires strict user/assistant alternation)
    merged: list[dict] = []
    for msg in converted:
        if merged and merged[-1]["role"] == msg["role"]:
            # Merge content blocks
            prev_content = merged[-1]["content"]
            curr_content = msg["content"]
            if isinstance(prev_content, list) and isinstance(curr_content, list):
                prev_content.extend(curr_content)
            elif isinstance(prev_content, list):
                prev_content.extend(_content_to_blocks(curr_content))
            else:
                merged[-1]["content"] = _content_to_blocks(prev_content) + _content_to_blocks(curr_content)
        else:
            merged.append(msg)

    # Anthropic requires the first message to be from 'user'.
    # If the first message is 'assistant', prepend a placeholder user message.
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})

    system_text = "\n\n".join(system_parts)
    return system_text, merged


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _parse_response_content(content_blocks: list[dict]) -> tuple[str, str, list[ToolCall], list[dict]]:
    """Parse Anthropic response content blocks into (text, reasoning, tool_calls, thinking_blocks).

    Content block types:
    - {"type": "thinking", "thinking": "...", "signature": "..."} -> reasoning + thinking_blocks
    - {"type": "redacted_thinking", "data": "..."} -> thinking_blocks (opaque, must round-trip)
    - {"type": "text", "text": "..."} -> text
    - {"type": "tool_use", "id": "...", "name": "...", "input": {...}} -> tool_calls

    [2026-05-01] 新增 thinking_blocks 返回值：保留含 signature 的原始 thinking block，
    用于多轮对话时回传给 Anthropic API，满足 extended thinking 签名验证要求。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    # [2026-05-01] 收集原始 thinking/redacted_thinking blocks，
    # 保留 signature 字段以供后续轮次回传
    thinking_blocks: list[dict] = []

    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "thinking":
            thinking_text = block.get("thinking", "")
            if thinking_text:
                reasoning_parts.append(thinking_text)
            # 保留整个 block（含 signature）用于 round-trip
            thinking_blocks.append(block)
        elif btype == "redacted_thinking":
            # redacted_thinking 是不透明的加密块，必须原样回传
            thinking_blocks.append(block)
        elif btype == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(t)
        elif btype == "tool_use":
            raw_input = block.get("input", {})
            args = raw_input if isinstance(raw_input, dict) else {}
            tool_calls.append(ToolCall(
                id=block.get("id", ""),
                name=block.get("name", ""),
                arguments=args,
            ))

    return "\n".join(text_parts), "\n".join(reasoning_parts), tool_calls, thinking_blocks


def _parse_usage(data: dict) -> dict:
    """Convert Anthropic usage to OpenAI-style usage dict."""
    usage = data.get("usage", {})
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    return {
        "prompt_tokens": inp,
        "completion_tokens": out,
        "total_tokens": inp + out,
    }


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


class AnthropicProvider(BaseProvider):
    """Native Anthropic Messages API provider.

    Accepts OpenAI-format messages from the engine and converts them to
    Anthropic format internally. Supports non-streaming (chat) and
    streaming (chat_stream) modes, tool use, and thinking/reasoning blocks.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
        provider_options: dict[str, Any] | None = None,
    ):
        super().__init__(model=model, name="anthropic")
        self._http = http
        self._api_key = api_key
        # Default to official Anthropic API; strip trailing slash
        self._base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self._options = provider_options or {}

    # -- Auth headers --

    def _headers(self) -> dict[str, str]:
        """Build request headers.
        Official Anthropic API uses x-api-key; reverse proxies use Bearer token.
        """
        h = {
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if _is_anthropic_domain(self._base_url):
            h["x-api-key"] = self._api_key
        else:
            # Reverse proxy — use standard Bearer auth
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/v1/messages"

    # -- Build request payload --

    def _build_payload(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int = 16384,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict:
        """Build the Anthropic API request payload from OpenAI-format inputs."""
        system_text, converted = _convert_messages(messages)
        converted_tools = _convert_tools(tools)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
        }
        if system_text:
            payload["system"] = system_text
        if converted_tools:
            payload["tools"] = converted_tools
        if temperature is not None:
            payload["temperature"] = temperature
        if stream:
            payload["stream"] = True
        # 注入 thinking 参数（如 {"type": "adaptive"} 或 {"type": "enabled", "budget_tokens": N}）
        _thinking = self._options.get("thinking")
        if _thinking and isinstance(_thinking, dict):
            payload["thinking"] = _thinking
        return payload

    # ===================================================================
    # Non-streaming: chat()
    # ===================================================================

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ProviderResponse:
        """Non-streaming request to Anthropic Messages API."""
        payload = self._build_payload(messages, tools=tools, stream=False)

        try:
            resp = await self._http.post(
                self._endpoint,
                headers=self._headers(),
                json=payload,
                timeout=300.0,
            )
        except Exception as exc:
            log.error("Anthropic request failed: %s", exc)
            return ProviderResponse(ok=False, error=str(exc), status_code=0)

        if resp.status_code != 200:
            return self._error_response(resp)

        data = resp.json()
        content_blocks = data.get("content", [])
        text, reasoning, tool_calls, thinking_blocks = _parse_response_content(content_blocks)
        usage = _parse_usage(data)

        # [2026-05-01] 将含 signature 的原始 thinking blocks 存入 provider_meta，
        # engine 会自动持久化到消息 _meta.metadata.anthropic，
        # 下一轮 _convert_messages 读取后原样回传给 API
        provider_meta: dict[str, Any] = {}
        if thinking_blocks:
            provider_meta["thinking_blocks"] = thinking_blocks

        return ProviderResponse(
            ok=True,
            text=text,
            reasoning=reasoning or None,
            tool_calls=tool_calls,
            usage=usage,
            raw=data,
            provider_meta=provider_meta,
        )

    # ===================================================================
    # Streaming: chat_stream()
    # ===================================================================

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        """Streaming request to Anthropic Messages API via SSE.

        Anthropic SSE event types:
        - message_start: message metadata
        - content_block_start: new content block (text / thinking / tool_use)
        - content_block_delta: incremental content (text_delta / thinking_delta / input_json_delta)
        - content_block_stop: block finished
        - message_delta: stop_reason and final usage
        - message_stop: end of message
        """
        payload = self._build_payload(messages, tools=tools, stream=True)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Track active tool_use blocks by index
        # Maps block index -> {"id": ..., "name": ..., "args_parts": [...]}
        active_tools: dict[int, dict] = {}
        tool_calls: list[ToolCall] = []
        usage: dict = {}
        input_usage: dict = {}  # from message_start
        # [2026-05-01] 追踪流式 thinking blocks，收集 signature 用于 round-trip。
        # active_thinking: 正在流式接收中的 thinking block，key=block index
        # thinking_blocks: 已完成的原始 thinking blocks（含 signature）
        active_thinking: dict[int, dict] = {}
        thinking_blocks: list[dict] = []

        try:
            async with self._http.stream(
                "POST",
                self._endpoint,
                headers=self._headers(),
                json=payload,
                timeout=300.0,
            ) as resp:
                if resp.status_code != 200:
                    # Read error body
                    body = await resp.aread()
                    return self._error_from_body(resp.status_code, body)

                # Parse SSE lines
                # Anthropic sends: "event: <type>\ndata: <json>\n\n"
                current_event = ""
                async for line in resp.aiter_lines():
                    line = line.rstrip()
                    if not line:
                        continue
                    if line.startswith("event: "):
                        current_event = line[7:]
                        continue
                    if not line.startswith("data: "):
                        continue

                    raw_data = line[6:]
                    if raw_data == "[DONE]":
                        break

                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        log.warning("Anthropic SSE: bad JSON: %s", raw_data[:200])
                        continue

                    evt = data.get("type", current_event)

                    if evt == "message_start":
                        # Extract input usage from message_start
                        msg = data.get("message", {})
                        mu = msg.get("usage", {})
                        if mu:
                            input_usage = mu

                    elif evt == "content_block_start":
                        block = data.get("content_block", {})
                        idx = data.get("index", 0)
                        if block.get("type") == "tool_use":
                            # Start tracking a tool_use block
                            active_tools[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "args_parts": [],
                            }
                        elif block.get("type") == "thinking":
                            # [2026-05-01] 开始追踪 thinking block，
                            # 累积文本，最终在 content_block_stop 时组装含 signature 的完整 block
                            active_thinking[idx] = {"type": "thinking", "thinking_parts": []}
                        elif block.get("type") == "redacted_thinking":
                            # [2026-05-01] redacted_thinking 是不透明加密块，
                            # content_block_start 时就包含完整 data，直接收集
                            thinking_blocks.append({"type": "redacted_thinking", "data": block.get("data", "")})

                    elif evt == "content_block_delta":
                        delta = data.get("delta", {})
                        dtype = delta.get("type", "")
                        idx = data.get("index", 0)

                        if dtype == "thinking_delta":
                            chunk = delta.get("thinking", "")
                            if chunk:
                                reasoning_parts.append(chunk)
                                # [2026-05-01] 同时累积到 active_thinking 以便组装完整 block
                                if idx in active_thinking:
                                    active_thinking[idx]["thinking_parts"].append(chunk)
                                if on_thinking:
                                    await on_thinking(chunk)

                        elif dtype == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                text_parts.append(chunk)
                                if on_text:
                                    await on_text(chunk)

                        elif dtype == "signature_delta":
                            # [2026-05-01] Anthropic 流式模式下，thinking block 的
                            # signature 作为单独的 signature_delta 事件发送，
                            # 出现在该 block 最后一个 thinking_delta 之后、
                            # content_block_stop 之前
                            sig = delta.get("signature", "")
                            if sig and idx in active_thinking:
                                active_thinking[idx]["signature"] = sig

                        elif dtype == "input_json_delta":
                            # Accumulate tool arguments JSON string
                            partial = delta.get("partial_json", "")
                            if partial and idx in active_tools:
                                active_tools[idx]["args_parts"].append(partial)

                    elif evt == "content_block_stop":
                        idx = data.get("index", 0)
                        # [2026-05-01] 完成 thinking block：组装含 signature 的完整 block
                        if idx in active_thinking:
                            info = active_thinking.pop(idx)
                            full_thinking = "".join(info.get("thinking_parts", []))
                            tb: dict[str, Any] = {"type": "thinking", "thinking": full_thinking}
                            if "signature" in info:
                                tb["signature"] = info["signature"]
                            thinking_blocks.append(tb)
                        elif idx in active_tools:
                            # Finalize the tool call
                            tool_info = active_tools.pop(idx)
                            args_str = "".join(tool_info["args_parts"])
                            try:
                                parsed_args = json.loads(args_str) if args_str.strip() else {}
                            except (json.JSONDecodeError, TypeError):
                                parsed_args = {"_raw": args_str}
                            tc = ToolCall(
                                id=tool_info["id"],
                                name=tool_info["name"],
                                arguments=parsed_args if isinstance(parsed_args, dict) else {"_raw": parsed_args},
                            )
                            tool_calls.append(tc)

                    elif evt == "message_delta":
                        # Final usage info
                        du = data.get("usage", {})
                        if du:
                            usage = du

                    elif evt == "error":
                        err = data.get("error", {})
                        err_msg = err.get("message", str(data))
                        log.error("Anthropic stream error event: %s", err_msg)
                        return ProviderResponse(
                            ok=False, error=err_msg, status_code=resp.status_code,
                        )

        except Exception as exc:
            log.error("Anthropic stream failed: %s", exc)
            return ProviderResponse(ok=False, error=str(exc), status_code=0)

        # Build final usage combining message_start and message_delta
        inp_tokens = input_usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        final_usage = {
            "prompt_tokens": inp_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": inp_tokens + out_tokens,
        }

        text = "".join(text_parts)
        reasoning = "".join(reasoning_parts)

        # [2026-05-01] 与非流式路径对齐：将含 signature 的 thinking blocks 存入 provider_meta
        provider_meta: dict[str, Any] = {}
        if thinking_blocks:
            provider_meta["thinking_blocks"] = thinking_blocks

        return ProviderResponse(
            ok=True,
            text=text,
            reasoning=reasoning or None,
            tool_calls=tool_calls,
            usage=final_usage,
            provider_meta=provider_meta,
        )

    # -- Error handling helpers --

    def _error_response(self, resp: httpx.Response) -> ProviderResponse:
        """Parse a non-200 response into a ProviderResponse."""
        try:
            body = resp.json()
            err = body.get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            if not msg:
                msg = resp.text[:500]
        except Exception:
            msg = resp.text[:500]
        log.error("Anthropic API error %d: %s", resp.status_code, msg)
        return ProviderResponse(ok=False, error=msg, status_code=resp.status_code)

    def _error_from_body(self, status_code: int, body: bytes) -> ProviderResponse:
        """Parse an error from raw response body bytes."""
        try:
            data = json.loads(body)
            err = data.get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            if not msg:
                msg = body.decode("utf-8", errors="replace")[:500]
        except Exception:
            msg = body.decode("utf-8", errors="replace")[:500]
        log.error("Anthropic API error %d: %s", status_code, msg)
        return ProviderResponse(ok=False, error=msg, status_code=status_code)
