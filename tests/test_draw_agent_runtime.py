"""plugins/draw/agent 运行时与工具协议测试：假 provider 驱动规划循环。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.draw.agent.protocol import create_tool_protocol, normalize_stored_tool_calls
from plugins.draw.agent.runtime import CORRECTION_PREFIX, PlannerRequest, PlannerRuntime
from plugins.draw.planner import contract
from plugins.draw.planner.assembly import build_planner_messages
from plugins.draw.planner.tool import create_submit_scene_plan_tool
from providers.base import ProviderResponse, ToolCall

PRESENT = [{"name": "艾丽丝", "aliases": ["小艾"], "type": "girl", "appearance": "blonde hair"}]
GOOD_PLAN = {"images": [{"index": 1, "title": "t", "orientation": "portrait", "scene": "indoors",
                         "characters": [{"name": "小艾", "costume": "dress", "action": "smile"}]}]}


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def chat(self, *, messages, tools=None):
        self.requests.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("no more fake responses")
        return self.responses.pop(0)


def _native(calls: list[tuple[str, dict]] | None = None, text: str = "", usage: dict | None = None) -> ProviderResponse:
    return ProviderResponse(
        ok=True, text=text,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls or [])],
        usage=usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def _rctx(preempts: list[dict] | None = None):
    events: list[tuple[str, dict]] = []
    consumed: list[tuple[str, int]] = []
    queue = list(preempts or [])

    async def check_preempted():
        return queue.pop(0) if queue else {"preempted": False, "message": "", "attachments": []}

    async def consume(message="", attachment_count=0):
        consumed.append((message, attachment_count))

    async def emit(event_type, payload):
        events.append((event_type, payload))

    async def cancelled():
        return False

    return SimpleNamespace(
        total_usage={}, response_info={}, task_id="t1",
        begin_llm_request=lambda: "req",
        check_preempted=check_preempted, consume_preempt=consume, emit_event=emit, check_cancelled=cancelled,
        _events=events, _consumed=consumed,
    )


def _request(messages: list[dict] | None = None, **kw) -> PlannerRequest:
    tool = create_submit_scene_plan_tool()
    search = {"type": "function", "function": {"name": "search_web", "description": "s",
                                               "parameters": {"type": "object", "properties": {}}}}

    def validate(calls):
        return contract.parse_submitted_scene_plan(calls, present_characters=PRESENT, max_plan_images=4)

    return PlannerRequest(
        messages=messages or [{"role": "system", "content": "s"}, {"role": "user", "content": "画"}],
        tools=[tool, search], validate=validate, custom_tool_names=frozenset({"search_web"}), **kw)


def _run(provider, rctx, request, protocol="native", execute_tool=None, sink=None):
    rt = PlannerRuntime(provider=provider, protocol=create_tool_protocol(protocol), rctx=rctx,
                        node_id="draw.novelai_planner", execute_tool=execute_tool, on_message=sink)
    return asyncio.run(rt.run(request))


def test_native_success_records_usage_and_history():
    provider = FakeProvider([_native([("submit_scene_plan", GOOD_PLAN)], text="ok")])
    rctx = _rctx()
    seen: list[dict] = []
    result = _run(provider, rctx, _request(), sink=seen.append)
    assert [t["chars"][0]["name"] for t in result.tasks] == ["艾丽丝"]
    assert result.attempts == 1 and result.corrections == []
    assert rctx.total_usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert rctx._events[0][0] == "context_usage" and rctx._events[0][1]["node_id"] == "draw.novelai_planner"
    assert seen[0]["role"] == "assistant" and seen[0]["tool_calls"][0]["name"] == "submit_scene_plan"
    # 原生协议：tools 随请求发送，tool_calls 以 API 形状发出
    assert provider.requests[0]["tools"][0]["function"]["name"] == "submit_scene_plan"


def test_custom_tool_round_then_submit_and_correction_loop():
    bad_plan = {"images": [{"index": 1, "scene": "x", "characters": [{"name": "路人", "costume": "", "action": "a"}]}]}
    provider = FakeProvider([
        _native([("search_web", {"query": "alice"})]),
        _native([("submit_scene_plan", bad_plan)]),
        _native([("submit_scene_plan", GOOD_PLAN)]),
    ])
    executed: list[tuple[str, dict]] = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True, "data": {"hits": 1}}

    result = _run(provider, _rctx(), _request(), execute_tool=execute)
    assert executed == [("search_web", {"query": "alice"})]
    assert result.custom_tool_calls == 1
    assert result.attempts == 2 and len(result.corrections) == 1
    assert result.corrections[0]["errorCode"] == "TOOL_ARGUMENTS_SCHEMA_INVALID"
    # 搜索结果作为 tool 返回接回；纠错作为 submit_scene_plan 的 tool 返回接回
    msgs = result.messages
    search_result = next(m for m in msgs if m.get("role") == "tool" and m.get("name") == "search_web")
    assert json.loads(search_result["content"])["data"]["hits"] == 1
    correction = next(m for m in msgs if m.get("role") == "tool" and m.get("name") == "submit_scene_plan")
    assert json.loads(correction["content"])["ok"] is False
    # 第三次请求里模型能看到纠错
    third = provider.requests[2]["messages"]
    assert any(m.get("role") == "tool" and "未知角色必须填写外貌" in m.get("content", "") for m in third)


def test_repeated_signature_terminates_early():
    provider = FakeProvider([_native(text="我不想调用"), _native(text="还是不调")])
    with pytest.raises(contract.ScenePlannerError) as info:
        _run(provider, _rctx(), _request())
    assert info.value.code == "TOOL_CALL_MISSING"
    assert info.value.details["terminationReason"] == "repeated_error"
    # 无 tool_calls 时纠错以 user 文本接回
    second = provider.requests[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"].startswith(CORRECTION_PREFIX)


def test_max_attempts_terminates():
    plans = [
        _native([("submit_scene_plan", {"images": []})]),
        _native(text="no call"),
        _native([("submit_scene_plan", {"images": "x"})]),
    ]
    provider = FakeProvider(plans)
    with pytest.raises(contract.ScenePlannerError) as info:
        _run(provider, _rctx(), _request(max_attempts=3))
    assert info.value.details["terminationReason"] == "max_attempts"
    assert info.value.details["attempts"] == 3


def test_preempt_message_is_injected_and_consumed():
    provider = FakeProvider([_native([("submit_scene_plan", GOOD_PLAN)])])
    rctx = _rctx([{"preempted": True, "message": "改成横图", "attachments": [{"path": "a"}]}])
    result = _run(provider, rctx, _request())
    assert result.preempt_messages == ["改成横图"]
    assert rctx._consumed == [("改成横图", 1)]
    sent = provider.requests[0]["messages"]
    assert sent[-1] == {"role": "user", "content": "改成横图"}


def test_provider_failure_and_timeout_map_to_codes():
    provider = FakeProvider([ProviderResponse(ok=False, error="boom", status_code=500)])
    with pytest.raises(contract.ScenePlannerError) as info:
        _run(provider, _rctx(), _request())
    assert info.value.code == "PROVIDER_REQUEST_FAILED" and info.value.details["status_code"] == 500

    class Slow(FakeProvider):
        async def chat(self, *, messages, tools=None):
            await asyncio.sleep(0.2)
            return _native()

    with pytest.raises(contract.ScenePlannerError) as info:
        _run(Slow([]), _rctx(), _request(timeout_sec=0.01))
    assert info.value.code == "REQUEST_TIMEOUT"


def test_text_protocol_parses_json_block_and_injects_tool_text():
    proto = create_tool_protocol("text")
    tool = create_submit_scene_plan_tool()
    tool_text = proto.tool_protocol_text([tool])
    assert "<<<TOOL_CALL>>>" in tool_text and "submit_scene_plan" in tool_text
    messages = build_planner_messages(None, {"opening": "开", "frame": "f", "content": "c", "guide": "", "rules": "", "limits": "l"},
                                      tool_protocol_text=tool_text)
    llm = proto.to_llm(messages)
    # 文本协议下预置的 tool 返回变成 user 文本，assistant 的 tool_calls 变成 JSON 块
    assert all(m["role"] != "tool" for m in llm)
    assert any("<<<TOOL_CALL>>>" in m["content"] and "read_scene_materials" in m["content"] for m in llm if m["role"] == "assistant")
    block = "<<<TOOL_CALL>>>\n" + json.dumps({"name": "submit_scene_plan", "arguments": GOOD_PLAN}, ensure_ascii=False) + "\n<<<END_TOOL_CALL>>>"
    provider = FakeProvider([ProviderResponse(ok=True, text="规划如下\n" + block, usage={"prompt_tokens": 1})])
    result = _run(provider, _rctx(), _request(messages=messages), protocol="text")
    assert result.tasks[0]["chars"][0]["name"] == "艾丽丝"
    assert provider.requests[0]["tools"] is None
    assert result.messages[-1]["content"] == "规划如下"


def test_normalize_stored_tool_calls_from_api_shape():
    msg = normalize_stored_tool_calls({"role": "assistant", "content": "", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}},
        {"id": "y", "type": "function", "function": {"name": "g", "arguments": "{broken"}},
    ]})
    assert msg["tool_calls"][0] == {"id": "x", "name": "f", "arguments": {"a": 1}}
    assert msg["tool_calls"][1]["arguments"] == {"_raw": "{broken"}
