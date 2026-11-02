from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.anthropic import _parse_usage as anthropic_usage
from providers.gemini import _parse_usage as gemini_usage
from providers.openai import _extract_usage
from providers.openai_responses import _normalize_usage
from providers.base import ProviderResponse
from engine.builtin.usage_tracker import UsageTracker, PLUGIN_META
from engine.hooks import HookContext, HookRegistry
from supervisor.usage_store import DEFAULT_MODEL_PRICING, UsageStore


def test_anthropic_cache_tokens_are_part_of_prompt():
    assert anthropic_usage({"usage": {
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 30,
    }}) == {"prompt_tokens": 180, "completion_tokens": 20, "total_tokens": 200,
            "cached_prompt_tokens": 50, "cache_write_prompt_tokens": 30}
    assert anthropic_usage({"usage": {"input_tokens": None, "output_tokens": None}}) == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }


@pytest.mark.parametrize("details", [
    {"prompt_tokens_details": {"cached_tokens": 50, "cache_creation_tokens": 30},
     "completion_tokens_details": {"reasoning_tokens": 10}},
    {"prompt_cache_hit_tokens": 50, "cache_creation_input_tokens": 30, "reasoning_tokens": 10},
])
def test_openai_nested_and_flat_usage(details):
    assert _extract_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                    "total_tokens": 120, **details}}) == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "cached_prompt_tokens": 50, "cache_write_prompt_tokens": 30, "reasoning_tokens": 10,
    }


def test_responses_usage():
    assert _normalize_usage({"input_tokens": 100, "output_tokens": 20,
                             "input_tokens_details": {"cached_tokens": 50},
                             "output_tokens_details": {"reasoning_tokens": 10}}) == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "cached_prompt_tokens": 50, "reasoning_tokens": 10,
    }


def test_gemini_reasoning_is_in_completion_tokens():
    assert gemini_usage({"promptTokenCount": 100, "candidatesTokenCount": 20,
                         "thoughtsTokenCount": 10, "totalTokenCount": 130,
                         "cachedContentTokenCount": 50}) == {
        "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
        "cached_prompt_tokens": 50, "reasoning_tokens": 10,
    }


@pytest.mark.parametrize("value", [0, -1, None, "10"])
def test_optional_usage_fields_require_positive_integers(value):
    assert "reasoning_tokens" not in gemini_usage({"thoughtsTokenCount": value})
    assert "reasoning_tokens" not in _normalize_usage({"output_tokens_details": {"reasoning_tokens": value}})
    result = _extract_usage({"usage": {"prompt_tokens": 1, "cache_creation_input_tokens": value,
                                       "reasoning_tokens": value}})
    assert result == {"prompt_tokens": 1}


@pytest.fixture
def store(tmp_path):
    return UsageStore(tmp_path / "data" / "usage.db")


@pytest.mark.parametrize("model, matched", [
    ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("claude-sonnet-4-6-20250514", "claude-sonnet-4-6"),
    ("anthropic/CLAUDE-SONNET-4-6-20250514", "claude-sonnet-4-6"),
    ("gpt-5-mini-2026", "gpt-5-mini"),
    ("gpt-4.1-mini", "gpt-4.1-mini"),
    ("deepseek/deepseek-r1", "deepseek-r1"),
])
def test_default_pricing_matching(store, model, matched):
    assert store.resolve_pricing(model) == {
        **DEFAULT_MODEL_PRICING[matched], "is_override": False, "matched_key": matched,
    }


def test_override_precedence_delete_and_replace(store):
    broad = dict(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)
    narrow = dict(input=2.0, output=3.0, cache_read=0.2, cache_write=2.0)
    store.set_pricing("gpt-5", broad)
    store.set_pricing("GPT-5-mini", narrow)
    assert store.resolve_pricing("openai/gpt-5-mini-latest")["input"] == 2.0
    assert store.resolve_pricing("gpt-5.4")["input"] == 1.0
    store.set_pricing("GPT-5-mini", None)
    assert store.resolve_pricing("gpt-5-mini")["matched_key"] == "gpt-5"
    store.set_pricing("gpt-5", None)
    assert store.resolve_pricing("gpt-5-mini")["input"] == 0.25
    store.replace_overrides({"unknown": broad})
    assert store.get_pricing()["overrides"] == {"unknown": broad}
    assert store.get_pricing()["effective"]["unknown"] == broad
    assert store.get_pricing()["defaults"] == DEFAULT_MODEL_PRICING
    store.replace_overrides({})
    assert store.resolve_pricing("unknown")["input"] == 0.0


def record(store, **extra):
    payload = {
        "task_id": "task", "node_id": "chat", "provider": "anthropic",
        "model": "claude-sonnet-4-6", "elapsed_ms": 2000,
        "usage": dict(prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
                      cached_prompt_tokens=400, cache_write_prompt_tokens=100, reasoning_tokens=40),
    }
    store.record("session", {**payload, **extra})


