"""plugins/draw/backends/novelai：角色提示词拼接、请求体编译、假 curl 执行与落盘。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from plugins.draw.backends import novelai as nai

KNOWN = [{"name": "艾丽丝", "aliases": ["小艾"], "type": "girl", "danbooru": "alice_(wonderland)",
          "appearance": "blonde hair, blue eyes, {long hair}", "negative_tags": "glasses"}]
PRESET = {"id": "p1", "name": "测试", "positive_prefix": "best quality", "negative_prefix": "lowres",
          "params": {"model": "nai-diffusion-4-5-full", "sampler": "k_euler_ancestral", "scheduler": "karras",
                     "steps": 28, "scale": 6, "seed": -1, "variety_boost": True}}


def test_known_character_prompt_order_and_dedupe():
    prompts = nai.assemble_character_prompts([
        {"name": "小艾", "type": "", "costume": "blue eyes, school uniform, 1.3::long hair::", "action": "smile, Blonde_Hair",
         "interact": "source#hugging", "uc": "hat", "center": {"x": 0.1, "y": 0.9}},
    ], KNOWN)
    p = prompts[0]
    assert p["name"] == "艾丽丝"
    # type → danbooru(下划线转空格) → appearance → costume(去重后) → action(去重后) → interact
    assert p["prompt"] == "girl, alice (wonderland), blonde hair, blue eyes, {long hair}, school uniform, 1.3::long hair::, smile, source#hugging"
    assert p["uc"] == "glasses, hat"
    assert p["center"] == {"x": 0.1, "y": 0.9}


def test_scene_type_overrides_library_type_and_unknown_character():
    prompts = nai.assemble_character_prompts([
        {"name": "小艾", "type": "woman, on the left", "costume": "", "action": "standing"},
        {"name": "路人", "type": "no_humans", "danbooru": "cat_(animal)", "appear": "black fur", "costume": "collar", "action": "sitting"},
    ], KNOWN)
    assert prompts[0]["prompt"].startswith("woman, on the left, alice (wonderland), blonde hair")
    assert "girl" not in prompts[0]["prompt"]
    assert prompts[1]["prompt"] == "no humans, cat (animal), black fur, collar, sitting"
    assert prompts[1]["center"] == {"x": 0.5, "y": 0.5}


def test_compile_task_v45_body_and_auto_size():
    task = {"index": 1, "title": "t", "orientation": "landscape", "scene": "indoors, window",
            "chars": [{"name": "小艾", "costume": "dress", "action": "smile", "center": {"x": 0.3, "y": 0.5}}]}
    compiled = nai.compile_task(task, preset=PRESET, known_characters=KNOWN, seed=42)
    body = compiled["payload"]
    assert compiled["prompt"] == "best quality, indoors, window"
    assert (compiled["width"], compiled["height"]) == (1216, 832)
    assert body["parameters"]["seed"] == 42 and body["parameters"]["use_coords"] is True
    assert body["parameters"]["v4_prompt"]["caption"]["char_captions"][0]["centers"] == [{"x": 0.3, "y": 0.5}]
    assert body["parameters"]["v4_negative_prompt"]["caption"]["base_caption"] == "lowres"
    assert body["parameters"]["skip_cfg_above_sigma"] is not None
    # 固定 override_size 覆盖方向；非法值忽略回落 auto
    fixed = nai.compile_task(task, preset=PRESET, known_characters=KNOWN, seed=1, override_size="1024x1024")
    assert (fixed["width"], fixed["height"]) == (1024, 1024)
    bad = nai.compile_task(task, preset=PRESET, known_characters=KNOWN, seed=1, override_size="999x999")
    assert (bad["width"], bad["height"]) == (1216, 832)
    # 随机 seed 在合法范围
    assert 0 <= nai.compile_task(task, preset=PRESET, known_characters=KNOWN)["seed"] <= nai.MAX_SEED


def test_compile_task_size_and_seed_follow_the_preset():
    task = {"index": 1, "orientation": "landscape", "scene": "x", "chars": []}
    fixed = {**PRESET, "override_size": "default", "params": {**PRESET["params"], "width": 1088, "height": 1920, "seed": 123}}

    def run(preset=fixed, task=task, **kw) -> tuple:
        out = nai.compile_task(task, preset=preset, known_characters=[], **kw)
        assert (out["payload"]["parameters"]["width"], out["payload"]["parameters"]["seed"]) == (out["width"], out["seed"])
        return out["width"], out["height"], out["seed"]

    # 预设选了固定尺寸：用预设的宽高；预设的种子不是 -1 就用它
    assert run() == (1088, 1920, 123)
    # 调用方另给画幅：auto 或标准尺寸才算数，其它值按预设
    assert run(override_size="auto")[:2] == (1216, 832)
    assert run(override_size="1024x1024")[:2] == (1024, 1024)
    for ignored in ("999x999", "", None, "default"):
        assert run(override_size=ignored)[:2] == (1088, 1920)
    # 预设是自动尺寸：按构图方向，不看预设里留着的宽高
    auto = {**fixed, "override_size": "auto"}
    assert run(auto)[:2] == (1216, 832) and run(auto, {**task, "orientation": "portrait"})[:2] == (832, 1216)
    # 选了固定尺寸却没有宽高（手改配置）：退回按构图方向
    assert run({**PRESET, "override_size": "default"})[:2] == (1216, 832)
    # 种子：调用方 > 任务 > 预设
    assert run(task={**task, "seed": 9})[2] == 9 and run(task={**task, "seed": 9}, seed=5)[2] == 5
    assert run(task={**task, "seed": None})[2] == 123


def test_compile_task_v3_merges_prompts():
    preset = {**PRESET, "params": {**PRESET["params"], "model": "nai-diffusion-3"}}
    task = {"index": 1, "orientation": "portrait", "scene": "street",
            "chars": [{"name": "路人", "appear": "red hair", "costume": "coat", "action": "walking", "uc": "hat"}]}
    body = nai.compile_task(task, preset=preset, known_characters=KNOWN, seed=7)["payload"]
    assert body["input"] == "best quality, street, red hair, coat, walking"
    assert body["parameters"]["negative_prompt"] == "lowres, hat"
    assert "v4_prompt" not in body["parameters"]


V5_PRESET = {**PRESET, "params": {**PRESET["params"], "model": "nai-diffusion-5-full", "v5QualityPresetId": "standard",
                                   "v5UcPresetId": "light", "transparentBackground": True}}


def test_compile_task_v5_body_quality_words_and_nsfw_rule():
    task = {"index": 1, "orientation": "portrait", "scene": "indoors, Masterpiece, Text: hello",
            "chars": [{"name": "小艾", "costume": "dress", "action": "smile", "uc": "hat", "center": {"x": 0.2, "y": 0.8}}]}
    compiled = nai.compile_task(task, preset=V5_PRESET, known_characters=KNOWN, seed=5)
    body, p = compiled["payload"], compiled["payload"]["parameters"]
    assert compiled["transport"] == "v5" and body["use_new_shared_trial"] is True and p["stream"] == "msgpack"
    # 质量词和透明背景插在 Text: 段前；已有的 masterpiece 不重复；Text: 段另起一行
    assert body["input"] == "best quality, indoors, Masterpiece, transparent background, very aesthetic, no text\nText: hello"
    assert p["v4_prompt"]["caption"]["base_caption"] == body["input"] and p["tag_hint_transparent_background"] is True
    # V5 Full + 负面预设不是 none → 负向前加 nsfw，然后是预设文字、用户负向
    assert p["negative_prompt"].startswith("nsfw, lowres, bad hands") and p["negative_prompt"].endswith(", lowres")
    assert p["params_version"] == 4 and p["use_coords"] is True and p["ucPresetId"] == "light" and p["qualityPresetId"] == "standard"
    assert p["characterPrompts"][0]["center"] == {"x": 0.2, "y": 0.8} and p["v4_negative_prompt"]["caption"]["char_captions"][0]["char_caption"] == "glasses, hat"
    assert "skip_cfg_above_sigma" not in p
    # 场景里已有 nsfw、或 Curated、或负面预设 none 时不加
    has = nai.compile_task({**task, "scene": "nsfw, indoors"}, preset=V5_PRESET, known_characters=KNOWN, seed=5)["payload"]["parameters"]
    assert not has["negative_prompt"].startswith("nsfw")
    curated = {**V5_PRESET, "params": {**V5_PRESET["params"], "model": "nai-diffusion-5-curated"}}
    assert not nai.compile_task(task, preset=curated, known_characters=KNOWN, seed=5)["payload"]["parameters"]["negative_prompt"].startswith("nsfw")
    none = {**V5_PRESET, "params": {**V5_PRESET["params"], "v5UcPresetId": "none", "transparentBackground": False}}
    assert nai.compile_task(task, preset=none, known_characters=KNOWN, seed=5)["payload"]["parameters"]["negative_prompt"] == "lowres"
    # Medium：后端强制锁参，哪怕预设里写了别的
    medium = {**V5_PRESET, "params": {**V5_PRESET["params"], "model": "nai-diffusion-5-full-medium", "steps": 40, "sampler": "ddim", "cfg_rescale": 0.4}}
    mp = nai.compile_task(task, preset=medium, known_characters=KNOWN, seed=5)["payload"]["parameters"]
    assert (mp["steps"], mp["sampler"], mp["cfg_rescale"], mp["ucPresetId"]) == (14, "k_euler_ancestral", 0, "heavy")
    # 超过 10 个角色拒绝
    many = {**task, "chars": [{"name": f"c{i}", "appear": "x"} for i in range(11)]}
    with pytest.raises(nai.NovelAIError) as info:
        nai.compile_task(many, preset=V5_PRESET, known_characters=KNOWN, seed=5)
    assert info.value.code == "V5_CHARACTER_LIMIT_EXCEEDED"


def _frames(*events) -> bytes:
    import msgpack
    out = b""
    for ev in events:
        packed = msgpack.packb(ev, use_bin_type=True)
        out += len(packed).to_bytes(4, "big") + packed
    return out


def test_parse_v5_stream():
    png = b"\x89PNG\r\n\x1a\n" + b"final"
    data = _frames({"event_type": "intermediate", "samp_ix": 0, "step_ix": 1, "image": b"\x89PNG\r\n\x1a\nmid"},
                   {"event_type": "final", "samp_ix": 0, "image": png},
                   {"event_type": "garbage-after-final"})
    assert nai.parse_v5_stream(data) == png
    with pytest.raises(nai.NovelAIError) as info:
        nai.parse_v5_stream(_frames({"event_type": "error", "message": "quota"}))
    assert info.value.code == "V5_PROVIDER_ERROR" and "quota" in info.value.message
    with pytest.raises(nai.NovelAIError) as info:
        nai.parse_v5_stream(_frames({"event_type": "intermediate", "samp_ix": 0}))
    assert info.value.code == "V5_NO_FINAL"
    with pytest.raises(nai.NovelAIError) as info:
        nai.parse_v5_stream(_frames({"event_type": "final", "samp_ix": 0, "image": png})[:-3])
    assert info.value.code == "V5_STREAM_TRUNCATED"
    with pytest.raises(nai.NovelAIError) as info:
        nai.parse_v5_stream(_frames({"event_type": "final", "samp_ix": 0, "image": b"not png"}))
    assert info.value.code == "V5_BAD_FRAME"
    with pytest.raises(nai.NovelAIError):
        nai.parse_v5_stream(b"\x00\x00\x00\x00zz")


FAKE_CURL_V5 = r"""
import sys, json, msgpack
from pathlib import Path
args = sys.argv[1:]
out = args[args.index("-o") + 1]
# 必须是 multipart：-F request=@<file>;type=application/json;filename=blob，不带 -d / Content-Type: application/json
assert "-d" not in args and "Content-Type: application/json" not in args
# V5 必须打到帧流接口；旧的 /ai/generate-image 不认 parameters.stream
assert "https://nai.test/ai/generate-image-stream" in args and "https://nai.test/ai/generate-image" not in args, args
form = args[args.index("-F") + 1]
assert form.startswith("request=@") and form.endswith(";type=application/json;filename=blob"), form
body = json.loads(Path(form[len("request=@"):-len(";type=application/json;filename=blob")]).read_text(encoding="utf-8"))
assert body["model"] == "nai-diffusion-5-full" and body["parameters"]["stream"] == "msgpack"
frames = b""
for ev in ({"event_type": "intermediate", "samp_ix": 0, "image": b"\x89PNG\r\n\x1a\nmid"},
           {"event_type": "final", "samp_ix": 0, "image": b"\x89PNG\r\n\x1a\n" + b"v5-final"}):
    packed = msgpack.packb(ev, use_bin_type=True)
    frames += len(packed).to_bytes(4, "big") + packed
