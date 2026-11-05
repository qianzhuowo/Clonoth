"""fallback_provider resolves "${ENV}" references like the main provider does."""
from __future__ import annotations

from engine.builtin.fallback_provider import _resolve_fallback_entry


def test_env_refs_in_fallback_entry_are_resolved(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-gw")
    entry = _resolve_fallback_entry(
        {"provider": "openai", "base_url": "${OPENAI_BASE_URL}", "api_key": "${OPENAI_API_KEY}", "model": "deepseek-flash"},
        {"openai": {"base_url": "http://other/v1", "api_key": "sk-main", "model": "m"}},
    )
    assert entry["base_url"] == "http://127.0.0.1:8000/v1"
    assert entry["api_key"] == "sk-gw"
    assert entry["model"] == "deepseek-flash"


def test_unset_env_ref_falls_back_to_provider_block(monkeypatch):
    monkeypatch.delenv("NOPE_BASE_URL", raising=False)
    monkeypatch.setenv("MAIN_KEY", "sk-main")
    entry = _resolve_fallback_entry(
        {"provider": "openai", "base_url": "${NOPE_BASE_URL}", "model": "x"},
        {"openai": {"base_url": "http://gw/v1", "api_key": "${MAIN_KEY}"}},
    )
    assert entry["base_url"] == "http://gw/v1"
    assert entry["api_key"] == "sk-main"
