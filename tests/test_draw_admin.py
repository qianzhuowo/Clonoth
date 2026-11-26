"""plugins/draw/admin + routes：管理面板数据层与 HTTP 壳的离线测试。"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
import yaml

from plugins.draw import admin
from plugins.draw.planner.frame import CONTENT_TEMPLATE
from plugins.draw.planner.layout import create_default_layout


def _workspace(tmp_path: Path) -> Path:
    draw = tmp_path / "tools" / "drawtools"
    (draw / "prompts" / "novelai").mkdir(parents=True)
    (draw / "settings.example.yaml").write_text(yaml.safe_dump({
        "api": {"api_key": "", "base_url": "https://image.novelai.net"},
        "params": {"selected_preset_id": "p1", "presets": [
            {"id": "p1", "name": "P1", "positive_prefix": "best quality", "negative_prefix": "lowres",
             "max_images": 0, "max_characters_per_image": 4, "params": {"model": "nai-diffusion-4-5-full"}}]},
    }, allow_unicode=True), encoding="utf-8")
    (draw / "character_tags.example.yaml").write_text(yaml.safe_dump({"characters": [
        {"name": "艾丽丝", "aliases": ["小艾"], "type": "girl", "danbooru": "alice", "appearance": "blonde hair"}]},
        allow_unicode=True), encoding="utf-8")
    for name, text in (("top-system", "EX-OPENING"), ("tag-guide", "EX-GUIDE"), ("scene-rules", "EX-RULES")):
        (draw / "prompts" / "novelai" / f"{name}.example.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_settings_view_masks_key_and_update_merges(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path)
    monkeypatch.delenv("NOVELAI_API_KEY", raising=False)
    view = admin.get_settings_view(root)
    assert view["api"]["api_key"] == "" and view["api_key_configured"] is False and view["is_private"] is False
    assert view["planner"]["tool_protocol"] == "native"

    view = admin.update_settings(root, {"api": {"api_key": "secret-key", "timeout_sec": 99},
                                        "planner": {"tool_protocol": "text", "max_plan_images": 2}})
    assert view["api"]["api_key"] == "" and view["api_key_configured"] is True and view["is_private"] is True
    assert view["api"]["timeout_sec"] == 99 and view["planner"]["tool_protocol"] == "text"
    raw = yaml.safe_load((root / "tools/drawtools/settings.yaml").read_text(encoding="utf-8"))
    assert raw["api"]["api_key"] == "secret-key"
    # 空 key 不覆盖；clear_api_key 才清空
    admin.update_settings(root, {"api": {"api_key": ""}})
    assert yaml.safe_load((root / "tools/drawtools/settings.yaml").read_text(encoding="utf-8"))["api"]["api_key"] == "secret-key"
    view = admin.update_settings(root, {"clear_api_key": True})
    assert view["api_key_configured"] is False
    # 未写段落保留 example 的默认
    assert view["params"]["selected_preset_id"] == "p1"


def test_legacy_recent_dialogue_turns_is_dropped(tmp_path: Path):
    root = _workspace(tmp_path)
    (root / "tools/drawtools/settings.yaml").write_text(
        yaml.safe_dump({"planner": {"recent_dialogue_turns": 6, "max_plan_images": 3}}), encoding="utf-8")
    assert "recent_dialogue_turns" not in admin.get_settings_view(root)["planner"]
    admin.update_settings(root, {"planner": {"max_plan_images": 2}})
    raw = yaml.safe_load((root / "tools/drawtools/settings.yaml").read_text(encoding="utf-8"))
    assert "recent_dialogue_turns" not in raw["planner"] and raw["planner"]["max_plan_images"] == 2


def test_presets_and_characters_roundtrip(tmp_path: Path):
    root = _workspace(tmp_path)
    out = admin.save_presets(root, [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}], "b")
    assert out["selected_preset_id"] == "b" and [p["id"] for p in out["presets"]] == ["a", "b"]
    with pytest.raises(admin.AdminError):
        admin.save_presets(root, [{"id": "a"}, {"id": "a"}])
    with pytest.raises(admin.AdminError):
        admin.save_presets(root, [{"id": "a"}], "zzz")
    assert admin.list_presets(root)["selected_preset_id"] == "b"

    library = admin.save_character_library(root, {"characters": [
        {"name": "初音", "aliases": ["miku", ""], "outfits": [{"name": "默认", "tags": "x"}]}]})
    saved = library["characters"][0]
    assert saved["id"] and library["is_private"] is True and library["groups"] == []
    assert {k: v for k, v in saved.items() if k != "id"} == {
        "name": "初音", "enabled": True, "aliases": ["miku"], "type": "girl", "danbooru": "", "appearance": "",
        "negative_tags": "", "outfits": [{"name": "默认", "tags": "x", "enabled": True}], "dynamic_states": [], "group_id": ""}
    assert admin.list_characters(root)[0]["name"] == "初音"
    with pytest.raises(admin.AdminError):
        admin.save_character_library(root, {"characters": [{"name": "x"}, {"name": "X"}]})
    # 带着过期的 revision 保存：拒绝，盘上的内容不变。
    with pytest.raises(admin.AdminError) as stale:
        admin.save_character_library(root, {"characters": [], "revision": "0000000000000000"})
    assert stale.value.status == 409 and admin.list_characters(root)[0]["name"] == "初音"
    assert admin.save_character_library(root, {"characters": [], "revision": library["revision"]})["characters"] == []


def test_prompts_save_reset_and_content_placeholder_check(tmp_path: Path):
    root = _workspace(tmp_path)
    prompts = admin.get_prompts(root)
    assert prompts["opening"] == {"content": "EX-OPENING", "default": "EX-OPENING", "is_default": True}
    assert prompts["content"]["content"] == CONTENT_TEMPLATE and prompts["content"]["is_default"]

    saved = admin.save_prompt(root, "guide", "MY-GUIDE")
    assert saved["content"] == "MY-GUIDE" and saved["is_default"] is False and saved["default"] == "EX-GUIDE"
    assert (root / "tools/drawtools/prompts/novelai/tag-guide.md").read_text(encoding="utf-8") == "MY-GUIDE"
    assert admin.reset_prompt(root, "guide")["is_default"] is True
    assert not (root / "tools/drawtools/prompts/novelai/tag-guide.md").exists()
    with pytest.raises(admin.AdminError):
        admin.save_prompt(root, "rules", "   ")
    with pytest.raises(admin.AdminError):
        admin.save_prompt(root, "nope", "x")

    with pytest.raises(admin.AdminError) as info:
        admin.save_prompt(root, "content", "只有 {{drawRequest}}")
    assert "{{characterInfo}}" in info.value.details["missing"]
    tpl = "A {{characterInfo}} B {{drawRequest}}"
    assert admin.save_prompt(root, "content", tpl)["content"] == tpl
    assert admin.reset_prompt(root, "content")["is_default"] is True


def test_layout_fields_and_preview(tmp_path: Path):
    root = _workspace(tmp_path)
    assert admin.get_layout(root)["is_default"] is True
    custom = create_default_layout()
    custom["blocks"] = [{"id": "msg-1", "role": "user", "content": "自定义提醒", "enabled": True, "stage": "setup"},
                        *custom.get("blocks", [])] if isinstance(custom.get("blocks"), list) else custom.get("blocks")
    out = admin.save_layout(root, custom)
    assert isinstance(out["layout"], dict)
    # 存默认布局 → 稀疏存储为 None
    assert admin.save_layout(root, create_default_layout())["is_default"] is True
    raw = yaml.safe_load((root / "tools/drawtools/settings.yaml").read_text(encoding="utf-8"))
    assert raw["planner"]["layout"] is None

    fields = admin.get_field_descriptions(root)
    assert fields["fields"][0]["path"] == "tool" and "tool" in fields["defaults"]
    admin.update_settings(root, {"planner": {"field_descriptions": {"tool": "自定义说明"}}})
    assert admin.get_field_descriptions(root)["fields"][0]["description"] == "自定义说明"

    chain = admin.preview_chain(root)
    roles = [m["role"] for m in chain]
    assert roles[0] == "system" and roles[-1] == "tools"
    joined = "\n".join(s["content"] for m in chain for s in m.get("sections", []))
    assert "EX-OPENING" in joined and "EX-RULES" in joined and "{{drawRequest}}" in joined
    assert chain[-1]["tool"]["function"]["name"] == "submit_scene_plan"
    # 预览可用未保存的文本覆盖
    chain2 = admin.preview_chain(root, texts_override={"opening": "DRAFT"})
    assert "DRAFT" in chain2[0]["sections"][0]["content"]


def test_images_list_delete_cleanup(tmp_path: Path):
    root = _workspace(tmp_path)
    folder = root / "data" / "attachments" / "novelai"
    folder.mkdir(parents=True)
    old = folder / "nai_old.png"
    old.write_bytes(b"x" * 10)
    os.utime(old, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    (folder / "nai_new.png").write_bytes(b"y" * 20)
    (folder / "note.txt").write_text("skip")
    listed = admin.list_images(root)
    assert [i["name"] for i in listed["images"]] == ["nai_new.png", "nai_old.png"] and listed["total_bytes"] == 30
    assert admin.delete_images(root, ["../../evil.png", "note.txt", "nai_new.png"]) == {"deleted": ["nai_new.png"]}
    admin.update_settings(root, {"storage": {"retention_days": 7, "max_total_mb": 2048}})
    assert admin.cleanup_images(root)["deleted"] == ["nai_old.png"]


def test_quick_test_dry_run_and_api_errors(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path)
    monkeypatch.delenv("NOVELAI_API_KEY", raising=False)
    out = asyncio.run(admin.quick_test(root, {"scene": "indoors", "orientation": "landscape", "dry_run": True,
                                              "characters": [{"name": "小艾", "costume": "dress", "action": "smile"}], "seed": 5}))
    assert out["dry_run"] and out["prompt"] == "best quality, indoors"
    assert out["payload"]["parameters"]["width"] == 1216 and out["payload"]["parameters"]["seed"] == 5
    assert out["character_prompts"][0]["prompt"].startswith("girl, alice, blonde hair, dress")
    with pytest.raises(admin.AdminError):
        asyncio.run(admin.quick_test(root, {"scene": ""}))
    with pytest.raises(admin.AdminError) as info:
        asyncio.run(admin.quick_test(root, {"scene": "x"}))
    assert info.value.status == 502 and info.value.details["code"] == "API_KEY_MISSING"


def test_routes_mount_under_plugin_prefix(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.draw.routes import build_api_router

    root = _workspace(tmp_path)
    app = FastAPI()
    app.state.workspace_root = root
    app.include_router(build_api_router(), prefix="/v1/plugins/draw")
    client = TestClient(app)
    assert client.get("/v1/plugins/draw/settings").json()["data"]["api"]["api_key"] == ""
    resp = client.post("/v1/plugins/draw/prompts/content", json={"content": "bad"})
    assert resp.status_code == 400 and resp.json()["ok"] is False and "missing" in resp.json()["details"]
    assert client.get("/v1/plugins/draw/fields").json()["ok"] is True
    assert client.post("/v1/plugins/draw/preview", json={}).json()["data"]["chain"][-1]["role"] == "tools"
    assert client.get("/v1/plugins/draw/images").json()["data"]["count"] == 0


# ---- 场景 Agent ----

def _raw_agent(root: Path) -> dict:
    return yaml.safe_load((root / "tools/drawtools/settings.yaml").read_text(encoding="utf-8"))["agent"]


def _with_channels(root: Path) -> Path:
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "config.yaml").write_text(yaml.safe_dump({"active_channel": "main", "channels": [
        {"id": "main", "name": "主渠道", "type": "openai", "base_url": "https://relay.test/v1", "api_key": "sk-main", "model": "m-main"},
        {"id": "draw", "name": "绘图分析", "type": "gemini", "base_url": "https://g.test/v1beta", "api_key": "sk-draw", "model": "m-draw"},
    ]}, allow_unicode=True), encoding="utf-8")
    return root


def test_agent_view_defaults_to_node_channel(tmp_path: Path):
    root = _with_channels(_workspace(tmp_path))
    view = admin.get_agent_view(root)
    assert [p["id"] for p in view["presets"]] == ["default"] and view["current_preset_id"] == "default"
    assert view["planner_max_retries"] == 3 and view["is_private"] is False
    assert view["effective"]["source"] == "node"
    assert [c["id"] for c in view["channels"]] == ["main", "draw"] and "sk-" not in str(view["channels"])
    assert "high" in view["reasoning_efforts"]


def test_agent_save_references_channel_and_validates(tmp_path: Path):
    root = _with_channels(_workspace(tmp_path))
    ok = {"id": "a", "name": "A", "channel": "draw"}
    bad_patches = [
        {"presets": []},
        {"presets": ["junk"]},
        {"presets": [{**ok, "name": "  "}]},
        {"presets": [{**ok, "id": ""}]},
        {"presets": [ok, {**ok, "id": "b"}]},
        {"presets": [ok, {**ok, "name": "B"}]},
        {"presets": [{**ok, "channel": "no-such-channel"}]},
        {"presets": [ok], "current_preset_id": "gone"},
        {"planner_max_retries": 11},
        {"planner_max_retries": 1.5},
        {"planner_max_retries": True},
    ]
    for patch in bad_patches:
        with pytest.raises(admin.AdminError):
            admin.save_agent(root, patch)
    assert not (root / "tools/drawtools/settings.yaml").exists()

    view = admin.save_agent(root, {"presets": [ok, {"id": "b", "name": "B"}], "current_preset_id": "a"})
    assert view["effective"] == {"source": "preset"} and view["is_private"] is True
    raw = _raw_agent(root)["presets"][0]
    assert raw["channel"] == "draw" and not {"provider", "base_url", "api_key"} & set(raw)
    view = admin.save_agent(root, {"current_preset_id": "b"})
    assert view["effective"]["source"] == "node"
    view = admin.save_agent(root, {"planner_max_retries": 0})
    assert view["planner_max_retries"] == 0 and view["current_preset_id"] == "b"
    view = admin.save_agent(root, {"presets": [ok]})
    assert view["current_preset_id"] == "a"


def test_agent_pull_and_check_use_the_selected_channel(tmp_path: Path, monkeypatch):
    root = _with_channels(_workspace(tmp_path))
    seen: list = []

    async def pull(kind, base_url, api_key, **kw):
        seen.append((kind, base_url, api_key))
        return ["m1", "m2"]

    async def check(preset, **kw):
        seen.append(dict(preset))
        return {"ok": True, "reply": "OK"}

    monkeypatch.setattr(admin.clonoth_model_list, "pull_models", pull)
    monkeypatch.setattr(admin.llm, "check_connection", check)
    draft = {"id": "a", "name": "A", "channel": "draw", "model": ""}
    assert asyncio.run(admin.pull_agent_models(root, {"preset": draft})) == {"channel": "draw", "models": ["m1", "m2"]}
    assert seen[-1] == ("gemini", "https://g.test/v1beta", "sk-draw")
    asyncio.run(admin.check_agent_connection(root, {"preset": {**draft, "model": "typed"}}))
    assert (seen[-1]["provider"], seen[-1]["model"], seen[-1]["api_key"]) == ("gemini", "typed", "sk-draw")
    asyncio.run(admin.check_agent_connection(root, {"preset": draft}))
    assert seen[-1]["model"] == "m-draw"

    async def pull_fail(*a, **kw):
        raise admin.clonoth_model_list.ModelListError("Gemini 拉取模型失败：401：bad key", 401)

    monkeypatch.setattr(admin.clonoth_model_list, "pull_models", pull_fail)
    with pytest.raises(admin.AdminError) as info:
        asyncio.run(admin.pull_agent_models(root, {"preset": draft}))
    assert info.value.status == 502 and info.value.details == {"status_code": 401} and "sk-draw" not in info.value.message
    for body in ({}, {"preset": {**draft, "channel": ""}}, {"preset": {**draft, "channel": "gone"}}):
        with pytest.raises(admin.AdminError):
            asyncio.run(admin.pull_agent_models(root, body))


def test_agent_routes(tmp_path: Path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.draw.routes import build_api_router

    root = _with_channels(_workspace(tmp_path))
    app = FastAPI()
    app.state.workspace_root = root
    app.include_router(build_api_router(), prefix="/v1/plugins/draw")
    client = TestClient(app)
    base = "/v1/plugins/draw/agent"

    assert client.get(base).json()["data"]["effective"]["source"] == "node"
    preset = {"id": "a", "name": "A", "channel": "main"}
    body = client.post(base, json={"presets": [preset]}).json()
    assert body["ok"] is True and body["data"]["effective"]["source"] == "preset" and "sk-main" not in str(body)
    resp = client.post(base, json={"planner_max_retries": 99})
    assert resp.status_code == 400 and resp.json()["ok"] is False

    async def pull(kind, base_url, api_key, **kw):
        return [api_key[:3] + "-model"]

    async def check(preset, **kw):
        return {"ok": False, "error": "HTTP 401"}

    monkeypatch.setattr(admin.clonoth_model_list, "pull_models", pull)
    monkeypatch.setattr(admin.llm, "check_connection", check)
    assert client.post(base + "/models", json={"preset": preset}).json()["data"]["models"] == ["sk--model"]
    resp = client.post(base + "/test", json={"preset": preset})
    assert resp.status_code == 200 and resp.json()["data"] == {"ok": False, "error": "HTTP 401"}
    assert client.post(base + "/test", json={}).status_code == 400
