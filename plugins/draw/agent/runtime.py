"""场景规划循环（对应 LWB draw-agent-runtime.js 的 callDrawScenePlannerAgentRuntime 内层）。

一次 run：
1. 轮询打断：有新消息则作为 user 追加并上报消费；任务被取消则抛 REQUEST_ABORTED。
2. 调模型（协议层转换消息 / 工具），累加用量并发 context_usage 事件。
3. 解析工具调用：
   - 只调了自定义工具（如 search_web）→ 执行并把结果作为 tool 返回接回，不占纠错次数；
   - 否则交给 validate（submit_scene_plan 合同）；
4. 校验失败且可纠错 → 把纠错 JSON 作为工具返回（无 tool_calls 时作为 user 文本）接回，
   下一 attempt 重试；同一错误签名连续出现或 attempt 用尽 → 抛出。
所有进入上下文的消息都经 on_message 回调（entry 用它写历史）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from providers.base import ProviderResponse

from ..planner.contract import (
    SUBMIT_SCENE_PLAN_TOOL_NAME,
    ScenePlannerError,
    create_correction_result,
    get_correction_signature,
    is_correction_error,
)
from .protocol import ToolProtocol

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_CUSTOM_TOOL_CALLS = 5
CORRECTION_PREFIX = "场景规划协议纠错："
SKIPPED_TOOL_NOTE = {"skipped": True, "note": f"与 {SUBMIT_SCENE_PLAN_TOOL_NAME} 同一批调用，没有执行。"}

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
MessageSink = Callable[[dict[str, Any]], None]
Validator = Callable[[list[dict[str, Any]]], dict[str, Any]]


@dataclass
class PlannerRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    validate: Validator
    custom_tool_names: frozenset[str] = frozenset()
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_custom_tool_calls: int = DEFAULT_MAX_CUSTOM_TOOL_CALLS
    timeout_sec: float | None = None


@dataclass
class PlannerResult:
    tasks: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    attempts: int
    custom_tool_calls: int = 0
    argument_repair: dict[str, Any] | None = None
    corrections: list[dict[str, Any]] = field(default_factory=list)
    preempt_messages: list[str] = field(default_factory=list)


def _validation_arguments(call: Mapping[str, Any]) -> Any:
    args = call.get("arguments")
    if isinstance(args, Mapping) and set(args.keys()) == {"_raw"}:
        return args["_raw"]
    return args


def _usage_int(usage: Mapping[str, Any] | None, key: str) -> int:
    value = (usage or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class PlannerRuntime:
    def __init__(self, *, provider: Any, protocol: ToolProtocol, rctx: Any, node_id: str,
                 execute_tool: ToolExecutor | None = None, on_message: MessageSink | None = None) -> None:
        self.provider = provider
        self.protocol = protocol
        self.rctx = rctx
        self.node_id = node_id
        self.execute_tool = execute_tool
        self.on_message = on_message

    # ---- helpers ----

    def _append(self, messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
        messages.append(message)
        if self.on_message is not None:
            try:
                self.on_message(message)
            except Exception:  # noqa: BLE001
                logger.debug("draw agent: on_message 回调失败", exc_info=True)

    async def _poll_preempt(self, messages: list[dict[str, Any]], result_sink: list[str]) -> None:
        check_cancelled = getattr(self.rctx, "check_cancelled", None)
        if check_cancelled is not None and await check_cancelled():
            raise ScenePlannerError("场景规划已取消。", "REQUEST_ABORTED")
        check_preempted = getattr(self.rctx, "check_preempted", None)
        if check_preempted is None:
            return
        info = await check_preempted()
        if not isinstance(info, Mapping) or not info.get("preempted"):
            return
        text = str(info.get("message") or "").strip()
        attachments = info.get("attachments") or []
        if text:
            self._append(messages, {"role": "user", "content": text, "_meta": {"preempt": True}})
            result_sink.append(text)
        consume = getattr(self.rctx, "consume_preempt", None)
        if consume is not None:
            await consume(text, len(attachments) if isinstance(attachments, list) else 0)

    async def _call_model(self, request: PlannerRequest, messages: list[dict[str, Any]], attempt: int) -> ProviderResponse:
        begin = getattr(self.rctx, "begin_llm_request", None)
        if begin is not None:
            begin()
        llm_messages = self.protocol.to_llm(messages)
        api_tools = self.protocol.api_tools(request.tools)
        started = time.monotonic()
        coro = self.provider.chat(messages=llm_messages, tools=api_tools)
        try:
            if request.timeout_sec and request.timeout_sec > 0:
                response = await asyncio.wait_for(coro, timeout=request.timeout_sec)
            else:
                response = await coro
        except asyncio.TimeoutError as error:
            raise ScenePlannerError("场景规划请求超时。", "REQUEST_TIMEOUT") from error
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ScenePlannerError(f"Scene Planner 请求失败：{error}", "PROVIDER_REQUEST_FAILED") from error
        elapsed_ms = (time.monotonic() - started) * 1000.0
        await self._record_usage(response, attempt, elapsed_ms)
        if not response.ok:
            raise ScenePlannerError(
                f"Scene Planner 请求失败：{response.error or '未知错误'}", "PROVIDER_REQUEST_FAILED",
                {"status_code": response.status_code},
            )
        return response

    async def _record_usage(self, response: ProviderResponse, attempt: int, elapsed_ms: float) -> None:
        usage = response.usage or {}
        total_usage = getattr(self.rctx, "total_usage", None)
        if isinstance(total_usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = _usage_int(usage, key)
                if value:
                    total_usage[key] = total_usage.get(key, 0) + value
        provider_name = getattr(self.provider, "name", "") or ""
        model = getattr(self.provider, "model", "") or ""
        if hasattr(self.rctx, "response_info"):
            self.rctx.response_info = {"provider": provider_name, "model": model, "notices": [], "usage": dict(usage)}
        emit = getattr(self.rctx, "emit_event", None)
        if emit is not None and _usage_int(usage, "prompt_tokens"):
            try:
                await emit("context_usage", {
                    "node_id": self.node_id,
                    "task_id": getattr(self.rctx, "task_id", ""),
                    "provider": provider_name,
                    "model": model,
                    "elapsed_ms": elapsed_ms,
                    "step": attempt,
                    "usage": dict(usage),
                })
            except Exception:  # noqa: BLE001
                logger.debug("draw agent: context_usage 事件发送失败", exc_info=True)

    async def _run_custom_tools(self, request: PlannerRequest, messages: list[dict[str, Any]],
                                calls: list[dict[str, Any]], used: int) -> int:
        for call in calls:
            name = str(call.get("name") or "")
            used += 1
            if used > request.max_custom_tool_calls:
                raise ScenePlannerError(
                    f"自定义工具调用超过 {request.max_custom_tool_calls} 次上限。", "CUSTOM_TOOL_LIMIT_EXCEEDED")
            if name not in request.custom_tool_names or self.execute_tool is None:
                payload: dict[str, Any] = {"ok": False, "error": f"tool not available: {name}"}
            else:
                args = call.get("arguments")
                try:
                    payload = await self.execute_tool(name, dict(args) if isinstance(args, Mapping) else {})
                except Exception as error:  # noqa: BLE001
                    payload = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            self._append(messages, self.protocol.tool_result_message(call, json.dumps(payload, ensure_ascii=False)))
        return used

    def _append_correction(self, messages: list[dict[str, Any]], calls: list[dict[str, Any]],
                           error: ScenePlannerError) -> dict[str, Any]:
        correction = create_correction_result(error)
        correction_json = json.dumps(correction, ensure_ascii=False)
        if calls:
            for call in calls:
                if str(call.get("name") or "") == SUBMIT_SCENE_PLAN_TOOL_NAME:
                    self._append(messages, self.protocol.tool_result_message(call, correction_json))
                else:
                    self._append(messages, self.protocol.tool_result_message(
                        call, json.dumps(SKIPPED_TOOL_NOTE, ensure_ascii=False)))
        else:
            self._append(messages, {"role": "user", "content": CORRECTION_PREFIX + correction_json})
        return {"errorCode": error.code, "errorMessage": error.message, "feedback": correction}

    # ---- main ----

    async def run(self, request: PlannerRequest) -> PlannerResult:
        messages = list(request.messages)
        attempts = 0
        custom_used = 0
        last_signature: str | None = None
        corrections: list[dict[str, Any]] = []
        preempts: list[str] = []
        while True:
            await self._poll_preempt(messages, preempts)
            attempts += 1
            response = await self._call_model(request, messages, attempts)
            calls = self.protocol.parse(response)
            self._append(messages, self.protocol.assistant_message(response, calls))
            submit_calls = [c for c in calls if str(c.get("name") or "") == SUBMIT_SCENE_PLAN_TOOL_NAME]
            if calls and not submit_calls and all(str(c.get("name") or "") in request.custom_tool_names for c in calls):
                custom_used = await self._run_custom_tools(request, messages, calls, custom_used)
                attempts -= 1  # 纯工具轮次不占纠错次数
                continue
            try:
                parsed = request.validate([
                    {"id": c.get("id"), "name": c.get("name"), "arguments": _validation_arguments(c)} for c in calls
                ])
            except ScenePlannerError as error:
                if not is_correction_error(error):
                    raise
                signature = get_correction_signature(error)
                if signature == last_signature:
                    error.details = {**(error.details or {}), "terminationReason": "repeated_error", "attempts": attempts}
                    raise
                if attempts >= request.max_attempts:
                    error.details = {**(error.details or {}), "terminationReason": "max_attempts", "attempts": attempts}
                    raise
                last_signature = signature
                record = self._append_correction(messages, calls, error)
                record["attempt"] = attempts
                corrections.append(record)
                logger.info("draw agent: 校验失败，第 %d/%d 次纠错：%s", attempts, request.max_attempts, error.message)
                continue
            return PlannerResult(
                tasks=list(parsed.get("tasks") or []),
                messages=messages,
                attempts=attempts,
                custom_tool_calls=custom_used,
                argument_repair=parsed.get("argumentRepair"),
                corrections=corrections,
                preempt_messages=preempts,
            )
