"""Channel settings: redacted listing with roles, key preservation, switches, fallback order, vision."""
from __future__ import annotations

import pytest
import yaml

from supervisor.config_store import ConfigStore

_CFG = """\
version: 1
channels:
  - {id: main, name: 主模型, type: openai, base_url: https://a.example/v1, api_key: sk-primary-1234, model: main-model}
  - {id: fallback-1, name: 备用, type: openai, base_url: https://b.example/v1, api_key: sk-secret-aaaa, model: backup-a}
  - {id: fallback-2, name: 备用 2, type: openai, base_url: https://b.example/v1, api_key: sk-secret-bbbb, model: deepseek-flash}
  - {id: draw, name: 绘图分析, type: openai, base_url: https://d.example/v1, api_key: sk-draw-cccc, model: d}
fallbacks:
  - {channel: fallback-1, reasoning_effort: low}
  - {channel: fallback-2}
node_models:
  draw.image_gen: {channel: draw}
model_capabilities:
  deepseek-flash: {vision: false}
"""


def _store(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_CFG, encoding="utf-8")
    return ConfigStore(path=path), path


def _saved(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_channels_are_redacted_with_roles_and_vision(tmp_path):
    cs, _ = _store(tmp_path)
    out = cs.get_channels_public()
    assert "sk-secret" not in str(out) and "sk-primary" not in str(out)
    by_id = {c["id"]: c for c in out["channels"]}
    assert by_id["main"]["api_key_redacted"] == "****1234"
    assert [by_id[i]["role"] for i in ("main", "fallback-1", "draw")] == ["main", "fallback", "other"]
    assert out["fallbacks"] == ["fallback-1", "fallback-2"]
    assert by_id["fallback-2"]["vision"] is False and by_id["main"]["vision"] is True
    assert out["roles"]["image_gpt"] == "GPT 生图"


def test_upsert_keeps_key_unless_cleared_and_switches_roles(tmp_path):
    cs, path = _store(tmp_path)
    cs.upsert_channel("fallback-1", base_url="https://c.example/v1", api_key="")
    assert _saved(path)["channels"][1]["api_key"] == "sk-secret-aaaa"
    cs.upsert_channel("fallback-1", clear_api_key=True)
    assert _saved(path)["channels"][1]["api_key"] == ""
    with pytest.raises(ValueError):
        cs.upsert_channel("new-one", name="x")  # a new channel needs a type
    out = cs.upsert_channel("image_gpt", type="openai", base_url="https://i.example/v1", model="gpt-image-2", enabled=False)
    img = next(c for c in out["channels"] if c["id"] == "image_gpt")
    assert (img["name"], img["enabled"], img["role"]) == ("GPT 生图", False, "image_gpt")
    cs.upsert_channel("image_gpt", enabled=True)
    assert "enabled" not in next(c for c in _saved(path)["channels"] if c["id"] == "image_gpt")
    with pytest.raises(ValueError, match="不能关闭"):
        cs.upsert_channel("main", enabled=False)


def test_delete_guards_main_and_node_choices_and_drops_fallback(tmp_path):
    cs, path = _store(tmp_path)
    with pytest.raises(ValueError, match="主模型"):
        cs.delete_channel("main")
    with pytest.raises(ValueError, match="正在使用"):
        cs.delete_channel("draw")
    cs.delete_channel("fallback-1")
    data = _saved(path)
    assert data["fallbacks"] == [{"channel": "fallback-2"}]
    assert "fallback-1" not in [c["id"] for c in data["channels"]]


def test_update_fallbacks_reorders_keeps_options_and_rejects_roles(tmp_path):
    cs, path = _store(tmp_path)
    cs.update_fallbacks(["fallback-2", "fallback-1"])
    assert _saved(path)["fallbacks"] == [{"channel": "fallback-2"}, {"channel": "fallback-1", "reasoning_effort": "low"}]
    with pytest.raises(ValueError, match="不存在"):
        cs.update_fallbacks(["gone"])
    with pytest.raises(ValueError, match="主模型"):
        cs.update_fallbacks(["main"])


def test_main_model_vision_and_node_models(tmp_path):
    cs, path = _store(tmp_path)
    assert cs.set_main_model("main-b") == "main"
    cs.set_model_vision("main-b", False)
    cs.update_node_model("draw.novelai_planner", channel="fallback-1", model="m")
    data = _saved(path)
    assert data["channels"][0]["model"] == "main-b" and data["model_capabilities"]["main-b"] == {"vision": False}
    assert data["node_models"]["draw.novelai_planner"] == {"channel": "fallback-1", "model": "m"}
    cs.update_node_model("draw.novelai_planner", channel="", model="")
    assert "draw.novelai_planner" not in _saved(path)["node_models"]
    with pytest.raises(ValueError):
        cs.update_node_model("x", channel="gone")
    assert cs.get_public().main_model == "main-b"
