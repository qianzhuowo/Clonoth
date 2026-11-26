"""clonoth_channels: role channels, fallbacks, switches, migration; replay filter of foreign reasoning."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import clonoth_channels as chn
from engine.inference.llm_call import drop_foreign_reasoning

_CFG = {
    "channels": [
        {"id": "main", "name": "主模型", "type": "gemini", "base_url": "http://gw/v1beta/", "api_key": "${MAIN_KEY}", "model": "g4"},
        {"id": "vision", "type": "gemini", "base_url": "http://gw/v1beta", "api_key": "kv", "model": "see"},
        {"id": "compact", "type": "openai", "base_url": "http://gw/v1", "api_key": "kc", "model": "flash", "enabled": False},
        {"id": "image", "type": "gemini", "base_url": "http://gw/v1beta", "api_key": "ki", "model": "img", "enabled": False},
        {"id": "fb", "type": "openai", "base_url": "http://gw/v1", "api_key": "k2", "model": "rp/g4", "reasoning_effort": "low"},
        {"id": "fb-off", "type": "openai", "base_url": "http://gw/v1", "api_key": "k3", "model": "x", "enabled": False},
        {"id": "draw", "type": "openai", "base_url": "http://gw/v1", "api_key": "kd", "model": "d"},
    ],
    "fallbacks": [{"channel": "fb", "max_tokens": 9}, {"channel": "gone"}, {"channel": "fb-off"}],
    "node_models": {"draw.image_gen": {"channel": "draw", "model": "draw-m"}},
}


def test_get_channel_resolves_refs_and_options(monkeypatch):
    monkeypatch.setenv("MAIN_KEY", "sk-main")
    ch = chn.get_channel(_CFG)
    assert (ch.id, ch.type, ch.base_url, ch.api_key, ch.model) == ("main", "gemini", "http://gw/v1beta", "sk-main", "g4")
    assert chn.get_channel(_CFG, "fb", model="x").options == {"reasoning_effort": "low"}
    with pytest.raises(chn.ChannelError):
        chn.get_channel(_CFG, "nope")
    with pytest.raises(chn.ChannelError, match="主模型"):
        chn.get_channel({})


def test_roles_switches_nodes_and_fallbacks():
    assert chn.slot_channel(_CFG, "vision").model == "see"
    assert chn.slot_channel(_CFG, "summary") is None          # no channel: system node follows main
    assert chn.slot_channel(_CFG, "compact") is None          # switched off: same
    assert chn.slot_channel(_CFG, "image") is None            # switched off: the image tool is unavailable
    assert chn.node_channel(_CFG, "system.compactor").id == "main"
    assert (chn.node_channel(_CFG, "draw.image_gen").id, chn.node_channel(_CFG, "draw.image_gen").model) == ("draw", "draw-m")
    assert chn.node_channel(_CFG, "qq.orchestrator").id == "main"
    chain = chn.fallback_channels(_CFG)
    assert [(c.id, c.model) for c in chain] == [("fb", "rp/g4")]
    assert chain[0].options == {"reasoning_effort": "low", "max_tokens": 9}
    assert [chn.role_of(_CFG, c) for c in ("main", "image", "fb", "draw")] == ["main", "image", "fallback", "other"]
    assert chn.references(_CFG, "draw") == ["节点模型 draw.image_gen"]


def test_session_override_picks_channel_then_fields():
    ch = chn.apply_session_override(_CFG, chn.get_channel(_CFG), {"channel": "fb", "model": "m2"})
    assert (ch.id, ch.type, ch.model) == ("fb", "openai", "m2")


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


def test_migrate_gives_every_role_its_own_channel(tmp_path):
    data, changed = chn.migrate(_legacy_workspace(tmp_path), root=tmp_path)
    assert changed and list(data)[:2] == ["version", "channels"]
    assert not {"active_channel", "system_models", "provider"} & set(data)
    by_id = {c["id"]: c for c in data["channels"]}
    main = by_id["main"]
    assert (main["name"], main["type"], main["base_url"], main["model"], main["thought_summary"]) == (
        "主模型", "gemini-aistudio", "http://gw/v1beta", "g4", True)
    assert "deepseek" not in by_id  # never-filled block is dropped
    # roles that pointed at a shared scheme got their own copy with their model
    assert (by_id["compact"]["base_url"], by_id["compact"]["model"]) == ("http://gw/v1beta", "deepseek-flash")
    assert (by_id["vision"]["base_url"], by_id["vision"]["model"]) == ("http://gw/v1beta", "LL/g4")
    assert (by_id["summary"]["base_url"], by_id["summary"]["model"]) == ("${OPENAI_BASE_URL}", "deepseek-flash")
    assert (by_id["image_gpt"]["base_url"], by_id["image_gpt"]["model"]) == ("http://img/v1", "gpt-image-2")
    # each fallback is its own channel; the first one takes over the scheme that only served it
    first, second = (by_id[e["channel"]] for e in data["fallbacks"])
    assert data["fallbacks"] == [{"channel": first["id"]}, {"channel": second["id"]}]
    assert (first["base_url"], first["model"]) == ("${OPENAI_BASE_URL}", "rp/g4")
    assert (second["base_url"], second["model"], second["name"]) == ("${OPENAI_BASE_URL}", "LL/g4", "备用链路 2")
    # both draw nodes point at one 「绘图分析」 channel; node files lose their model lines
    draw_refs = {data["node_models"][n]["channel"] for n in ("draw.image_gen", "draw.novelai_planner")}
    assert len(draw_refs) == 1 and by_id[draw_refs.pop()]["name"] == "绘图分析"
    assert data["node_models"]["draw.image_gen"]["model"] == "ver/sap"
    node_text = (tmp_path / "config" / "nodes" / "draw.image_gen.yaml").read_text(encoding="utf-8")
    assert node_text == "id: draw.image_gen\ntype: ai\n"
    # scene-agent preset follows the main channel's new id
    preset = yaml.safe_load((tmp_path / "tools" / "drawtools" / "settings.yaml").read_text(encoding="utf-8"))["agent"]["presets"][0]
    assert preset == {"id": "default", "name": "默认", "model": "g4", "channel": "main"}
    assert data["mimo_audio"] == {"tts_voice": "v"}
    # resolving through the migrated config gives the same endpoints as before
    assert chn.slot_channel(data, "summary", root=tmp_path).api_key == "sk-oa"
    assert chn.node_channel(data, "draw.novelai_planner", root=tmp_path).api_key == "sk-draw"
    assert [c.model for c in chn.fallback_channels(data, root=tmp_path)] == ["rp/g4", "LL/g4"]
    again, changed = chn.migrate(data, root=tmp_path)
    assert not changed and again == data


def test_migrate_active_channel_layout(tmp_path):
    """The intermediate layout (active_channel + system_models refs + fallback models) migrates too."""
    data, changed = chn.migrate({
        "active_channel": "gemini",
        "channels": [{"id": "gemini", "name": "主渠道", "type": "gemini", "base_url": "u1", "api_key": "k1", "model": "S/g"},
                     {"id": "fallback-1", "name": "备用", "type": "openai", "base_url": "u2", "api_key": "k2", "model": "LL/g"},
                     {"id": "draw", "name": "绘图分析", "type": "openai", "base_url": "u2", "api_key": "k3", "model": "d"}],
        "system_models": {"summary": {"channel": "fallback-1", "model": "flash"}, "image": {"channel": "fallback-1"}},
        "fallbacks": [{"channel": "fallback-1", "model": "rp/g"}, {"channel": "gemini", "model": "LL/g"}],
        "node_models": {"draw.image_gen": {"channel": "draw"}, "x.node": {"channel": "gemini"}},
    }, root=tmp_path)
    by_id = {c["id"]: c for c in data["channels"]}
    assert changed and "gemini" not in by_id and by_id["main"]["model"] == "S/g"
    assert data["node_models"]["x.node"] == {"channel": "main"}
    assert (by_id["summary"]["base_url"], by_id["summary"]["model"]) == ("u2", "flash")
    assert by_id["image"]["model"] == "LL/g"
    assert data["fallbacks"] == [{"channel": "fallback-1"}, {"channel": "fallback-2"}]
    assert by_id["fallback-1"]["model"] == "rp/g"
    assert (by_id["fallback-2"]["base_url"], by_id["fallback-2"]["model"]) == ("u1", "LL/g")


def test_migrate_empty_config_has_no_default_channel():
    data, _ = chn.migrate({"version": 1, "openai": {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt"}})
    assert chn.channel_list(data) == [] and not data.get("active_channel")


def test_model_list_keeps_every_model():
    import clonoth_model_list
    assert clonoth_model_list.filter_models(["LL/gpt-image-2.6-sunburst", "tts-1", "tts-1", "g4"]) == [
        "LL/gpt-image-2.6-sunburst", "tts-1", "g4"]


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
