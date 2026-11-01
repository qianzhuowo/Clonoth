from pathlib import Path

import pytest
import yaml

from clonoth_runtime import SYSTEM_NODE_MODEL_SLOTS, resolve_system_model


def _write_config(root: Path, data: dict) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for slot in ("COMPACT", "SUMMARY", "IMAGE", "IMAGE_GPT"):
        for field in ("MODEL", "BASE_URL", "API_KEY"):
            monkeypatch.delenv(f"CLONOTH_{slot}_{field}", raising=False)


def test_config_block_wins_and_supports_env_ref(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUM_KEY", "k-from-env")
    monkeypatch.setenv("CLONOTH_SUMMARY_MODEL", "env-model")
    _write_config(tmp_path, {"system_models": {"summary": {"model": "cfg-model", "api_key": "${SUM_KEY}"}}})
    assert resolve_system_model(tmp_path, "summary") == ("cfg-model", "", "k-from-env")


def test_env_then_default(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, {"system_models": {"compact": {}}})
    monkeypatch.setenv("CLONOTH_COMPACT_BASE_URL", "https://compact.example")
    out = resolve_system_model(tmp_path, "compact", default_model="main-model")
    assert out == ("main-model", "https://compact.example", "")


def test_missing_config_and_unknown_slot(tmp_path) -> None:
    assert resolve_system_model(tmp_path, "compact") == ("", "", "")
    assert resolve_system_model(tmp_path, "nope", default_model="m") == ("m", "", "")


def test_slot_mapping_covers_compactor_and_summarizer() -> None:
    assert SYSTEM_NODE_MODEL_SLOTS["system.compactor"] == "compact"
    assert SYSTEM_NODE_MODEL_SLOTS["system.turn_summarizer"] == "summary"


def test_config_store_does_not_list_system_models_as_provider(tmp_path) -> None:
    from supervisor.config_store import ConfigStore

    _write_config(tmp_path, {
        "version": 1, "provider": "openai",
        "openai": {"base_url": "https://a", "api_key": "x", "model": "m"},
        "system_models": {"compact": {"model": "c"}},
    })
    store = ConfigStore(path=tmp_path / "data" / "config.yaml")
    assert "system_models" not in store.get_providers_public().get("providers", {})
