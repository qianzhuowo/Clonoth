"""Channel scheme settings: redacted listing, key preservation, fallback chain, vision, slot references."""
from __future__ import annotations

import pytest
import yaml

from supervisor.config_store import ConfigStore

_CFG = """\
version: 1
active_channel: main
channels:
  - {id: main, name: 主渠道, type: openai, base_url: https://a.example/v1, api_key: sk-primary-1234, model: main-model}
  - {id: backup, name: 备用, type: openai, base_url: https://b.example/v1, api_key: sk-secret-aaaa, model: backup-a}
fallbacks:
  - {channel: backup, reasoning_effort: low}
  - {channel: main, model: deepseek-flash}
model_capabilities:
  deepseek-flash: {vision: false}
"""


def _store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_CFG, encoding="utf-8")
    return ConfigStore(path=path), path


def _saved(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_channels_are_redacted_with_vision(tmp_path):
    cs, _ = _store(tmp_path)
    out = cs.get_channels_public()
    assert "sk-secret" not in str(out) and "sk-primary" not in str(out)
    assert [c["api_key_redacted"] for c in out["channels"]] == ["****1234", "****aaaa"]
    assert [f["effective_model"] for f in out["fallbacks"]] == ["backup-a", "deepseek-flash"]
    assert [f["vision"] for f in out["fallbacks"]] == [True, False]
    assert out["active_channel"] == "main"


def test_upsert_keeps_key_unless_cleared(tmp_path):
    cs, path = _store(tmp_path)
    cs.upsert_channel("backup", base_url="https://c.example/v1", api_key="")
    assert _saved(path)["channels"][1]["api_key"] == "sk-secret-aaaa"
    cs.upsert_channel("backup", clear_api_key=True)
    assert _saved(path)["channels"][1]["api_key"] == ""
    with pytest.raises(ValueError):
        cs.upsert_channel("new-one", name="x")  # a new scheme needs a type


def test_delete_refuses_referenced_scheme(tmp_path):
    cs, path = _store(tmp_path)
    with pytest.raises(ValueError, match="正在使用"):
        cs.delete_channel("backup")
    cs.update_fallbacks([{"channel": "main", "model": "x", "source_index": 1}])
    cs.delete_channel("backup")
    assert [c["id"] for c in _saved(path)["channels"]] == ["main"]


def test_update_fallbacks_keeps_options_and_rejects_unknown(tmp_path):
    cs, path = _store(tmp_path)
    cs.update_fallbacks([
        {"channel": "main", "model": "deepseek-flash", "source_index": 1},
        {"channel": "backup", "model": "backup-a2", "source_index": 0},
    ])
    chain = _saved(path)["fallbacks"]
    assert chain[0] == {"channel": "main", "model": "deepseek-flash"}
    assert chain[1] == {"channel": "backup", "reasoning_effort": "low", "model": "backup-a2"}
    with pytest.raises(ValueError, match="不存在"):
        cs.update_fallbacks([{"channel": "gone"}])


def test_active_channel_model_vision_and_slots(tmp_path):
    cs, path = _store(tmp_path)
    cs.set_active_channel("backup")
    assert cs.set_active_model("backup-b") == "backup"
    cs.set_model_vision("backup-b", False)
    pub = cs.update_system_model("vision", channel="main", model="LL/gemini-4-argon")
    assert pub == {"slot": "vision", "channel": "main", "model": "LL/gemini-4-argon"}
    cs.update_system_model("vision", channel=None, model=None)  # None keeps fields
    cs.update_node_model("draw.image_gen", channel="backup")
    data = _saved(path)
    assert data["active_channel"] == "backup" and data["channels"][1]["model"] == "backup-b"
    assert data["system_models"]["vision"] == {"channel": "main", "model": "LL/gemini-4-argon"}
    assert data["node_models"]["draw.image_gen"] == {"channel": "backup"}
    assert data["model_capabilities"]["backup-b"] == {"vision": False}
    cs.update_system_model("vision", channel="", model="")  # empty = back to the current scheme
    assert "vision" not in _saved(path)["system_models"]
    with pytest.raises(ValueError):
        cs.update_system_model("vision", channel="gone")
