"""Provider settings API: redacted fallback chain, key preservation, model vision, system model slots."""
from __future__ import annotations

import yaml

from supervisor.config_store import ConfigStore

_CFG = """\
version: 1
provider: openai
openai: {base_url: https://a.example/v1, api_key: sk-primary-1234, model: main-model}
fallbacks:
  - {provider: openai, model: backup-a, base_url: https://b.example/v1, api_key: sk-secret-aaaa, reasoning_effort: low}
  - {provider: openai, model: deepseek-flash, api_key: sk-secret-bbbb}
model_capabilities:
  deepseek-flash: {vision: false}
"""


def _store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_CFG, encoding="utf-8")
    return ConfigStore(path=path), path


def test_fallbacks_are_redacted_with_vision(tmp_path):
    cs, _ = _store(tmp_path)
    out = cs.get_providers_public()
    assert "sk-secret" not in str(out)
    assert out["fallbacks"][0]["api_key_redacted"] == "****aaaa"
    assert [f["vision"] for f in out["fallbacks"]] == [True, False]
    assert out["providers"]["openai"]["vision"] is True
    assert "model_capabilities" not in out["providers"]


def test_update_fallbacks_keeps_keys_and_unmanaged_fields(tmp_path):
    cs, path = _store(tmp_path)
    cs.update_fallbacks([
        {"provider": "openai", "model": "deepseek-flash", "base_url": "", "api_key": "", "source_index": 1},
        {"provider": "openai", "model": "backup-a2", "base_url": "https://b.example/v1", "api_key": "", "source_index": 0},
        {"provider": "openai", "model": "new-one", "base_url": "", "api_key": "sk-new-cccc", "source_index": None},
    ])
    chain = yaml.safe_load(path.read_text(encoding="utf-8"))["fallbacks"]
    assert chain[0] == {"provider": "openai", "model": "deepseek-flash", "api_key": "sk-secret-bbbb"}
    assert chain[1]["api_key"] == "sk-secret-aaaa" and chain[1]["reasoning_effort"] == "low"
    assert chain[1]["model"] == "backup-a2"
    assert chain[2] == {"provider": "openai", "model": "new-one", "api_key": "sk-new-cccc"}


def test_clear_api_key(tmp_path):
    cs, path = _store(tmp_path)
    cs.update_fallbacks([{"provider": "openai", "model": "backup-a", "base_url": "", "api_key": "",
                          "clear_api_key": True, "source_index": 0}])
    assert "api_key" not in yaml.safe_load(path.read_text(encoding="utf-8"))["fallbacks"][0]


def test_set_model_vision_and_system_model_slot(tmp_path):
    cs, path = _store(tmp_path)
    out = cs.set_model_vision("main-model", False)
    assert out["providers"]["openai"]["vision"] is False
    pub = cs.update_system_model("vision", model="LL/gemini-4-argon", base_url="", api_key="sk-vision-dddd")
    assert pub["model"] == "LL/gemini-4-argon" and pub["api_key_redacted"] == "****dddd"
    assert "sk-vision" not in str(pub)
    cs.update_system_model("vision", model=None, api_key=None)  # None keeps fields
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["system_models"]["vision"] == {"model": "LL/gemini-4-argon", "api_key": "sk-vision-dddd"}
    assert data["model_capabilities"]["main-model"] == {"vision": False}