Path(out).write_bytes(frames)
sys.stdout.write("200")
"""


def test_resolve_image_api_picks_the_endpoint_by_transport():
    assert nai.resolve_image_api("https://nai.test") == "https://nai.test/ai/generate-image"
    assert nai.resolve_image_api("https://nai.test/", "v5") == "https://nai.test/ai/generate-image-stream"
    assert nai.resolve_image_api("", "v5") == "https://image.novelai.net/ai/generate-image-stream"
    # 地址里已经带了接口路径：按 transport 换成对应的那个，查询串保留
    assert nai.resolve_image_api("https://nai.test/ai/generate-image", "v5") == "https://nai.test/ai/generate-image-stream"
    assert nai.resolve_image_api("https://nai.test/ai/generate-image-stream?k=1") == "https://nai.test/ai/generate-image?k=1"
    assert nai.resolve_image_api("https://nai.test/proxy", "v5") == "https://nai.test/proxy/ai/generate-image-stream"


def test_generate_v5_uses_multipart_and_takes_the_final_frame(tmp_path: Path, monkeypatch):
    fake = tmp_path / "fake_curl.py"
    fake.write_text(FAKE_CURL_V5, encoding="utf-8")
    monkeypatch.setenv("NOVELAI_CURL_BIN", json.dumps([sys.executable, str(fake)]))
    task = {"index": 1, "title": "v5", "orientation": "portrait", "scene": "indoors", "chars": []}
    compiled = nai.compile_task(task, preset=V5_PRESET, known_characters=KNOWN, seed=3)
    result = asyncio.run(nai.generate_image(compiled, api_key="k", base_url="https://nai.test",
                                            workspace_root=tmp_path, settings=_settings()))
    assert (tmp_path / result["path"]).read_bytes() == b"\x89PNG\r\n\x1a\n" + b"v5-final"
    assert result["model"] == "nai-diffusion-5-full" and result["seed"] == 3


FAKE_CURL_ZIP = r"""
import sys, zipfile
args = sys.argv[1:]
out = args[args.index("-o") + 1]
with zipfile.ZipFile(out, "w") as zf:
    zf.writestr("image_0.png", b"\x89PNG\r\n\x1a\n" + b"fake")
