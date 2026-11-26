"""plugins/draw/agent/llm：场景 Agent 预设的规范化、供应商创建、拉取模型、测试连接与重试判定（全部离线）。"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from plugins.draw.agent import llm
from plugins.draw.planner.contract import ScenePlannerError
from providers import registry

USER = [{"role": "user", "content": "hi"}]


def _preset(**over: Any) -> dict[str, Any]:
    return llm.normalize_preset({"id": "p", "name": "P", "provider": "openai", "model": "m", "api_key": "sk-test", **over})


def _on(value: Any) -> dict[str, Any]:
    return {"enabled": True, "value": value}


# ---- 规范化 ----

def test_settings_without_presets_derive_default_from_legacy_planner():
    agent = llm.normalize_agent_settings({"presets": []}, {"tool_protocol": "text", "timeout_sec": 99})
    assert agent["current_preset_id"] == "default" and agent["planner_max_retries"] == 3
    preset = agent["presets"][0]
    assert (preset["name"], preset["tool_mode"], preset["timeout_sec"], preset["provider"]) == ("默认", "text", 99, "openai")
    assert not llm.preset_is_configured(preset)


def test_settings_keep_presets_fix_duplicate_ids_and_unknown_current():
    agent = llm.normalize_agent_settings({
        "current_preset_id": "gone", "planner_max_retries": 0,
        "presets": [{"id": "a", "name": "A", "model": "m", "api_key": "k"}, {"id": "a", "name": "B"}, "junk"],
    }, {"tool_protocol": "text"})
    ids = [p["id"] for p in agent["presets"]]
    assert len(ids) == 2 and ids[0] == "a" and ids[1] != "a"
    assert agent["current_preset_id"] == "a" and agent["planner_max_retries"] == 0
    # 已有预设时不再读旧的 planner 设置
    assert agent["presets"][0]["tool_mode"] == "native"
    assert llm.preset_is_configured(llm.current_preset(agent))


@pytest.mark.parametrize("value,expected", [
    (None, 3), ("", 3), (True, 3), (False, 3), (-1, 3), ("abc", 3), (float("nan"), 3),
    (0, 0), ("0", 0), (2.7, 2), ("4", 4), (99, 10),
])
def test_planner_max_retries_never_turns_blank_into_zero(value, expected):
    assert llm.normalize_planner_max_retries(value) == expected


def test_preset_values_are_clamped():
    preset = llm.normalize_preset({
        "name": "  ", "provider": " Anthropic ", "tool_mode": "weird", "timeout_sec": -5, "max_tokens": "abc",
        "model_list_auth": "nope", "reasoning": {"mode": "ON", "effort": "ultra"},
        "sampling": {"temperature": _on(5), "top_k": _on(0), "top_p": {"enabled": "yes", "value": "0.3"}},
    })
    assert preset["id"].startswith("agent-") and preset["name"] == "默认" and preset["provider"] == "anthropic"
    assert preset["tool_mode"] == "native" and preset["timeout_sec"] == 0 and preset["max_tokens"] == 0
    assert preset["model_list_auth"] == "auto" and preset["reasoning"] == {"mode": "on", "effort": ""}
    assert preset["sampling"]["temperature"] == {"enabled": True, "value": 2.0}
    assert preset["sampling"]["top_k"] == {"enabled": True, "value": 1}
    # 只有明确的 true 才算启用
    assert preset["sampling"]["top_p"] == {"enabled": False, "value": 0.3}
    assert preset["sampling"]["repetition_penalty"] == {"enabled": False, "value": 1.0}


# ---- 供应商 ----

def test_provider_options_cover_tokens_and_reasoning():
    assert llm.build_provider_options(_preset()) == {}
    assert llm.build_provider_options(_preset(model="gpt-4o", max_tokens=100)) == {"max_tokens": 100}
    assert llm.build_provider_options(_preset(model="gpt-5-mini", max_tokens=100)) == {"max_completion_tokens": 100}
    assert llm.build_provider_options(_preset(model="openai/o3", max_tokens=100)) == {"max_completion_tokens": 100}
    assert llm.build_provider_options(_preset(provider="anthropic", model="o3", max_tokens=100)) == {"max_tokens": 100}
    assert llm.build_provider_options(_preset(reasoning={"mode": "off"})) == {"reasoning_effort": "none"}
    assert llm.build_provider_options(_preset(reasoning={"mode": "on", "effort": "high"})) == {"reasoning_effort": "high"}
    assert llm.build_provider_options(_preset(reasoning={"mode": "on"})) == {}
    assert llm.build_provider_options(_preset(reasoning={"mode": "inherit", "effort": "high"})) == {}


def test_list_providers_puts_known_first_and_describes_each():
    providers = llm.list_providers()
    ids = [p["id"] for p in providers]
    assert ids[:3] == ["openai", "openai-responses", "anthropic"] and set(ids) == set(registry.list())
    by_id = {p["id"]: p for p in providers}
    assert by_id["openai"]["sampling"] == list(llm.SAMPLING_SPECS) and by_id["openai"]["list_auth"] == []
    assert by_id["anthropic"]["list_auth"] == ["auto", "bearer", "x-api-key"] and by_id["anthropic"]["sampling_note"]
    assert llm.provider_info("third-party")["sampling"] == ()


def test_openai_payload_carries_only_enabled_sampling_and_registry_is_untouched():
    plain = llm.create_provider(_preset(), http=None)
    assert type(plain) is registry.get("openai")
    assert "temperature" not in plain._build_payload(USER, None)

    preset = _preset(max_tokens=64, sampling={"temperature": _on(0.7), "top_k": _on(20), "top_p": {"enabled": False, "value": 0.5}})
    provider = llm.create_provider(preset, http=None)
    payload = provider._build_payload(USER, None)
    assert payload["temperature"] == 0.7 and payload["top_k"] == 20 and "top_p" not in payload
    assert payload["model"] == "m" and payload["max_tokens"] == 64
    assert provider.name == "openai" and isinstance(provider, registry.get("openai"))
    # 派生类只给这一次规划用，不进注册表
    assert type(provider) is not registry.get("openai") and type(provider) not in {registry.get(n) for n in registry.list()}


def test_unsupported_sampling_is_dropped_per_provider():
    preset = _preset(provider="deepseek", sampling={"temperature": _on(0.5), "top_k": _on(20)})
    assert llm.payload_extras(preset) == {"temperature": 0.5}
    payload = llm.create_provider(preset, http=None)._build_payload(USER, None)
    assert payload["temperature"] == 0.5 and "top_k" not in payload


def test_anthropic_sampling_only_goes_out_when_thinking_is_off():
    sampling = {"temperature": _on(0.4), "top_k": _on(30)}
    off = llm.create_provider(_preset(provider="anthropic", reasoning={"mode": "off"}, sampling=sampling), http=None)
    payload = off._build_payload(USER, tools=None)
    assert payload["thinking"] == {"type": "disabled"} and payload["temperature"] == 0.4 and payload["top_k"] == 30

    thinking = llm.create_provider(_preset(provider="anthropic", reasoning={"mode": "on", "effort": "high"}, sampling=sampling), http=None)
    payload = thinking._build_payload(USER, tools=None)
    assert payload["thinking"]["type"] != "disabled" and "temperature" not in payload and "top_k" not in payload


def test_gemini_sampling_travels_in_provider_options():
    preset = _preset(provider="gemini-aistudio", sampling={"temperature": _on(0.3), "frequency_penalty": _on(1)})
    assert llm.payload_extras(preset) == {}
    assert llm.build_provider_options(preset) == {"temperature": 0.3}
    assert type(llm.create_provider(preset, http=None)) is registry.get("gemini-aistudio")


def test_create_provider_rejects_incomplete_presets():
    for over, word in (({"provider": "nope"}, "未知的 Provider"), ({"model": ""}, "Model"), ({"api_key": ""}, "API Key")):
        with pytest.raises(ValueError) as info:
            llm.create_provider(_preset(**over), http=None)
        assert word in str(info.value)


# ---- 拉取模型 ----

def _pull(preset: dict[str, Any], handler) -> tuple[list[str], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            return await llm.pull_models(preset, client=client)

    try:
        return asyncio.run(run()), seen
    except llm.ModelListError as error:
        error.requests = seen  # type: ignore[attr-defined]
        raise


def test_pull_models_tries_versioned_path_first_and_filters():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(404, json={"error": {"message": "not here"}})
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "text-embedding-3"}, {"id": "gpt-4o"}, {"id": "tts-1"}]})

    models, seen = _pull(_preset(base_url="https://relay.test/"), handler)
    assert models == ["gpt-4o"]
    assert [str(r.url) for r in seen] == ["https://relay.test/v1/models", "https://relay.test/models"]
    assert all(r.headers["authorization"] == "Bearer sk-test" for r in seen)


def test_pull_models_uses_single_url_when_base_has_version():
    models, seen = _pull(_preset(base_url="relay.test/api/v3"), lambda r: httpx.Response(200, json=["a", "b"]))
    assert models == ["a", "b"] and [str(r.url) for r in seen] == ["https://relay.test/api/v3/models"]


def test_pull_models_stops_on_auth_failure():
    with pytest.raises(llm.ModelListError) as info:
        _pull(_preset(base_url="https://relay.test"), lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    error = info.value
    assert error.status == 401 and len(error.requests) == 1
    assert "401" in error.message and "bad key" in error.message and "sk-test" not in error.message


def test_pull_models_native_auth_on_official_hosts_and_bearer_on_relays():
    ok = lambda r: httpx.Response(200, json={"data": [{"id": "claude-x"}]})  # noqa: E731
    _, seen = _pull(_preset(provider="anthropic"), ok)
    assert str(seen[0].url) == "https://api.anthropic.com/v1/models"
    assert seen[0].headers["x-api-key"] == "sk-test" and seen[0].headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen[0].headers

    _, seen = _pull(_preset(provider="anthropic", base_url="https://relay.test"), ok)
    assert seen[0].headers["authorization"] == "Bearer sk-test" and "x-api-key" not in seen[0].headers
    _, seen = _pull(_preset(provider="anthropic", base_url="https://relay.test", model_list_auth="x-api-key"), ok)
    assert seen[0].headers["x-api-key"] == "sk-test"

    google = lambda r: httpx.Response(200, json={"models": [{"name": "models/gemini-x"}, {"name": "models/embedding-001"}]})  # noqa: E731
    models, seen = _pull(_preset(provider="gemini-aistudio"), google)
    assert models == ["gemini-x"]
    assert str(seen[0].url) == "https://generativelanguage.googleapis.com/v1beta/models"
    assert seen[0].headers["x-goog-api-key"] == "sk-test"


def test_pull_models_reports_missing_base_url_and_empty_lists():
    with pytest.raises(llm.ModelListError) as info:
        _pull(_preset(provider="third-party"), lambda r: httpx.Response(200, json={}))
    assert info.value.message == "请先填写 Base URL。"
    with pytest.raises(llm.ModelListError) as info:
        _pull(_preset(base_url="https://relay.test/v1"), lambda r: httpx.Response(200, json={"data": []}))
    assert "模型列表为空" in info.value.message
    # 全被关键词剔除时退回原列表
    assert llm.filter_models(["text-embedding-3", "tts-1"]) == ["text-embedding-3", "tts-1"]


# ---- 测试连接 ----

def _check(preset: dict[str, Any], handler) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await llm.check_connection(preset, http=client)

    return asyncio.run(run())


def test_check_connection_reports_reply_or_reason():
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})

    out = _check(_preset(base_url="https://relay.test/v1"), ok)
    assert out["ok"] is True and out["reply"] == "OK" and (out["provider"], out["model"]) == ("openai", "m")
    assert str(seen[0].url) == "https://relay.test/v1/chat/completions" and seen[0].headers["authorization"] == "Bearer sk-test"

    out = _check(_preset(base_url="https://relay.test/v1"), lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    assert out["ok"] is False and out["status_code"] == 401 and "bad key" in out["error"]

    with pytest.raises(ValueError):
        _check(_preset(model=""), ok)


# ---- 失败重试 ----

def test_retry_rules():
    retryable = [
        ScenePlannerError("x", "REQUEST_TIMEOUT"),
        ScenePlannerError("x", "PROVIDER_REQUEST_FAILED"),
        ScenePlannerError("x", "PROVIDER_REQUEST_FAILED", {"status_code": 500}),
        ScenePlannerError("x", "PROVIDER_REQUEST_FAILED", {"status_code": 429}),
        ScenePlannerError("x", "TOOL_CALL_MISSING", {"terminationReason": "max_attempts"}),
        ScenePlannerError("x", "TOOL_ARGUMENTS_SCHEMA_INVALID"),
    ]
    final = [
        None,
        ScenePlannerError("x", "REQUEST_ABORTED"),
        ScenePlannerError("x", "AGENT_PRESET_INVALID"),
        ScenePlannerError("x", "CUSTOM_TOOL_LIMIT_EXCEEDED"),
        *(ScenePlannerError("x", "PROVIDER_REQUEST_FAILED", {"status_code": s}) for s in (400, 401, 403, 404, 422)),
    ]
    assert all(llm.is_retryable_planner_error(e) for e in retryable)
    assert not any(llm.is_retryable_planner_error(e) for e in final)


def test_retry_delay_backs_off_with_cap_and_jitter():
    mid = lambda: 0.5  # noqa: E731
    assert [llm.planner_retry_delay(n, mid) for n in (1, 2, 3, 4, 5, 9)] == pytest.approx([2, 4, 8, 16, 30, 30])
    assert llm.planner_retry_delay(1, lambda: 0.0) == pytest.approx(1.6)
    assert llm.planner_retry_delay(1, lambda: 1.0) == pytest.approx(2.4)
