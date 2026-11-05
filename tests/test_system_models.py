from pathlib import Path

import pytest
import yaml

from clonoth_runtime import SYSTEM_NODE_MODEL_SLOTS, SystemModel, resolve_system_model


def _write_config(root: Path, data: dict) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for slot in ("COMPACT", "SUMMARY", "MEMORY", "IMAGE", "IMAGE_GPT", "VISION"):
        for field in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
            monkeypatch.delenv(f"CLONOTH_{slot}_{field}", raising=False)


def test_config_block_wins_and_supports_env_ref(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUM_KEY", "k-from-env")
    monkeypatch.setenv("CLONOTH_SUMMARY_MODEL", "env-model")
    _write_config(tmp_path, {"system_models": {"summary": {"model": "cfg-model", "api_key": "${SUM_KEY}"}}})
    assert resolve_system_model(tmp_path, "summary") == SystemModel(model="cfg-model", api_key="k-from-env")


def test_env_then_default(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, {"system_models": {"compact": {}}})
    monkeypatch.setenv("CLONOTH_COMPACT_BASE_URL", "https://compact.example")
    monkeypatch.setenv("CLONOTH_COMPACT_PROVIDER", "DeepSeek")
    out = resolve_system_model(tmp_path, "compact", default_model="main-model")
    assert out == SystemModel(provider="deepseek", model="main-model", base_url="https://compact.example")


def test_missing_config_and_unknown_slot(tmp_path) -> None:
    assert resolve_system_model(tmp_path, "compact") == SystemModel()
    assert resolve_system_model(tmp_path, "nope", default_model="m") == SystemModel(model="m")


def test_slot_mapping_covers_system_nodes() -> None:
    assert SYSTEM_NODE_MODEL_SLOTS == {
        "system.compactor": "compact",
        "system.turn_summarizer": "summary",
        "system.memory_extractor": "memory",
    }


def test_builtin_system_nodes_do_not_pin_a_channel() -> None:
    from engine.node import _node_from_dict

    root = Path(__file__).resolve().parent.parent
    nodes_dir = root / "engine" / "builtin" / "nodes"
    for node_id in SYSTEM_NODE_MODEL_SLOTS:
        data = yaml.safe_load((nodes_dir / f"{node_id}.yaml").read_text(encoding="utf-8"))
        node = _node_from_dict(root, node_id, data)
        assert (node.provider, node.model, node.base_url, node.api_key) == ("", "", "", ""), node_id


def test_config_store_does_not_list_system_models_as_provider(tmp_path) -> None:
    from supervisor.config_store import ConfigStore

    _write_config(tmp_path, {
        "version": 1, "provider": "openai",
        "openai": {"base_url": "https://a", "api_key": "x", "model": "m"},
        "system_models": {"compact": {"model": "c"}},
    })
    store = ConfigStore(path=tmp_path / "data" / "config.yaml")
    assert "system_models" not in store.get_providers_public().get("providers", {})


def test_config_store_round_trips_slot_provider(tmp_path) -> None:
    from supervisor.config_store import ConfigStore

    _write_config(tmp_path, {"version": 1, "provider": "openai", "openai": {"base_url": "https://a", "model": "m"}})
    store = ConfigStore(path=tmp_path / "data" / "config.yaml")
    out = store.update_system_model("compact", provider="openai", model="deepseek-flash")
    assert (out["provider"], out["model"]) == ("openai", "deepseek-flash")
    out = store.update_system_model("compact", provider="")
    assert (out["provider"], out["model"]) == ("", "deepseek-flash")


def _resolved_compactor(tmp_path, config: dict):
    from engine.model import resolve_provider
    from engine.node import Node
    from engine.runner import _apply_system_model_slot

    _write_config(tmp_path, config)
    pinned = Node(id="system.compactor", type="ai", model="old", provider="gemini-vertex")
    providers = {k: v for k, v in config.items() if isinstance(v, dict) and "base_url" in v}
    node = _apply_system_model_slot(tmp_path, pinned)
    return resolve_provider(tmp_path, node, "main", provider_configs=providers,
                            default_provider_type=config.get("provider", "openai"))


def test_model_only_slot_follows_global_channel(tmp_path) -> None:
    rp = _resolved_compactor(tmp_path, {
        "provider": "openai",
        "openai": {"base_url": "http://gw/v1", "api_key": "gw-key", "model": "main"},
        "system_models": {"compact": {"model": "deepseek-flash"}},
    })
    assert (rp.provider_type, rp.model, rp.base_url, rp.api_key) == ("openai", "deepseek-flash", "http://gw/v1", "gw-key")


def test_slot_provider_uses_that_channel_section(tmp_path) -> None:
    rp = _resolved_compactor(tmp_path, {
        "provider": "openai",
        "openai": {"base_url": "http://gw/v1", "api_key": "gw-key", "model": "main"},
        "deepseek": {"base_url": "https://ds", "api_key": "ds-key", "model": "ds-chat"},
        "system_models": {"compact": {"provider": "deepseek"}},
    })
    assert (rp.provider_type, rp.model, rp.base_url, rp.api_key) == ("deepseek", "ds-chat", "https://ds", "ds-key")


def test_unconfigured_slot_keeps_node(tmp_path) -> None:
    rp = _resolved_compactor(tmp_path, {"provider": "openai", "openai": {"base_url": "http://gw/v1", "model": "main"}})
    assert (rp.provider_type, rp.model) == ("gemini-vertex", "old")