sys.stdout.write("200")
"""

FAKE_CURL_LOCK_THEN_OK = r"""
import sys, zipfile, os
from pathlib import Path
args = sys.argv[1:]
out = args[args.index("-o") + 1]
marker = Path(out).parent / "lock_hit"
if not marker.exists():
    marker.write_text("1")
    Path(out).write_text("Concurrent generation is locked")
    sys.stdout.write("429")
else:
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("image_0.png", b"\x89PNG\r\n\x1a\n" + b"ok")
    sys.stdout.write("200")
"""

FAKE_CURL_400 = r"""
import sys
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("-o") + 1]).write_text('{"message":"bad"}')
sys.stdout.write("400")
"""


def _settings(**gen):
    return {"api": {"timeout_sec": 5},
            "generation": {"retry_wait_sec": 0, "retry_max_attempts": 1, "lock_retry_max_attempts": 2,
                           "lock_retry_wait_min_sec": 0, "lock_retry_wait_max_sec": 0, "lock_timeout_sec": 5, **gen}}


def _generate(tmp_path: Path, monkeypatch, fake_curl: str, settings=None, api_key="k"):
    fake = tmp_path / "fake_curl.py"
    fake.write_text(fake_curl, encoding="utf-8")
    monkeypatch.setenv("NOVELAI_CURL_BIN", json.dumps([sys.executable, str(fake)]))
    task = {"index": 1, "title": "标题", "orientation": "portrait", "scene": "indoors",
            "chars": [{"name": "小艾", "costume": "dress", "action": "smile"}]}
    compiled = nai.compile_task(task, preset=PRESET, known_characters=KNOWN, seed=3)
    return asyncio.run(nai.generate_image(compiled, api_key=api_key, base_url="https://nai.test",
                                          workspace_root=tmp_path, settings=settings or _settings()))


def test_generate_saves_png_under_attachments(tmp_path: Path, monkeypatch):
    result = _generate(tmp_path, monkeypatch, FAKE_CURL_ZIP)
    assert result["path"].startswith("data/attachments/novelai/nai_") and result["path"].endswith(".png")
    assert (tmp_path / result["path"]).read_bytes().startswith(b"\x89PNG")
    assert result["attachment"]["name"] == Path(result["path"]).name
    assert result["seed"] == 3 and result["title"] == "标题"
    # 临时文件与锁都清理了
    tmp_dir = tmp_path / "data" / "temp" / "novelai"
    assert not (tmp_dir / "novelai_generate.lock").exists()
    assert not any(tmp_dir.glob("nai_*"))


def test_generate_retries_concurrent_lock(tmp_path: Path, monkeypatch):
    result = _generate(tmp_path, monkeypatch, FAKE_CURL_LOCK_THEN_OK)
    assert (tmp_path / result["path"]).exists()


def test_generate_http_error_and_missing_key(tmp_path: Path, monkeypatch):
    with pytest.raises(nai.NovelAIError) as info:
        _generate(tmp_path, monkeypatch, FAKE_CURL_400)
    assert info.value.code == "HTTP_ERROR" and info.value.status_code == "400" and "bad" in info.value.message
    with pytest.raises(nai.NovelAIError) as info:
        _generate(tmp_path, monkeypatch, FAKE_CURL_ZIP, api_key="")
    assert info.value.code == "API_KEY_MISSING"