def test_record_stats_and_dynamic_cost(store):
    record(store, llm_request_id="req")
    stats = store.get_stats("all")
    summary = stats["summary"]
    cost = (500 * 3 + 400 * 0.3 + 100 * 3.75 + 100 * 15) / 1_000_000
    assert summary == {
        "request_count": 1, "prompt_tokens": 1000, "completion_tokens": 100,
        "total_tokens": 1100, "cached_prompt_tokens": 400,
        "cache_write_prompt_tokens": 100, "reasoning_tokens": 40,
        "cache_hit_rate": 0.4, "avg_elapsed_ms": 2000.0, "avg_tps": 50.0,
        "total_cost_usd": pytest.approx(cost),
    }
    recent = stats["recent"][0]
    assert recent["session_id"] == "session"
    assert recent["task_id"] == "task"
    assert recent["created_at"]
    assert recent["cost_usd"] == pytest.approx(cost)
    assert recent["tps"] == 50.0
    assert stats["by_model"][0]["pricing"]["matched_key"] == "claude-sonnet-4-6"
    for key in ("timeseries", "by_model", "by_provider", "by_node"):
        assert stats[key][0]["cost_usd"] == pytest.approx(cost)
    store.set_pricing("claude-sonnet-4-6", dict(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0))
    assert store.get_stats("all")["summary"]["total_cost_usd"] == pytest.approx(0.00084)
    store.set_pricing("claude-sonnet-4-6", None)
    assert store.get_stats("all")["summary"]["total_cost_usd"] == pytest.approx(cost)
    # Reopen the file, including its pricing overrides, rather than relying on memory.
    assert UsageStore(store.path).get_stats("all")["summary"]["request_count"] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT llm_request_id FROM llm_usage_logs").fetchone()[0] == "req"


def test_local_time_ranges_and_empty_buckets(store):
    tz = timezone(timedelta(minutes=480))
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    record(store, ts=midnight.timestamp())
    record(store, ts=(midnight - timedelta(seconds=1)).timestamp(), node_id="system.compactor")
    record(store, ts=(midnight - timedelta(days=8)).timestamp())
    today = store.get_stats("today", 480)
    assert today["summary"]["request_count"] == 1
    assert len(today["timeseries"]) == 24
    assert today["timeseries"][0]["bucket"] == "00:00"
    assert today["timeseries"][0]["request_count"] == 1
    assert today["timeseries"][1]["request_count"] == 0
    seven = store.get_stats("7d", 480)
    assert len(seven["timeseries"]) == 7
    assert seven["summary"]["request_count"] == 2
    assert len(store.get_stats("30d")["timeseries"]) == 30
    all_stats = store.get_stats("all")
    assert all_stats["summary"]["request_count"] == 3
    assert all_stats["recent"][1]["is_system_task"] == 1


def test_recent_limit_model_sorting_and_zero_elapsed(store):
    for _ in range(51):
        record(store, model="unknown", elapsed_ms=0)
    record(store)
    stats = store.get_stats("all")
    assert len(stats["recent"]) == 50
    assert stats["recent"][0]["model"] == "claude-sonnet-4-6"
    assert stats["by_model"][0]["model"] == "claude-sonnet-4-6"
    assert stats["by_model"][1]["avg_tps"] == 0.0
    assert stats["summary"]["request_count"] == 52


def test_empty_stats_and_invalid_pricing_are_atomic(store):
    assert store.get_stats("all")["summary"]["total_cost_usd"] == 0.0
    assert store.get_stats("all")["timeseries"] == []
    good = dict(input=1, output=2, cache_read=0, cache_write=1)
    store.set_pricing("test", good)
    with pytest.raises(ValueError):
        store.replace_overrides({"valid": good, "invalid": {**good, "input": -1}})
    assert store.get_overrides() == {"test": good}
    with pytest.raises(ValueError):
        store.set_pricing("bad", {**good, "input": float("nan")})


def test_usage_tracker_runs_after_fallback_response_replacement():
    registry = HookRegistry()
    response = ProviderResponse(ok=True, usage=dict(prompt_tokens=10, completion_tokens=5,
        total_tokens=15, cached_prompt_tokens=3, cache_write_prompt_tokens=2, reasoning_tokens=4))

    async def fallback(ctx):
        ctx.response = response

    tracker = UsageTracker()
    registry.register("after_llm_call", fallback, priority=-20)
    registry.register("after_llm_call", tracker.handle)
    rctx = SimpleNamespace(total_usage={})
    ctx = HookContext(messages=[], tools=[], node=None, provider=None,
                      response=ProviderResponse(ok=False), rctx=rctx)
    asyncio.run(registry.afire("after_llm_call", ctx))
    assert rctx.total_usage == response.usage
