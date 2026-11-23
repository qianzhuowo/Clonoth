"""
Google Gemini native API provider.

Implements the BaseProvider interface for Gemini's generateContent /
streamGenerateContent endpoints. Converts OpenAI-format messages and tools
to Gemini's native format.

Created: 2026-05-01
Reason: Complete the provider trio (Anthropic, OpenAI, Gemini) so the engine
        can route to Gemini models natively without an OpenAI-compatible shim.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall

log = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """Provider for Google Gemini (defaults to Vertex AI behavior; 'gemini' compatibility alias)."""

    # [provider-registry 2026-05-03] 这是自动发现注册使用的 key。
    # 原因：engine 不再硬编码 GeminiProvider 分支；做法：类声明 provider_name；
    # 目的：registry 能把配置里的 "gemini" 映射回这个类。
    provider_name = "gemini"
    default_is_vertex: bool = True
    default_max_tokens: int = 65535
    default_thought_summary: bool = True

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
        provider_options: dict[str, Any] | None = None,
    ):
        # [provider-registry 2026-05-03] 实例 name 复用 provider_name。
        # 原因：下游仍通过 provider.name 判断 provider 特性；做法：从类属性传入；
        # 目的：注册 key、实例名和配置 provider 字段保持一致。
        super().__init__(model=model, name=self.provider_name)
        self._http = http
        self._api_key = api_key
        base = (base_url or "https://generativelanguage.googleapis.com").strip().rstrip("/")
        if base.endswith("/v1beta"):
            base = base[:-len("/v1beta")].rstrip("/")
        self._base_url = base
        self._options = provider_options or {}

    def _is_vertex_backend(self) -> bool:
        """Return True if this provider instance should use Vertex AI semantics, False for AI Studio."""
        backend_opt = str(self._options.get("backend") or "").strip().lower()
        if backend_opt in ("vertex", "gemini-vertex", "vertex-gemini"):
            return True
        if backend_opt in ("aistudio", "ai-studio", "gemini-aistudio", "aistudio-gemini"):
            return False
        if self.provider_name == "gemini-vertex":
            return True
        if self.provider_name == "gemini-aistudio":
            return False
        if "generativelanguage.googleapis.com" in (self._base_url or "").lower():
            return False
        return self.default_is_vertex

    def _meta_namespaces(self) -> tuple[str, ...]:
        """Return ordered metadata namespace keys accepted by this backend."""
        if self._is_vertex_backend():
            return ("gemini-vertex", "gemini")
        return ("gemini-aistudio",)

    # ── URL helpers ──────────────────────────────────────────────────

    def _endpoint(self, stream: bool = False) -> str:
        """Build the request URL for generateContent / streamGenerateContent."""
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self._base_url}/v1beta/models/{self.model}:{action}"

    def _headers(self) -> dict[str, str]:
        """Return auth headers.

        Official googleapis.com → x-goog-api-key header.
        Reverse-proxy (any other base_url) → standard Bearer token.
        """
        h: dict[str, str] = {"Content-Type": "application/json"}
        if "googleapis.com" in self._base_url:
            h["x-goog-api-key"] = self._api_key
        else:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    # ── Message conversion ───────────────────────────────────────────

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
        *,
        strip_thoughts: bool = False,
        meta_namespaces: tuple[str, ...] = ("gemini-vertex", "gemini"),
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages → Gemini contents + system instruction.

        Returns (system_text_or_None, gemini_contents_list).

        Conversion rules:
        - leading system/developer → merged into systemInstruction; later ones → user
        - role=user    → role="user", text/image parts
        - role=assistant → role="model", text + optional functionCall parts
        - role=tool     → role="function", functionResponse parts
                          (name is resolved from prior assistant tool_calls via
                           a tool_call_id→name mapping built on the fly)
        """
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        # Map tool_call_id → function name so we can fill functionResponse.name
        # when processing role=tool messages (which only carry tool_call_id).
        tc_id_to_name: dict[str, str] = {}

        seen_non_system = False
        for msg_idx, msg in enumerate(messages):
            role = msg.get("role", "")

            # ── system ──
            if role in ("system", "developer"):
                if not seen_non_system:
                    txt = _extract_text(msg)
                    if txt:
                        system_parts.append(txt)
                    continue
                role = "user"
            seen_non_system = True

            # ── user ──
            if role == "user":
                contents.append({"role": "user", "parts": _user_parts(msg)})
                continue

            # ── assistant / model ──
            if role == "assistant":
                # [2026-05-01] Gemini 3 文档要求 "Return the entire response with
                # all parts back to the model"。任何 part（text/functionCall/thought）
                # 都可能带 thoughtSignature，必须原样回传，不能合并或重建。
                # 优先使用 provider_meta 中保存的原始 parts。
                _meta = msg.get("_meta")
                _raw_parts = None
                if isinstance(_meta, dict):
                    _md = _meta.get("metadata")
                    if isinstance(_md, dict):
                        for _ns in meta_namespaces:
                            _gem_meta = _md.get(_ns)
                            if isinstance(_gem_meta, dict) and _gem_meta.get("raw_parts"):
                                _raw_parts = _gem_meta.get("raw_parts")
                                break

                if _raw_parts and isinstance(_raw_parts, list):
                    # [2026-10-14] 先将流式切碎的连续 thought / text 分片合并，
                    # 避免因流式分片未合并导致 Gemini 报 400 Corrupted thought signature
                    _clean_parts = _coalesce_raw_parts(_raw_parts)
                    # [2026-10-25] 绝不将思维链摘要（thought=True）回传给 Gemini；若其携带 thoughtSignature 则转移保留
                    _clean_parts = _strip_thought_summary_parts(_clean_parts)
                    if strip_thoughts:
                        _clean_parts = _strip_thought_signatures(_clean_parts)
                    # 同时从 functionCall parts 中提取 tc_id_to_name 映射
                    # 使用独立计数器避免多轮 ID 冲突
                    _fc_idx = 0
                    for rp in _clean_parts:
                        fc = rp.get("functionCall")
                        if fc:
                            _fc_idx += 1
                            tc_id_to_name[f"call_{_fc_idx}"] = fc.get("name", "")
                    if _fc_idx > 0:
                        _next_tool_msgs: list[dict[str, Any]] = []
                        for _nxt in messages[msg_idx + 1:]:
                            if not isinstance(_nxt, dict):
                                continue
                            if _nxt.get("role") == "tool":
                                _next_tool_msgs.append(_nxt)
                            else:
                                break
                        if _fc_idx > len(_next_tool_msgs):
                            _clean_parts = _filter_unpaired_fc_parts(
                                _clean_parts, _next_tool_msgs, fallback_text=_extract_text(msg),
                            )
                    if _clean_parts and not (_fc_idx == 0 and msg.get("tool_calls")):
                        _ensure_fc_thought_signature(_clean_parts)
                        contents.append({"role": "model", "parts": _clean_parts})
                        continue

                # 无原始 parts（旧消息、非 Gemini provider 历史、或 strip_thoughts 后为空）：从 OpenAI 格式重建
                parts: list[dict[str, Any]] = []
                txt = _extract_text(msg)
                if txt:
                    parts.append({"text": txt})
                for tc in msg.get("tool_calls") or []:
                    # Support both OpenAI API format {function: {name, arguments}}
                    # and Clonoth simplified storage format {name, arguments}
                    fn = tc.get("function") or {}
                    name = fn.get("name", "") or tc.get("name", "")
                    tc_id = tc.get("id", "")
                    if tc_id:
                        tc_id_to_name[tc_id] = name
                    raw_args = fn.get("arguments", tc.get("arguments", {}))
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            raw_args = {}
                    parts.append({"functionCall": {"name": name, "args": raw_args}})
                if parts:
                    _ensure_fc_thought_signature(parts)
                    contents.append({"role": "model", "parts": parts})
                continue

            # ── tool result ──
            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                # Resolve function name: prefer msg["name"] if present,
                # otherwise look up from the mapping we built.
                name = msg.get("name") or tc_id_to_name.get(tc_id, "unknown")
                raw_content = msg.get("content", "")
                # Gemini expects response to be an object, not a bare string.
                # Try to parse as JSON; fall back to wrapping in {"result": ...}.
                try:
                    resp_obj = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                    if not isinstance(resp_obj, dict):
                        resp_obj = {"result": resp_obj}
                except (json.JSONDecodeError, TypeError):
                    resp_obj = {"result": raw_content}
                # Gemini requires all function responses for the same turn to be
                # grouped in a single role="user" entry with functionResponse parts.
                # (2026-08-19: changed from role="function" to role="user" per current
                # Gemini API spec; the old "function" role is no longer accepted.)
                _fr_part = {"functionResponse": {"name": name, "response": resp_obj}}
                if contents and contents[-1].get("role") == "user" and contents[-1]["parts"] and "functionResponse" in contents[-1]["parts"][-1]:
                    contents[-1]["parts"].append(_fr_part)
                else:
                    contents.append({
                        "role": "user",
                        "parts": [_fr_part],
                    })
                continue

            # ── fallback: treat unknown roles as user ──
            txt = _extract_text(msg)
            if txt:
                contents.append({"role": "user", "parts": [{"text": txt}]})

        system_text = "\n\n".join(system_parts) if system_parts else None
        return system_text, contents

    # ── Tool declaration conversion ──────────────────────────────────

    @staticmethod
    def _convert_tools(
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Convert OpenAI tool declarations → Gemini functionDeclarations.

        OpenAI format:
          [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        Gemini format:
          [{"functionDeclarations": [{"name": ..., "description": ..., "parameters": ...}]}]
        """
        if not tools:
            return None
        decls: list[dict[str, Any]] = []
        for t in tools:
            fn = t.get("function") or t  # tolerate flat format too
            decl: dict[str, Any] = {"name": fn.get("name", "")}
            if fn.get("description"):
                decl["description"] = fn["description"]
            if fn.get("parameters"):
                decl["parameters"] = _clean_schema(fn["parameters"])
            decls.append(decl)
        return [{"functionDeclarations": decls}] if decls else None

    # ── Build request body ───────────────────────────────────────────

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        strip_thoughts: bool = False,
        strip_system_role: bool = False,
    ) -> dict[str, Any]:
        is_vertex = self._is_vertex_backend()
        system_text, contents = self._convert_messages(
            messages,
            strip_thoughts=strip_thoughts,
            meta_namespaces=self._meta_namespaces(),
        )
        if not contents:
            contents = [{"role": "user", "parts": [{"thought": True, "text": " "}]}]
        elif contents[-1].get("role") == "model":
            contents.append({"role": "user", "parts": [{"thought": True, "text": " "}]})
        elif is_vertex and not strip_system_role:
            contents.append({"role": "system", "parts": [{"text": " "}]})
        elif not is_vertex:
            contents.append({
                "role": "model",
                "parts": [{"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": " "}}],
            })
            contents.append({"role": "user", "parts": [{"thought": True, "text": " "}]})
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        gem_tools = self._convert_tools(tools)
        if gem_tools:
            body["tools"] = gem_tools

        custom_safety = self._options.get("safety_settings")
        if isinstance(custom_safety, list):
            body["safetySettings"] = custom_safety
        else:
            model_lower = (self.model or "").lower()
            thresh = (
                "OFF"
                if any(k in model_lower for k in ("gemini-2", "gemini-3", "gemini-4", "image"))
                else "BLOCK_NONE"
            )
            cats = [
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_CIVIC_INTEGRITY",
                "HARM_CATEGORY_JAILBREAK",
            ]
            if is_vertex:
                cats.extend([
                    "HARM_CATEGORY_IMAGE_HATE",
                    "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_IMAGE_HARASSMENT",
                    "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT",
                ])
            body["safetySettings"] = [{"category": c, "threshold": thresh} for c in cats]

        # generationConfig:
        # - max_tokens -> maxOutputTokens (default 65535)
        # - reasoning_effort -> thinkingConfig.thinkingLevel (uppercase; omitted if unset or "NONE")
        # - thought_summary -> thinkingConfig.includeThoughts (default True)
        # - thinkingBudget is never sent
        _raw_max_tok = self._options.get("max_tokens")
        if _raw_max_tok is None:
            _raw_max_tok = self._options.get("max_output_tokens")
        try:
            _max_tok = int(_raw_max_tok) if _raw_max_tok is not None and int(_raw_max_tok) > 0 else self.default_max_tokens
        except (TypeError, ValueError):
            _max_tok = self.default_max_tokens
        gen_config: dict[str, Any] = {"maxOutputTokens": _max_tok}
        if self._options.get("temperature") is not None:
            gen_config["temperature"] = self._options["temperature"]
        if self._options.get("top_p") is not None:
            gen_config["topP"] = self._options["top_p"]
        if self._options.get("top_k") is not None:
            gen_config["topK"] = self._options["top_k"]

        _tc_copy: dict[str, Any] = {}
        _raw_tc = self._options.get("thinking_config")
        if isinstance(_raw_tc, dict):
            _tc_copy.update(_raw_tc)
            _tc_copy.pop("thinkingBudget", None)

        _raw_effort = self._options.get("reasoning_effort")
        if _raw_effort is not None:
            _effort_up = str(_raw_effort).strip().upper()
            if _effort_up and _effort_up != "NONE":
                _tc_copy["thinkingLevel"] = _effort_up
            else:
                _tc_copy.pop("thinkingLevel", None)
        elif "thinkingLevel" in _tc_copy:
            _tl_up = str(_tc_copy.get("thinkingLevel") or "").strip().upper()
            if _tl_up and _tl_up != "NONE":
                _tc_copy["thinkingLevel"] = _tl_up
            else:
                _tc_copy.pop("thinkingLevel", None)

        _raw_ts = self._options.get("thought_summary", self.default_thought_summary)
        if isinstance(_raw_ts, bool):
            _summary_enabled = _raw_ts
        elif isinstance(_raw_ts, str):
            _summary_enabled = _raw_ts.strip().lower() not in ("false", "0", "no", "off", "none")
        else:
            _summary_enabled = bool(_raw_ts)

        if _summary_enabled:
            _tc_copy["includeThoughts"] = True
        elif "thought_summary" in self._options:
            _tc_copy.pop("includeThoughts", None)

        if _tc_copy:
            gen_config["thinkingConfig"] = _tc_copy
        body["generationConfig"] = gen_config
        return body

    # ── Non-streaming chat ───────────────────────────────────────────

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        body = self._build_body(messages, tools)
        try:
            resp = await self._http.post(
                self._endpoint(stream=False),
                headers=self._headers(),
                json=body,
                timeout=300,
            )
        except Exception as exc:
            return ProviderResponse(ok=False, error=f"Gemini request failed: {exc}")

        if resp.status_code != 200:
            err_resp = _error_from_response(resp)
            is_sig_err = _is_thought_signature_error(err_resp.error)
            is_sys_err = _is_system_role_error(err_resp.error)
            if resp.status_code == 400 and (is_sig_err or is_sys_err):
                log.warning(
                    "Gemini 400 (%s); retrying with strip_thoughts=%s, strip_system_role=%s",
                    err_resp.error, is_sig_err, is_sys_err,
                )
                retry_body = self._build_body(
                    messages,
                    tools,
                    strip_thoughts=is_sig_err,
                    strip_system_role=is_sys_err,
                )
                try:
                    resp = await self._http.post(
                        self._endpoint(stream=False),
                        headers=self._headers(),
                        json=retry_body,
                        timeout=300,
                    )
                except Exception as exc:
                    return ProviderResponse(ok=False, error=f"Gemini retry request failed: {exc}")
                if resp.status_code != 200:
                    return _error_from_response(resp)
            else:
                return err_resp

        try:
            data = resp.json()
        except Exception:
            return ProviderResponse(
                ok=False, error="Gemini returned non-JSON response", status_code=resp.status_code,
            )

        return _parse_response(data)

    # ── Streaming chat ───────────────────────────────────────────────

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProviderResponse:
        body = self._build_body(messages, tools)
        try:
            req = self._http.build_request(
                "POST",
                self._endpoint(stream=True),
                headers=self._headers(),
                json=body,
                timeout=300,
            )
            resp = await self._http.send(req, stream=True)
        except Exception as exc:
            return ProviderResponse(ok=False, error=f"Gemini stream request failed: {exc}")

        if resp.status_code != 200:
            msg = await _read_stream_error(resp)
            is_sig_err = _is_thought_signature_error(msg)
            is_sys_err = _is_system_role_error(msg)
            if resp.status_code == 400 and (is_sig_err or is_sys_err):
                log.warning(
                    "Gemini stream 400 (%s); retrying with strip_thoughts=%s, strip_system_role=%s",
                    msg, is_sig_err, is_sys_err,
                )
                retry_body = self._build_body(
                    messages,
                    tools,
                    strip_thoughts=is_sig_err,
                    strip_system_role=is_sys_err,
                )
                try:
                    req = self._http.build_request(
                        "POST",
                        self._endpoint(stream=True),
                        headers=self._headers(),
                        json=retry_body,
                        timeout=300,
                    )
                    resp = await self._http.send(req, stream=True)
                except Exception as exc:
                    return ProviderResponse(ok=False, error=f"Gemini stream retry failed: {exc}")
                if resp.status_code != 200:
                    retry_msg = await _read_stream_error(resp)
                    return ProviderResponse(ok=False, error=retry_msg, status_code=resp.status_code)
            else:
                return ProviderResponse(ok=False, error=msg, status_code=resp.status_code)

        # Accumulate streaming chunks
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, int] = {}
        inline_data_list: list[dict[str, Any]] = []
        tc_counter = 0  # for generating synthetic tool_call ids
        # [2026-05-01] 收集所有原始 parts，Gemini 3 要求整个 response 原样回传
        all_raw_parts: list[dict[str, Any]] = []
        # [2026-08-18] 保留原始响应文本，用于非 SSE 格式（如 Vertex AI JSON 数组）的回退解析
        _raw_chunks: list[str] = []

        try:
            # Gemini SSE: each line is "data: {json}\n\n"
            buf = ""
            async for raw_chunk in resp.aiter_text():
                buf += raw_chunk
                _raw_chunks.append(raw_chunk)
                # Process complete SSE events in buffer
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line.startswith("data: "):
                        continue
                    json_str = line[6:]  # strip "data: " prefix
                    if not json_str.strip():
                        continue
                    try:
                        chunk_data = json.loads(json_str)
                    except json.JSONDecodeError:
                        continue

                    # Extract parts from the first candidate
                    candidates = chunk_data.get("candidates") or []
                    if candidates:
                        parts = (candidates[0].get("content") or {}).get("parts") or []
                        for part in parts:
                            # 收集所有原始 parts 用于 round-trip
                            all_raw_parts.append(part)
                            # Thinking / reasoning parts
                            if part.get("thought"):
                                t = part.get("text", "")
                                if t:
                                    thinking_parts.append(t)
                                    if on_thinking:
                                        await on_thinking(t)
                            # Regular text
                            elif "text" in part:
                                t = part["text"]
                                text_parts.append(t)
                                if on_text:
                                    await on_text(t)
                            # Function calls
                            elif "functionCall" in part:
                                fc = part["functionCall"]
                                tc_counter += 1
                                tc_id = f"call_{tc_counter}"
                                tc_index = tc_counter - 1
                                tc_name = fc.get("name", "")
                                tc_args_raw = fc.get("args") or {}
                                tc_args = tc_args_raw if isinstance(tc_args_raw, dict) else {"_raw": tc_args_raw}
                                # [tool-stream 2026-05-19] Gemini 当前返回完整 functionCall part，而非逐字符参数流。
                                # 原因：没有真实 args delta 时仍要接入统一实时管道。
                                # 做法：收到完整 functionCall 后合成 start、完整 args_delta、done 三个事件。
                                # 目的：前端和 supervisor 不需要为 Gemini 维护另一套非流式工具调用逻辑。
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_start",
                                        "index": tc_index,
                                        "id": tc_id,
                                        "name": tc_name,
                                    })
                                    await on_tool_delta({
                                        "event": "tool_call_args_delta",
                                        "index": tc_index,
                                        "delta": json.dumps(tc_args, ensure_ascii=False),
                                    })
                                    await on_tool_delta({"event": "tool_call_done", "index": tc_index})
                                tool_calls.append(ToolCall(
                                    id=tc_id,
                                    name=tc_name,
                                    arguments=tc_args,
                                ))
                            # Inline data (images etc generated by model)
                            elif "inlineData" in part:
                                inline_data_list.append(part["inlineData"])

                    # Usage metadata (typically present in the last chunk)
                    um = chunk_data.get("usageMetadata")
                    if um:
                        usage = _parse_usage(um)
        except Exception as exc:
            # Partial result is still better than nothing; log and continue
            log.warning("Gemini stream read error: %s", exc)
        finally:
            await resp.aclose()

        # [2026-08-18] Vertex AI / 非 SSE 回退：如果 SSE 解析一无所获，
        # 尝试将原始响应当作 JSON 数组解析（Vertex streamGenerateContent
        # 在不带 alt=sse 时返回 JSON 数组而非 SSE 事件流）。
        if not all_raw_parts and not text_parts and not thinking_parts and not tool_calls and _raw_chunks:
            _raw_body = "".join(_raw_chunks).strip()
            if _raw_body:
                try:
                    _fb = json.loads(_raw_body)
                    if isinstance(_fb, dict):
                        _fb = [_fb]
                    if isinstance(_fb, list):
                        log.info("Gemini SSE empty; JSON-array fallback (%d chunks)", len(_fb))
                        for _fd in _fb:
                            _cands = _fd.get("candidates") or []
                            if not _cands:
                                continue
                            for _p in (_cands[0].get("content") or {}).get("parts") or []:
                                all_raw_parts.append(_p)
                                if _p.get("thought"):
                                    _t = _p.get("text", "")
                                    if _t:
                                        thinking_parts.append(_t)
                                        if on_thinking:
                                            await on_thinking(_t)
                                elif "text" in _p:
                                    _t = _p["text"]
                                    text_parts.append(_t)
                                    if on_text:
                                        await on_text(_t)
                                elif "functionCall" in _p:
                                    _fc = _p["functionCall"]
                                    tc_counter += 1
                                    _tc_id = f"call_{tc_counter}"
                                    _tc_idx = tc_counter - 1
                                    _tc_name = _fc.get("name", "")
                                    _tc_args_raw = _fc.get("args") or {}
                                    _tc_args = _tc_args_raw if isinstance(_tc_args_raw, dict) else {"_raw": _tc_args_raw}
                                    if on_tool_delta:
                                        await on_tool_delta({"event": "tool_call_start", "index": _tc_idx, "id": _tc_id, "name": _tc_name})
                                        await on_tool_delta({"event": "tool_call_args_delta", "index": _tc_idx, "delta": json.dumps(_tc_args, ensure_ascii=False)})
                                        await on_tool_delta({"event": "tool_call_done", "index": _tc_idx})
                                    tool_calls.append(ToolCall(id=_tc_id, name=_tc_name, arguments=_tc_args))
                                elif "inlineData" in _p:
                                    inline_data_list.append(_p["inlineData"])
                            _um = _fd.get("usageMetadata")
                            if _um:
                                usage = _parse_usage(_um)
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass

        full_text = "".join(text_parts) or None
        thought_summary = "".join(thinking_parts) or None

        # [2026-05-01] 存储原始 parts，但 [2026-10-25] 剔除 thought=True 思维链摘要分片
        # （摘要仅存于 _meta["thought_summary"] 一份，不存入 raw_parts 也不回传给 Gemini；
        # 若 thought 分片携带 thoughtSignature 则自动转移保留）
        provider_meta: dict[str, Any] = {}
        if all_raw_parts:
            _coalesced = _coalesce_raw_parts(all_raw_parts)
            _stripped = _strip_thought_summary_parts(_coalesced)
            if _stripped:
                provider_meta["raw_parts"] = _stripped

        return ProviderResponse(
            ok=True,
            text=full_text,
            tool_calls=tool_calls,
            reasoning=None,
            thought_summary=thought_summary,
            usage=usage or None,
            inline_data=inline_data_list,
            raw=None,
            provider_meta=provider_meta,
        )


# ── Module-level helpers ─────────────────────────────────────────────


def _extract_text(msg: dict[str, Any]) -> str | None:
    """Extract text from a message's 'content' field.

    Handles both plain-string content and the OpenAI multi-part
    [{"type": "text", "text": "..."}] format.
    """
    content = msg.get("content")
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    # Multi-part array (OpenAI vision format)
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content if p.get("type") == "text"]
        joined = "".join(texts)
        return joined or None
    return None


def _user_parts(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Gemini 'parts' array for a user message.

    Handles plain text and OpenAI multi-modal content arrays
    (text + image_url with base64 data URIs).
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return [{"text": content}] if content else [{"text": " "}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if item.get("type") == "text":
                parts.append({"text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                # Handle base64 data URIs: "data:<mime>;base64,<data>"
                if url.startswith("data:"):
                    # Parse "data:image/png;base64,iVBOR..."
                    header, _, b64data = url.partition(",")
                    mime = header.split(":", 1)[-1].split(";", 1)[0]
                    parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
                else:
                    # External URL — Gemini supports fileData for GCS URIs;
                    # for http(s) URLs, we'd need to download. For now, pass
                    # as text fallback since Gemini doesn't accept arbitrary URLs.
                    parts.append({"text": f"[Image: {url}]"})
        return parts or [{"text": " "}]
    return [{"text": str(content)}]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Clean a JSON Schema for Gemini compatibility.

    Gemini's function-calling schema support is stricter than OpenAI's.
    Strategy:
    - Drop unsupported keywords outright.
    - Convert exclusiveMinimum/exclusiveMaximum to minimum/maximum (widen
      exclusive bound to inclusive, keep the tighter value when both exist).
    - Recursively clean nested 'properties', 'items', and composition arrays.
    """
    # [2026-10-10] Flatten union types like ["string","null"] to a single
    # value: Gemini's proto type field is a non-repeating enum and rejects
    # lists outright (400: Proto field is not repeating, cannot start list).
    # TS/Zod-based MCP servers emit nullable optional params this way;
    # OpenAI/Anthropic accept them so the issue only surfaces on Gemini.
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        non_null = [x for x in raw_type if x != "null"]
        schema = dict(schema)
        schema["type"] = non_null[0] if non_null else "string"

    # Keywords Gemini's OpenAPI-3.0 subset does not accept at all
    DROP = {"additionalProperties", "default", "$schema", "$id", "$ref",
            "$defs", "definitions", "multipleOf", "uniqueItems",
            "patternProperties", "propertyNames", "const",
            "oneOf", "allOf", "not", "if", "then", "else"}
    # exclusiveMinimum/exclusiveMaximum: convert, not drop
    exc_min = schema.get("exclusiveMinimum")
    exc_max = schema.get("exclusiveMaximum")
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k in DROP or k in ("exclusiveMinimum", "exclusiveMaximum"):
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) if isinstance(pv, dict) else pv for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif k == "items" and isinstance(v, list):
            # [2026-10-10] JSON Schema tuple-style items (list) → Gemini proto
            # expects a singular Schema message; keep the first dict element,
            # fall back to a permissive string schema when none.
            dict_items = [x for x in v if isinstance(x, dict)]
            cleaned[k] = _clean_schema(dict_items[0]) if dict_items else {"type": "string"}
        elif k in ("anyOf",) and isinstance(v, list):
            cleaned[k] = [_clean_schema(item) if isinstance(item, dict) else item for item in v]
        elif k == "enum" and isinstance(v, list):
            # [2026-05-06] Gemini rejects empty enum members in function schemas.
            # Why: stale or dynamically generated tools can contain target enum values
            # such as "" for internal runtime semantics, which causes Gemini to reject
            # the whole GenerateContentRequest before the model runs. How: remove None
            # and blank-string members, de-duplicate the remaining values while keeping
            # their order, and omit enum entirely if nothing valid remains. Purpose: keep
            # Gemini requests valid without changing the runtime handlers that may still
            # accept an omitted or empty argument value.
            enum_values: list[Any] = []
            for item in v:
                if item is None:
                    continue
                if isinstance(item, str) and not item.strip():
                    continue
                if item not in enum_values:
                    enum_values.append(item)
            if enum_values:
                cleaned[k] = enum_values
        else:
            cleaned[k] = v
    # Widen exclusive bounds to inclusive (Gemini has no exclusive bound)
    if isinstance(exc_min, (int, float)):
        cur = cleaned.get("minimum")
        cleaned["minimum"] = max(cur, exc_min) if isinstance(cur, (int, float)) else exc_min
    if isinstance(exc_max, (int, float)):
        cur = cleaned.get("maximum")
        cleaned["maximum"] = min(cur, exc_max) if isinstance(cur, (int, float)) else exc_max
    return cleaned


def _parse_usage(um: dict[str, Any]) -> dict[str, int]:
    """Convert Gemini usageMetadata to the standard usage dict.

    [AutoC 2026-08-24] 缓存命中统一为 Clonoth 内部字段 cached_prompt_tokens。
    Gemini 的 cachedContentTokenCount 归一化到该字段。
    [2026-10-23] 计入 toolUsePromptTokenCount 与 thoughtsTokenCount，保证与 cachedContentTokenCount
    及真实上下文窗口大小口径一致。
    """
    prompt_tokens = int(um.get("promptTokenCount", 0) or 0)
    cached = um.get("cachedContentTokenCount")
    cached_tok = int(cached) if isinstance(cached, int) and cached > 0 else 0
    thoughts = int(um.get("thoughtsTokenCount", 0) or 0)
    completion_tokens = (um.get("candidatesTokenCount", 0) or 0) + thoughts
    total_tokens = um.get("totalTokenCount", 0) or (prompt_tokens + completion_tokens)
    result = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    if cached_tok > 0:
        result["cached_prompt_tokens"] = cached_tok
    if isinstance(um.get("thoughtsTokenCount"), int) and thoughts > 0:
        result["reasoning_tokens"] = thoughts
    return result


class GeminiVertexProvider(GeminiProvider):
    """Explicit Vertex AI Gemini provider (appends trailing role='system', 10 safety categories)."""

    provider_name = "gemini-vertex"
    default_is_vertex = True


class GeminiAIStudioProvider(GeminiProvider):
    """Explicit Google AI Studio Gemini provider (no role='system', 6 safety categories)."""

    provider_name = "gemini-aistudio"
    default_is_vertex = False



def _parse_response(data: dict[str, Any]) -> ProviderResponse:
    """Parse a non-streaming Gemini generateContent response into ProviderResponse."""
    # Check for API-level errors
    if "error" in data:
        err = data["error"]
        return ProviderResponse(
            ok=False,
            error=err.get("message", str(err)),
            status_code=err.get("code"),
        )

    candidates = data.get("candidates") or []
    if not candidates:
        # Could be a safety block or empty response
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            return ProviderResponse(ok=False, error=f"Blocked by safety filter: {block_reason}")
        return ProviderResponse(ok=False, error="Gemini returned no candidates")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    inline_data_list: list[dict[str, Any]] = []
    tc_counter = 0

    for part in parts:
        if part.get("thought"):
            t = part.get("text", "")
            if t:
                thinking_parts.append(t)
        elif "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            tc_counter += 1
            tool_calls.append(ToolCall(
                id=f"call_{tc_counter}",
                name=fc.get("name", ""),
                arguments=fc.get("args") or {},
            ))
        elif "inlineData" in part:
            inline_data_list.append(part["inlineData"])

    usage = _parse_usage(data.get("usageMetadata") or {})
    full_text = "".join(text_parts) or None
    thought_summary = "".join(thinking_parts) or None

    # [2026-10-25] 剔除 thought=True 思维链摘要分片，仅保留非 thought 分片与 thoughtSignature
    provider_meta: dict[str, Any] = {}
    if parts:
        _stripped_parts = _strip_thought_summary_parts(parts)
        if _stripped_parts:
            provider_meta["raw_parts"] = _stripped_parts

    return ProviderResponse(
        ok=True,
        text=full_text,
        tool_calls=tool_calls,
        reasoning=None,
        thought_summary=thought_summary,
        usage=usage or None,
        inline_data=inline_data_list,
        raw=data,
        provider_meta=provider_meta,
    )


def _error_from_response(resp: httpx.Response) -> ProviderResponse:
    """Build an error ProviderResponse from a failed httpx.Response."""
    try:
        data = resp.json()
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        err = data.get("error", {}) if isinstance(data, dict) else {}
        msg = err.get("message", resp.text[:500]) if isinstance(err, dict) else resp.text[:500]
    except Exception:
        msg = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
    return ProviderResponse(ok=False, error=msg, status_code=resp.status_code)


async def _read_stream_error(resp: httpx.Response) -> str:
    """Read and parse error message from a failed streaming response, then close it."""
    try:
        raw_body = await resp.aread()
        error_text = raw_body.decode("utf-8", errors="replace")
        try:
            err_data = json.loads(error_text)
            if isinstance(err_data, list) and err_data and isinstance(err_data[0], dict):
                err_data = err_data[0]
            err_info = err_data.get("error", {}) if isinstance(err_data, dict) else {}
            if isinstance(err_info, dict):
                return err_info.get("message", error_text[:500])
            return error_text[:500]
        except (json.JSONDecodeError, TypeError, AttributeError):
            return error_text[:500]
    except Exception:
        return f"HTTP {resp.status_code}"
    finally:
        await resp.aclose()


def _is_thought_signature_error(err_msg: str | None) -> bool:
    """Return True if the error indicates a corrupted or invalid thought signature."""
    if not err_msg:
        return False
    low = err_msg.lower()
    return "thought signature" in low or "thoughtsignature" in low or "thought_signature" in low


def _is_system_role_error(err_msg: str | None) -> bool:
    """Return True if an AIS channel rejected role='system' in contents."""
    if not err_msg:
        return False
    low = err_msg.lower()
    return "role 'system' is not supported" in low or 'role "system" is not supported' in low


def _coalesce_raw_parts(raw_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coalesce consecutive streamed thought or text parts into complete parts.

    Why: Gemini's streamGenerateContent emits dozens of incremental thought/text
    parts and attaches `thoughtSignature` only to the final chunk of a block.
    Sending the unmerged fragments back on the next turn causes Gemini to validate
    the signature against only the last fragment, failing with 400 Corrupted
    thought signature.
    """
    merged: list[dict[str, Any]] = []
    _NON_TEXT_KEYS = {"functionCall", "inlineData", "fileData", "executableCode", "codeExecutionResult"}

    for p in raw_parts:
        if not isinstance(p, dict):
            continue
        has_special = any(k in p for k in _NON_TEXT_KEYS)
        is_thought = bool(p.get("thought"))

        if not has_special and is_thought:
            if (
                merged
                and bool(merged[-1].get("thought"))
                and not any(k in merged[-1] for k in _NON_TEXT_KEYS)
                and "thoughtSignature" not in merged[-1]
            ):
                merged[-1]["text"] = merged[-1].get("text", "") + p.get("text", "")
                if "thoughtSignature" in p:
                    merged[-1]["thoughtSignature"] = p["thoughtSignature"]
            else:
                new_p = dict(p)
                if "text" not in new_p:
                    new_p["text"] = ""
                merged.append(new_p)
        elif not has_special and "text" in p:
            if (
                merged
                and not merged[-1].get("thought")
                and "text" in merged[-1]
                and not any(k in merged[-1] for k in _NON_TEXT_KEYS)
                and "thoughtSignature" not in merged[-1]
            ):
                merged[-1]["text"] = merged[-1].get("text", "") + p.get("text", "")
                if "thoughtSignature" in p:
                    merged[-1]["thoughtSignature"] = p["thoughtSignature"]
            else:
                merged.append(dict(p))
        else:
            merged.append(dict(p))

    return merged


_SKIP_THOUGHT_SIG = "skip_thought_signature_validator"


def _filter_unpaired_fc_parts(
    parts: list[dict[str, Any]],
    next_tool_msgs: list[dict[str, Any]],
    *,
    fallback_text: str | None = None,
) -> list[dict[str, Any]]:
    """Filter out excess functionCall parts in raw_parts when fewer role=tool responses follow."""
    target_count = len(next_tool_msgs)
    matched_call_ids = {
        str(tm.get("tool_call_id") or "")
        for tm in next_tool_msgs
        if isinstance(tm, dict) and tm.get("tool_call_id")
    }
    fc_ordinals: list[int] = []
    fc_seq = 0
    for p in parts:
        if isinstance(p, dict) and "functionCall" in p:
            fc_seq += 1
            fc_ordinals.append(fc_seq)

    keep_ordinals: set[int] = {
        ord_idx for ord_idx in fc_ordinals if f"call_{ord_idx}" in matched_call_ids
    }
    if len(keep_ordinals) > target_count:
        keep_ordinals = set(sorted(keep_ordinals)[:target_count])
    elif len(keep_ordinals) < target_count:
        for ord_idx in fc_ordinals:
            if len(keep_ordinals) >= target_count:
                break
            keep_ordinals.add(ord_idx)

    preserved_sig: str | None = None
    filtered: list[dict[str, Any]] = []
    cur_fc_ord = 0
    for p in parts:
        if isinstance(p, dict) and "functionCall" in p:
            cur_fc_ord += 1
            if cur_fc_ord in keep_ordinals:
                filtered.append(dict(p))
            elif p.get("thoughtSignature") and not preserved_sig:
                preserved_sig = p["thoughtSignature"]
        else:
            filtered.append(dict(p))

    if preserved_sig and filtered and not any(
        isinstance(p, dict) and "thoughtSignature" in p for p in filtered
    ):
        for p in filtered:
            if isinstance(p, dict) and "functionCall" in p:
                p["thoughtSignature"] = preserved_sig
                break

    if not filtered and fallback_text:
        filtered.append({"text": fallback_text})
    return filtered


def _ensure_fc_thought_signature(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure a model turn with functionCall parts carries a thoughtSignature on its first functionCall."""
    fc_parts = [p for p in parts if isinstance(p, dict) and "functionCall" in p]
    if fc_parts and not any(isinstance(p, dict) and "thoughtSignature" in p for p in parts):
        fc_parts[0]["thoughtSignature"] = _SKIP_THOUGHT_SIG
    return parts


def _strip_thought_summary_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove thought=True summary parts from raw_parts while preserving any thoughtSignature."""
    kept: list[dict[str, Any]] = []
    orphan_sig: str | None = None
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("thought"):
            if p.get("thoughtSignature") and not orphan_sig:
                orphan_sig = p["thoughtSignature"]
            continue
        kept.append(dict(p))
    if orphan_sig and kept and not any(isinstance(p, dict) and "thoughtSignature" in p for p in kept):
        for p in kept:
            if "functionCall" in p:
                p["thoughtSignature"] = orphan_sig
                break
        else:
            kept[0]["thoughtSignature"] = orphan_sig
    return kept


def _strip_thought_signatures(raw_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove thought parts and replace thoughtSignature with bypass validator for cross-account fallback."""
    cleaned: list[dict[str, Any]] = []
    for p in raw_parts:
        if not isinstance(p, dict) or p.get("thought"):
            continue
        cp = {k: v for k, v in p.items() if k != "thoughtSignature"}
        if cp:
            cleaned.append(cp)
    return _ensure_fc_thought_signature(cleaned)
