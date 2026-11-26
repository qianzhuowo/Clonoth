"""plugins/draw/presets：预设清洗与校验、旧全局画幅设置的并入、小白X 参数预设文件互转；以及用到它的管理接口。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from plugins.draw import admin
from plugins.draw import presets as rules
from plugins.draw.agent.config import iter_presets, selected_preset


def _strict_error(raw: dict) -> str:
    with pytest.raises(rules.PresetError) as info:
        rules.clean_preset(raw, strict=True)
    return str(info.value)


def test_clean_fills_defaults_and_keeps_unknown_keys():
    out = rules.clean_preset({"id": " p1 ", "custom_key": 1, "negative_prefix": "", "aliases": "a, b，A",
                              "params": {"model": "my-model", "scale": "5.5", "ucPreset": "2", "future": "x"}})
    assert out["id"] == "p1" and out["name"] == "p1" and out["aliases"] == ["a", "b"]
    # 没给的字段用默认值；明确给了空字符串就是空
    assert out["positive_prefix"] == rules.DEFAULT_PRESET["positive_prefix"] and out["negative_prefix"] == ""
    assert out["max_images"] == 0 and out["max_characters_per_image"] == 4
    assert out["override_size"] == "auto" and "width" not in out["params"]
    params = out["params"]
    assert params["model"] == "my-model" and params["steps"] == 28 and params["seed"] == -1
    assert params["scale"] == 5.5 and params["ucPreset"] == 2 and params["qualityToggle"] is True and params["sm"] is False
    # 不认识的键原样保留
    assert out["custom_key"] == 1 and params["future"] == "x"


def test_clean_strict_names_the_preset_and_the_field():
    assert _strict_error({"id": "a", "name": "甲", "params": {"steps": 99}}) == "预设「甲」的步数必须是 1–50 的整数"
    assert "步数" in _strict_error({"id": "a", "params": {"steps": 28.5}})
    assert "CFG" in _strict_error({"id": "a", "params": {"scale": "abc"}})
    assert "CFG 重缩放" in _strict_error({"id": "a", "params": {"cfg_rescale": 1.5}})
    assert "种子必须是 -1（随机）" in _strict_error({"id": "a", "params": {"seed": -2}})
    assert "种子" in _strict_error({"id": "a", "params": {"seed": rules.MAX_SEED + 1}})
    assert "负面预设" in _strict_error({"id": "a", "params": {"ucPreset": 4}})
    assert "质量增强" in _strict_error({"id": "a", "params": {"qualityToggle": "yes"}})
    assert "模型不能为空" in _strict_error({"id": "a", "params": {"model": "  "}})
    assert "要求出图数" in _strict_error({"id": "a", "max_images": 17})
    assert "每张图最多角色数" in _strict_error({"id": "a", "max_characters_per_image": -1})
    assert "名称" in _strict_error({"id": "a", "name": "名" * 65})
    assert "别名" in _strict_error({"id": "a", "aliases": 5})
    assert "正向固定" in _strict_error({"id": "a", "positive_prefix": {}})
    assert "缺少 id" in _strict_error({"name": "x"})
    with pytest.raises(rules.PresetError):
        rules.clean_preset(["not", "a", "preset"])


def test_clean_size_rules():
    fixed = rules.clean_preset({"id": "a", "override_size": "default", "params": {"width": 1088, "height": "1920"}})
    assert fixed["override_size"] == "default" and (fixed["params"]["width"], fixed["params"]["height"]) == (1088, 1920)
    # 选了固定尺寸但宽高不能用
    assert "尺寸" in _strict_error({"id": "a", "override_size": "default"})
    assert "尺寸" in _strict_error({"id": "a", "override_size": "default", "params": {"width": 1000, "height": 1024}})
    assert "尺寸" in _strict_error({"id": "a", "override_size": "default", "params": {"width": 4096, "height": 1024}})
    assert "尺寸只能是 auto 或 default" in _strict_error({"id": "a", "override_size": "huge"})
    # 旧写法：override_size 直接写了标准尺寸
    old = rules.clean_preset({"id": "a", "override_size": "1280X768", "params": {"width": 64, "height": 64}})
    assert old["override_size"] == "default" and (old["params"]["width"], old["params"]["height"]) == (1280, 768)
    # 自动尺寸下，不能用的宽高直接丢掉，能用的留着（切回固定尺寸时还在）
    assert "width" not in rules.clean_preset({"id": "a", "params": {"width": 999, "height": 999}})["params"]
    assert rules.clean_preset({"id": "a", "params": {"width": 1024, "height": 1024}})["params"]["width"] == 1024


def test_clean_lenient_repairs_and_explains():
    notes: list[str] = []
    out = rules.clean_preset({"id": "a", "max_images": 99, "override_size": "default",
                              "params": {"steps": 99, "scale": "abc", "seed": -5, "ucPreset": 9, "qualityToggle": "yes", "model": ""}},
                             strict=False, notes=notes)
    params = out["params"]
    assert params["steps"] == 50 and params["scale"] == 6 and params["seed"] == -1 and params["ucPreset"] == 0
    assert params["qualityToggle"] is True and params["model"] == "nai-diffusion-4-5-full"
    assert out["max_images"] == 16 and out["override_size"] == "auto"
    assert "步数必须是 1–50 的整数，已改为 50" in notes and any(n.startswith("质量增强") and n.endswith("已改为 开") for n in notes)
    assert any(n.startswith("尺寸") and n.endswith("已改为 auto") for n in notes) and len(notes) == 8


def test_legacy_global_size_applies_only_to_presets_without_their_own():
    settings = {"planner": {"override_size": "1024x1024"}, "params": {"selected_preset_id": "B", "presets": [
        {"id": "a"}, {"id": "b", "override_size": "auto", "params": {"steps": 999}},
        {"id": "c", "override_size": "default", "params": {"width": 768, "height": 1280}}]}}
    a, b, c = iter_presets(settings)
    assert a["override_size"] == "default" and (a["params"]["width"], a["params"]["height"]) == (1024, 1024)
    assert b["override_size"] == "auto" and b["params"]["steps"] == 50
    assert c["override_size"] == "default" and (c["params"]["width"], c["params"]["height"]) == (768, 1280)
    assert selected_preset(settings)["id"] == "b"
    # 旧全局设置是 auto 或认不出的值：都按 auto
    for legacy in ("auto", "999x999", None):
        assert rules.clean_preset({"id": "a"}, legacy_size=legacy)["override_size"] == "auto"
    only = iter_presets({})
    assert len(only) == 1 and only[0]["id"] == "default-v45-full" and only[0]["override_size"] == "auto"


def test_panel_options_agree_with_the_rules():
    options = rules.panel_options()
    assert all(rules.parse_size(item["value"]) for item in options["sizes"])
    assert [item["value"] for item in options["uc_presets"]] == [0, 1, 2, 3]
    assert options["limits"]["steps"] == [1, 50] and options["dimension"] == {"min": 64, "max": 2048, "step": 64}
    assert rules.clean_preset(options["blank"], strict=True) == options["blank"]
    # V5 模型已接入：在下拉里，并带上它们的质量词 / 负面预设选项和 Medium 的固定参数
    assert set(options["v5_models"]) <= {item["value"] for item in options["models"]}
    assert [i["value"] for i in options["v5_quality_presets"]] == ["standard", "light", "none"]
    assert [i["value"] for i in options["v5_uc_presets"]] == ["heavy", "light", "humanFocus", "furryFocus", "none"]
    assert options["locked"]["nai-diffusion-5-full-medium"]["steps"] == 14 and options["v5_max_characters"] == 10


def test_v5_fields_default_from_legacy_switches_and_medium_is_locked():
    legacy = rules.clean_preset({"id": "a", "params": {"model": "nai-diffusion-5-full", "qualityToggle": False, "ucPreset": 2}}, strict=True)
    assert legacy["params"]["v5QualityPresetId"] == "none" and legacy["params"]["v5UcPresetId"] == "humanFocus"
    assert legacy["params"]["transparentBackground"] is False
    explicit = rules.clean_preset({"id": "a", "params": {"v5QualityPresetId": "light", "v5UcPresetId": "none", "transparentBackground": True}}, strict=True)
    assert explicit["params"]["v5QualityPresetId"] == "light" and explicit["params"]["v5UcPresetId"] == "none" and explicit["params"]["transparentBackground"] is True
    with pytest.raises(rules.PresetError, match="V5 质量预设"):
        rules.clean_preset({"id": "a", "params": {"v5QualityPresetId": "ultra"}}, strict=True)
    notes: list[str] = []
    loose = rules.clean_preset({"id": "a", "params": {"v5UcPresetId": "nope"}}, strict=False, notes=notes)
    assert loose["params"]["v5UcPresetId"] == "heavy" and any("V5 负面预设" in n for n in notes)
    # Medium：不管写什么都改回固定值，不报错
    medium = rules.clean_preset({"id": "m", "params": {"model": "nai-diffusion-5-full-medium", "steps": 40, "sampler": "ddim",
                                                      "cfg_rescale": 0.5, "v5UcPresetId": "none"}}, strict=True)
    assert (medium["params"]["steps"], medium["params"]["sampler"], medium["params"]["cfg_rescale"], medium["params"]["v5UcPresetId"]) \
        == (14, "k_euler_ancestral", 0, "heavy")
    assert rules.locked_params("nai-diffusion-5-full") == {}
    # 规划时的角色上限：V5 填 0 就是 10，填了取较小；其它模型按预设
    assert rules.effective_max_characters({"max_characters_per_image": 0, "params": {"model": "nai-diffusion-5-curated"}}) == 10
    assert rules.effective_max_characters({"max_characters_per_image": 3, "params": {"model": "nai-diffusion-5-full"}}) == 3
    assert rules.effective_max_characters({"max_characters_per_image": 0, "params": {"model": "nai-diffusion-4-5-full"}}) == 0


LWB_FILE = {"type": "novel-draw-preset", "version": 2, "name": "云端风格", "author": "x", "preset": {
    "positivePrefix": "masterpiece", "negativePrefix": "lowres", "maxImages": 2, "maxCharactersPerImage": 3,
    "overrideSize": "default", "thumbnail": "data:image/jpeg;base64,AAAA", "vibe": {"references": []},
    "params": {"model": "nai-diffusion-4-5-curated", "sampler": "k_euler", "scheduler": "native", "steps": 99, "scale": 5,
               "width": 1024, "height": 1024, "seed": -1, "ucPreset": 2, "v5QualityPresetId": "standard"}}}


def test_import_lwb_preset_file():
    preset, notes = rules.import_preset(LWB_FILE)
    assert preset["id"].startswith("preset-") and preset["name"] == "云端风格"
    assert preset["positive_prefix"] == "masterpiece" and preset["negative_prefix"] == "lowres"
    assert preset["max_images"] == 2 and preset["max_characters_per_image"] == 3
    params = preset["params"]
    assert preset["override_size"] == "default" and (params["width"], params["height"]) == (1024, 1024)
    assert params["model"] == "nai-diffusion-4-5-curated" and params["sampler"] == "k_euler" and params["scheduler"] == "native"
    assert params["steps"] == 50 and params["ucPreset"] == 2 and params["v5QualityPresetId"] == "standard"
    assert "thumbnail" not in preset and "vibe" not in preset
    assert any("步数" in n for n in notes) and any("Vibe" in n for n in notes) and len(notes) == 2
    assert rules.import_preset(LWB_FILE)[0]["id"] != preset["id"]

    # v1：没有数量限制；没写 overrideSize 也没带宽高时按小白X 的默认尺寸固定
    old, old_notes = rules.import_preset({"type": "novel-draw-preset", "version": 1,
                                          "preset": {"maxImages": 5, "maxCharactersPerImage": 5, "params": {"steps": 23}}})
    assert old["name"] == "导入的预设" and old["max_images"] == 0 and old["max_characters_per_image"] == 0
    assert old["override_size"] == "default" and (old["params"]["width"], old["params"]["height"]) == (1216, 832)
    assert old["params"]["steps"] == 23 and old_notes == []

    auto, _ = rules.import_preset({"type": "novel-draw-preset", "version": 2, "preset": {"overrideSize": "auto", "params": {}}})
    assert auto["override_size"] == "auto"
    v5, v5_notes = rules.import_preset({"type": "novel-draw-preset", "version": 2,
                                        "preset": {"overrideSize": "auto", "params": {"model": "nai-diffusion-5-full"}}})
    assert v5["params"]["model"] == "nai-diffusion-5-full" and v5_notes == []

    for bad in (None, [], {"type": "other", "version": 2, "preset": {}}, {"type": "novel-draw-preset", "version": 2},
                {"type": "novel-draw-preset", "version": 3, "preset": {}}, {"type": "novel-draw-preset", "preset": {}}):
        with pytest.raises(rules.PresetError):
            rules.import_preset(bad)


def test_export_roundtrips_through_import():
    source = {"id": "a", "name": "甲", "aliases": ["x"], "positive_prefix": "p", "negative_prefix": "", "max_images": 2,
              "max_characters_per_image": 3, "override_size": "default",
              "params": {"width": 1024, "height": 1024, "steps": 23, "scale": 5.5, "seed": 7, "variety_boost": True}}
    data = rules.export_preset(source)
    assert data["type"] == "novel-draw-preset" and data["version"] == 2 and data["name"] == "甲" and data["exportDate"].endswith("Z")
    body = data["preset"]
    assert body["positivePrefix"] == "p" and body["negativePrefix"] == "" and body["maxImages"] == 2
    assert body["maxCharactersPerImage"] == 3 and body["overrideSize"] == "default"
    assert body["params"]["width"] == 1024 and body["params"]["steps"] == 23 and body["params"]["seed"] == 7

    back, notes = rules.import_preset(data)
    cleaned = rules.clean_preset(source)
    assert notes == [] and back["id"] != "a" and back["aliases"] == []
    for key in ("name", "positive_prefix", "negative_prefix", "max_images", "max_characters_per_image", "override_size", "params"):
        assert back[key] == cleaned[key], key

    with pytest.raises(rules.PresetError):
        rules.export_preset({"id": "a", "params": {"steps": 0}})


# ---- 管理接口 ----

def _workspace(tmp_path: Path, *, planner: dict | None = None, selected: str = "p1") -> Path:
    draw = tmp_path / "tools" / "drawtools"
    draw.mkdir(parents=True)
    settings: dict = {"api": {"api_key": ""}, "params": {"selected_preset_id": selected, "presets": [
        {"id": "p1", "name": "P1", "positive_prefix": "best quality", "negative_prefix": "lowres",
         "params": {"model": "nai-diffusion-4-5-full", "seed": 77}},
        {"id": "p2", "name": "P2", "override_size": "default", "params": {"width": 768, "height": 1280}}]}}
    if planner:
        settings["planner"] = planner
    (draw / "settings.example.yaml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    return tmp_path


def _size(preset: dict) -> tuple:
    return preset["override_size"], preset["params"].get("width"), preset["params"].get("height")


def test_admin_list_shows_what_generation_uses(tmp_path: Path):
    root = _workspace(tmp_path / "a", planner={"override_size": "1280x768"})
    view = admin.list_presets(root)
    p1, p2 = view["presets"]
    assert view["selected_preset_id"] == "p1" and view["options"]["sizes"][0]["value"] == "832x1216"
    # p1 没写自己的画幅：沿用旧的全局固定尺寸；p2 写了，用自己的
    assert _size(p1) == ("default", 1280, 768) and _size(p2) == ("default", 768, 1280)
    assert p1["params"]["steps"] == 28 and p1["params"]["seed"] == 77 and p1["max_characters_per_image"] == 4
    # 选中的 id 指向不存在的预设时，实际生效的是第一套
    assert admin.list_presets(_workspace(tmp_path / "b", selected="gone"))["selected_preset_id"] == "p1"


def test_admin_save_validates_and_writes_clean_presets(tmp_path: Path):
    root = _workspace(tmp_path)
    private = root / "tools/drawtools/settings.yaml"
    presets = admin.list_presets(root)["presets"]
    presets[0]["params"]["steps"] = 99
    with pytest.raises(admin.AdminError) as info:
        admin.save_presets(root, presets, "p1")
    assert info.value.message == "预设「P1」的步数必须是 1–50 的整数" and not private.exists()
    presets[0]["params"]["steps"] = 30
    with pytest.raises(admin.AdminError) as info:
        admin.save_presets(root, [presets[0], {**presets[1], "id": "P1"}])
    assert "重复" in info.value.message and not private.exists()

    presets[0]["override_size"] = "default"
    presets[0]["params"].update(width=1088, height=1920)
    out = admin.save_presets(root, presets, "p2")
    assert out["selected_preset_id"] == "p2" and _size(out["presets"][0]) == ("default", 1088, 1920)
    raw = yaml.safe_load(private.read_text(encoding="utf-8"))["params"]
    assert raw["selected_preset_id"] == "p2" and _size(raw["presets"][0]) == ("default", 1088, 1920)
    assert raw["presets"][0]["params"]["steps"] == 30 and raw["presets"][1]["params"]["sampler"] == "k_euler_ancestral"


def test_quick_test_size_and_seed_follow_the_preset(tmp_path: Path):
    root = _workspace(tmp_path)

    def dry(**body) -> tuple:
        params = asyncio.run(admin.quick_test(root, {"scene": "x", "orientation": "landscape", "dry_run": True, **body}))["payload"]["parameters"]
        return params["width"], params["height"], params["seed"]

    assert dry() == (1216, 832, 77)
    width, height, seed = dry(preset="p2")
    assert (width, height) == (768, 1280) and 0 <= seed <= rules.MAX_SEED
    assert dry(preset="p2", override_size="")[:2] == (768, 1280)
    assert dry(preset="p2", override_size="auto", seed=5) == (1216, 832, 5)
    assert dry(preset="p2", override_size="1024x1024")[:2] == (1024, 1024)


def test_preset_exchange_routes(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.draw.routes import build_api_router

    app = FastAPI()
    app.state.workspace_root = _workspace(tmp_path)
    app.include_router(build_api_router(), prefix="/v1/plugins/draw")
    client = TestClient(app)
    base = "/v1/plugins/draw/presets"
    preset = client.get(base).json()["data"]["presets"][1]
    exported = client.post(base + "/export", json={"preset": {**preset, "name": 'a/b:c*?"<>|'}}).json()
    assert exported["ok"] is True and exported["data"]["filename"] == "a_b_c______.json"
    assert exported["data"]["data"]["type"] == "novel-draw-preset"
    back = client.post(base + "/import", json={"data": exported["data"]["data"]}).json()["data"]
    assert _size(back["preset"]) == ("default", 768, 1280) and back["notes"] == [] and back["preset"]["id"] != "p2"
    # 导入不写盘
    assert not (tmp_path / "tools/drawtools/settings.yaml").exists()
    for path, body in (("/import", {"data": {"type": "x"}}), ("/import", {}), ("/export", {}),
                       ("", {"presets": [{"id": "a", "params": {"scale": 99}}]})):
        resp = client.post(base + path, json=body)
        assert resp.status_code == 400 and resp.json()["ok"] is False and resp.json()["message"], path
    assert "CFG" in client.post(base, json={"presets": [{"id": "a", "params": {"scale": 99}}]}).json()["message"]
