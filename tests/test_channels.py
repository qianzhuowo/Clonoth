"""clonoth_channels: scheme lookup, slots, fallbacks, one-time migration; replay filter of foreign reasoning."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import clonoth_channels as chn
from engine.inference.llm_call import drop_foreign_reasoning

_CFG = {
    "active_channel": "main",
    "channels": [
        {"id": "main", "name": "主渠道", "type": "gemini", "base_url": "http://gw/v1beta/", "api_key": "${MAIN_KEY}", "model": "g4"},
        {"id": "oa", "name": "备用", "type": "openai", "base_url": "http://gw/v1", "api_key": "k2", "model": "o1",
         "reasoning_effort": "low"},
    ],
    "fallbacks": [{"channel": "oa", "model": "rp/g4", "max_tokens": 9}, {"channel": "gone"}, {"channel": "main"}],
    "system_models": {"compact": {"model": "deepseek-flash"}, "vision": {"channel": "oa"}},
    "node_models": {"draw.image_gen": {"channel": "oa", "model": "draw-m"}},
}


def test_get_channel_resolves_refs_and_options(monkeypatch):
    monkeypatch.setenv("MAIN_KEY", "sk-main")
    ch = chn.get_channel(_CFG)
    assert (ch.id, ch.type, ch.base_url, ch.api_key, ch.model) == ("main", "gemini", "http://gw/v1beta", "sk-main", "g4")
    assert chn.get_channel(_CFG, "oa", model="x").options == {"reasoning_effort": "low"}
    with pytest.raises(chn.ChannelError):
        chn.get_channel(_CFG, "nope")
    with pytest.raises(chn.ChannelError, match="还没有配置"):
        chn.get_channel({})


def test_slots_nodes_and_fallbacks():
    assert chn.slot_channel(_CFG, "summary") is None
    compact = chn.slot_channel(_CFG, "compact")
    assert (compact.id, compact.model) == ("main", "deepseek-flash")  # model-only slot follows the current scheme
    assert chn.slot_channel(_CFG, "vision").model == "o1"
    assert chn.node_channel(_CFG, "system.compactor").model == "deepseek-flash"
    assert (chn.node_channel(_CFG, "draw.image_gen").id, chn.node_channel(_CFG, "draw.image_gen").model) == ("oa", "draw-m")
    assert chn.node_channel(_CFG, "qq.orchestrator").id == "main"
    chain = chn.fallback_channels(_CFG)
    assert [(c.id, c.model) for c in chain] == [("oa", "rp/g4"), ("main", "g4")]
    assert chain[0].options == {"reasoning_effort": "low", "max_tokens": 9}
    assert chn.references(_CFG, "oa") == ["备用链第 1 项", "系统模型 vision", "节点模型 draw.image_gen"]


def test_session_override_picks_scheme_then_fields():
    ch = chn.apply_session_override(_CFG, chn.get_channel(_CFG), {"channel": "oa", "model": "m2"})
    assert (ch.id, ch.type, ch.model) == ("oa", "openai", "m2")


def test_type_hints():
    assert chn.infer_type("gemini-aistudio", "http://gw/v1") == "openai"
    assert chn.infer_type("", "http://gw/v1beta") == "gemini"
    assert chn.type_mismatch("gemini", "http://gw/v1") and chn.type_mismatch("openai", "http://gw/v1beta")
    assert chn.type_mismatch("gemini", "http://gw/v1beta") == ""


def _legacy_workspace(root: Path) -> dict:
    nodes = root / "config" / "nodes"
    nodes.mkdir(parents=True)
    for name in ("draw.image_gen", "draw.novelai_planner"):
        (nodes / f"{name}.yaml").write_text(
            f"id: {name}\nprovider: openai\nmodel: ${{DRAW_TAG_MODEL}}\nbase_url: ${{DRAW_TAG_BASE_URL}}\n"
            "api_key: ${DRAW_TAG_API_KEY}\ntype: ai\n", encoding="utf-8")
    (nodes / "qq.orchestrator.yaml").write_text("id: qq.orchestrator\ntype: ai\n", encoding="utf-8")
    (root / ".env").write_text("DRAW_TAG_MODEL=ver/sap\nDRAW_TAG_BASE_URL=http://gw/v1\nDRAW_TAG_API_KEY=sk-draw\n"
                               "OPENAI_BASE_URL=http://gw/v1\nOPENAI_API_KEY=sk-oa\n", encoding="utf-8")
    draw = root / "tools" / "drawtools"
    draw.mkdir(parents=True)
    (draw / "settings.yaml").write_text(yaml.safe_dump({"agent": {"current_preset_id": "default", "presets": [
        {"id": "default", "name": "默认", "provider": "gemini", "base_url": "http://gw/v1beta",
         "api_key": "sk-main", "model": "g4", "model_list_auth": "auto"},
    ]}}, allow_unicode=True), encoding="utf-8")
    return {
        "version": 1, "provider": "gemini-aistudio",
        "gemini-aistudio": {"base_url": "http://gw/v1beta", "api_key": "sk-main", "model": "g4", "thought_summary": True},
        "openai": {"base_url": "${OPENAI_BASE_URL}", "api_key": "${OPENAI_API_KEY}", "model": ""},
        "deepseek": {"base_url": "https://api.deepseek.com", "api_key": "", "model": "deepseek-chat"},
        "fallbacks": [{"provider": "openai", "model": "rp/g4"}, {"provider": "openai", "model": "LL/g4"}],
        "system_models": {
            "compact": {"model": "deepseek-flash"},
            "summary": {"model": "deepseek-flash", "base_url": "${OPENAI_BASE_URL}", "api_key": "${OPENAI_API_KEY}"},
            "image_gpt": {"model": "gpt-image-2", "base_url": "http://img/v1", "api_key": "sk-img"},
            "vision": {"model": "LL/g4"},
        },
        "mimo_audio": {"tts_voice": "v"},
    }


def test_migrate_production_shape(tmp_path):
    data, changed = chn.migrate(_legacy_workspace(tmp_path), root=tmp_path)
    assert changed and list(data)[:3] == ["version", "active_channel", "channels"]
    by_id = {c["id"]: c for c in data["channels"]}
    main = by_id[data["active_channel"]]
    assert main["name"] == "主渠道"
    assert (main["type"], main["base_url"], main["model"], main["thought_summary"]) == ("gemini-aistudio", "http://gw/v1beta", "g4", True)
    oa = next(c for c in data["channels"] if c["base_url"] == "${OPENAI_BASE_URL}")
    assert oa["type"] == "openai" and "deepseek" not in by_id  # never-filled block is dropped
    assert data["fallbacks"] == [{"channel": oa["id"], "model": "rp/g4"}, {"channel": oa["id"], "model": "LL/g4"}]
    slots = data["system_models"]
    assert slots["compact"] == {"model": "deepseek-flash"} and slots["vision"] == {"model": "LL/g4"}
    assert slots["summary"] == {"channel": oa["id"], "model": "deepseek-flash"}
    img = by_id[slots["image_gpt"]["channel"]]
    assert (img["base_url"], img["api_key"]) == ("http://img/v1", "sk-img")
    # both draw nodes point at one 「绘图分析」 scheme; node files lose their model lines
    draw_refs = {data["node_models"][n]["channel"] for n in ("draw.image_gen", "draw.novelai_planner")}
    assert len(draw_refs) == 1
    draw = by_id[draw_refs.pop()]
    assert draw["name"] == "绘图分析" and data["node_models"]["draw.image_gen"]["model"] == "ver/sap"
    node_text = (tmp_path / "config" / "nodes" / "draw.image_gen.yaml").read_text(encoding="utf-8")
    assert node_text == "id: draw.image_gen\ntype: ai\n"
    assert "qq.orchestrator" not in data["node_models"]
    # scene-agent preset now references the main scheme
    preset = yaml.safe_load((tmp_path / "tools" / "drawtools" / "settings.yaml").read_text(encoding="utf-8"))["agent"]["presets"][0]
    assert preset == {"id": "default", "name": "默认", "model": "g4", "channel": main["id"]}
    assert data["mimo_audio"] == {"tts_voice": "v"} and "provider" not in data
    # resolving through the migrated config gives the same endpoints as before
    assert chn.slot_channel(data, "summary", root=tmp_path).api_key == "sk-oa"
    assert chn.node_channel(data, "draw.novelai_planner", root=tmp_path).api_key == "sk-draw"
    # second run is a no-op
    again, changed = chn.migrate(data, root=tmp_path)
    assert not changed and again == data


def test_migrate_empty_config_has_no_default_scheme():
    data, _ = chn.migrate({"version": 1, "openai": {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt"}})
    assert chn.channel_list(data) == [] and not data.get("active_channel")


def _assistant(channel: str | None, model: str | None) -> dict:
    meta = {"metadata": {"sig": "x"}, "reasoning": "r", "has_reasoning": True, "id": "m1"}
    if channel is not None:
        meta.update(channel=channel, model=model)
    return {"role": "assistant", "content": "hi", "reasoning_content": "thinking", "_meta": meta,
            "tool_calls": [{"id": "t", "function": {"name": "f"}, "extra_content": {"google": {"thought_signature": "s"}}}]}


def test_replay_keeps_reasoning_only_for_same_channel_and_model():
    msgs = [{"role": "user", "content": "q"}, _assistant("main", "g4"), _assistant("main", "g3"),
            _assistant("oa", "g4"), _assistant(None, None)]
    out = drop_foreign_reasoning(msgs, "main", "g4")
    assert out[0] == msgs[0] and out[1] == msgs[1]
    for clean in out[2:]:
        assert clean["content"] == "hi" and clean["tool_calls"] == [{"id": "t", "function": {"name": "f"}}]
        assert "reasoning_content" not in clean
        assert not {"metadata", "reasoning", "has_reasoning"} & set(clean["_meta"]) and clean["_meta"]["id"] == "m1"
    # without a channel nothing is trusted
    assert all("reasoning_content" not in m for m in drop_foreign_reasoning(msgs, "", "g4")[1:])


def test_gemini_replay_drops_empty_text_parts():
    from providers.gemini import _coalesce_raw_parts

    parts = [{"functionCall": {"name": "bash", "args": {}}, "thoughtSignature": "sig"}, {"text": ""}]
    assert _coalesce_raw_parts(parts) == [parts[0]]
    moved = _coalesce_raw_parts([{"text": "hi"}, {"text": "", "thoughtSignature": "s2"}])
    assert moved == [{"text": "hi", "thoughtSignature": "s2"}]
