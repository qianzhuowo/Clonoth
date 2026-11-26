"""NovelAI 后端（移植自 LWB providers/novelai/compiler.js + shared/character-prompts.js；
请求执行复刻 tools/drawtools/nai_generate.py 的 curl / 重试 / 并发锁 / 落盘规则）。

编译（纯函数）：
- 角色提示词 = type → danbooru → appear → costume → action → interact；已录入角色用库里的
  type/danbooru/appearance，场景 type 非空时打头顶替库 type；costume/action 里与固定外貌重复的 tag 去掉。
- V4.5：scene 作 base_caption，角色进 char_captions（带 center）；V3：全部拼成一条。
- V5（移自 LWB novel-v5-request.js）：质量词和负面预设的文字由这里拼进提示词（已有的词不重复，`Text:` 段保留在最后）；
  只有 V5 Full 且负面预设不是 none 时在负向前加 nsfw；Full Medium 强制 14 步 / Euler Ancestral / 重缩放 0 / Heavy；
  每图最多 10 个角色；请求走 multipart，响应是 4 字节大端长度 + MessagePack 帧的流，取 event_type == final 的 image。
- 画幅：预设的 override_size 为 auto 时按 orientation 取 portrait 832x1216 / landscape 1216x832；
  为 default 时用预设 params 里的 width / height。调用方另传 override_size 时以它为准（auto 或五种标准尺寸，其它值忽略）。
执行（异步，subprocess 放线程）：本地队列锁串行、普通重试与并发锁重试分开计数、
2xx 响应解 zip / 裸 PNG、落盘 data/attachments/novelai/nai_<md5前10>_<uuid前6>.png。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..planner.contract import find_known_character, to_scene_character_prompt_tag
from ..presets import V5_MAX_CHARACTERS, V5_QUALITY_TEXT, V5_UC_TEXT, is_v5_model, locked_params

logger = logging.getLogger(__name__)

MAX_SEED = 0xFFFFFFFF
AUTO_ORIENTATION_SIZE = {"portrait": "832x1216", "landscape": "1216x832"}
_SIZE_RE = re.compile(r"^(832x1216|1216x832|1024x1024|768x1280|1280x768)$", re.IGNORECASE)
_WEIGHT_SYNTAX = re.compile(r"-?\d*\.?\d+::|::|[(){}\[\]]")
_WEIGHT_PREFIX = re.compile(r"-?\d*\.?\d+::|::")
_TAG_SPLIT = re.compile(r"[,，、]")
_TEXT_BLOCK = re.compile(r"\bText\s*:", re.IGNORECASE)
_NSFW_WORD = re.compile(r"\bnsfw\b", re.IGNORECASE)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# V5 帧流：单帧上限（中间图也是整张 PNG）和整个响应上限。
V5_MAX_FRAME_BYTES = 64 * 1024 * 1024
V5_MAX_STREAM_BYTES = 512 * 1024 * 1024


class NovelAIError(Exception):
    def __init__(self, message: str, *, code: str = "NOVELAI_ERROR", status_code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# ---- tag helpers ----

def join_tags(*parts: Any) -> str:
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        text = str(part).strip().replace("，", ",").replace("、", ",").strip(",").strip()
        if text:
            out.append(text)
    return ", ".join(out)


def _split_tags(text: Any) -> list[str]:
    return [t.strip() for t in _TAG_SPLIT.split(str(text or "")) if t.strip()]


def _tag_key(tag: str) -> str:
    return re.sub(r"\s+", " ", tag.lower().replace("_", " ")).strip()


def remove_duplicate_tags(text: Any, fixed_text: Any) -> str:
    """去掉 text 里与固定外貌重复的 tag，保持顺序只留首次；text 自带权重/括号语法的 tag 一律保留。"""
    fixed = {
        _tag_key(re.sub(r":-?\d*\.?\d+$", "", re.sub(r"[(){}\[\]]", "", tag)))
        for tag in _split_tags(_WEIGHT_PREFIX.sub("", str(fixed_text or "")))
    }
    seen: set[str] = set()
    kept: list[str] = []
    for tag in _split_tags(text):
        key = _tag_key(tag)
        if not _WEIGHT_SYNTAX.search(tag) and (key in fixed or key in seen):
            continue
        seen.add(key)
        kept.append(tag)
    return ", ".join(kept)


def format_danbooru_tag(tag: Any, *, preserve_canonical: bool = False) -> str:
    value = str(tag or "").strip()
    return value if preserve_canonical else value.replace("_", " ")


def _center(value: Any) -> dict[str, float]:
    if isinstance(value, Mapping):
        try:
            x, y = float(value.get("x")), float(value.get("y"))
        except (TypeError, ValueError):
            return {"x": 0.5, "y": 0.5}
        if 0 <= x <= 1 and 0 <= y <= 1:
            return {"x": x, "y": y}
    return {"x": 0.5, "y": 0.5}


def build_known_character_prompt(known: Mapping[str, Any]) -> str:
    return join_tags(
        to_scene_character_prompt_tag(known.get("type")),
        format_danbooru_tag(known.get("danbooru")),
        known.get("appearance"),
    )


def assemble_character_prompts(scene_characters: Sequence[Mapping[str, Any]] | None,
                               known_characters: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for character in scene_characters or []:
        if not isinstance(character, Mapping):
            continue
        known = find_known_character(character.get("name"), known_characters)
        center = _center(character.get("center"))
        if known:
            scene_type = to_scene_character_prompt_tag(character.get("type"))
            base = dict(known)
            if scene_type:
                base["type"] = ""
            fixed = join_tags(scene_type, build_known_character_prompt(base))
            out.append({
                "name": str(known.get("name") or character.get("name") or ""),
                "prompt": join_tags(
                    fixed,
                    remove_duplicate_tags(character.get("costume"), fixed),
                    remove_duplicate_tags(character.get("action"), fixed),
                    character.get("interact"),
                ),
                "uc": join_tags(known.get("negative_tags"), character.get("uc")),
                "center": center,
            })
            continue
        out.append({
            "name": str(character.get("name") or ""),
            "prompt": join_tags(
                to_scene_character_prompt_tag(character.get("type")),
                format_danbooru_tag(character.get("danbooru")),
                character.get("appear"),
                character.get("costume"),
                character.get("action"),
                character.get("interact"),
            ),
            "uc": str(character.get("uc") or ""),
            "center": center,
        })
    return out


# ---- request compile ----

def apply_size_override(params: Mapping[str, Any], override_size: Any, orientation: Any) -> dict[str, Any]:
    size = str(override_size or "").strip()
    if size.lower() == "auto":
        size = AUTO_ORIENTATION_SIZE.get(str(orientation or "").strip().lower(), "")
    match = _SIZE_RE.match(size)
    merged = dict(params)
    if match:
        width, height = (int(v) for v in match.group(1).lower().split("x"))
        merged["width"], merged["height"] = width, height
    return merged


def normalize_seed(value: Any) -> int:
    try:
        seed = int(value)
    except (TypeError, ValueError):
        seed = -1
    if seed < 0 or seed > MAX_SEED:
        seed = random.randint(0, MAX_SEED)
    return seed


# ---- V5 提示词拼接 ----

def _clean_part(value: Any) -> str:
    return str(value or "").strip().strip(",").strip()


def _join_parts(*values: Any) -> str:
    return ", ".join(p for p in (_clean_part(v) for v in values) if p)


def append_v5_automatic_prompt(scene: str, suffixes: Sequence[str]) -> str:
    """把质量词等自动词追加到场景后：已有的词（不分大小写）不重复；有 `Text:` 段时插在它前面，`Text:` 段另起一行留在最后。"""
    source = str(scene or "").strip()
    match = _TEXT_BLOCK.search(source)
    body = (source if match is None else source[:match.start()]).rstrip()
    existing = {_clean_part(p).lower() for p in body.split(",") if _clean_part(p)}
    words: list[str] = []
    for suffix in suffixes:
        for word in str(suffix or "").split(","):
            cleaned = _clean_part(word)
            if cleaned and cleaned.lower() not in existing:
                existing.add(cleaned.lower())
                words.append(cleaned)
    automatic = ", ".join(words)
    if not automatic:
        return source
    if match is None:
        return _join_parts(source, automatic)
    return f"{_join_parts(body, automatic)}\n{source[match.start():].lstrip()}".strip()


def build_v5_request_body(*, scene: str, character_prompts: Sequence[Mapping[str, Any]], negative_prompt: str,
                          params: Mapping[str, Any], seed: int) -> dict[str, Any]:
    model = str(params.get("model") or "").strip()
    if not is_v5_model(model):
        raise NovelAIError(f"NovelAI V5 请求不支持模型：{model}", code="V5_MODEL_UNSUPPORTED")
    if len(character_prompts) > V5_MAX_CHARACTERS:
        raise NovelAIError(f"NovelAI V5 每张图最多支持 {V5_MAX_CHARACTERS} 个角色提示词", code="V5_CHARACTER_LIMIT_EXCEEDED")
    merged = {**params, **locked_params(model)}
    width = int(merged.get("width") or 832)
    height = int(merged.get("height") or 1216)
    scale = float(merged.get("scale") if merged.get("scale") is not None else 6)
    steps = int(merged.get("steps") or 28)
    sampler = str(merged.get("sampler") or "k_euler_ancestral")
    scheduler = str(merged.get("scheduler") or merged.get("noise_schedule") or "karras")
    cfg_rescale = float(merged.get("cfg_rescale") or 0)
    quality_id = str(merged.get("v5QualityPresetId") or "")
    if quality_id not in V5_QUALITY_TEXT:
        quality_id = "standard"
    uc_id = str(merged.get("v5UcPresetId") or "")
    if uc_id not in V5_UC_TEXT:
        uc_id = "heavy"
    transparent = merged.get("transparentBackground") is True
    base_prompt = append_v5_automatic_prompt(scene, ["transparent background" if transparent else "", V5_QUALITY_TEXT[quality_id]])
    prepend_nsfw = model == "nai-diffusion-5-full" and uc_id != "none" and not _NSFW_WORD.search(base_prompt)
    negative = _join_parts("nsfw" if prepend_nsfw else "", V5_UC_TEXT[uc_id], negative_prompt)
    chars = [{"prompt": str(c.get("prompt") or ""), "uc": str(c.get("uc") or ""),
              "center": _center(c.get("center")), "enabled": True} for c in character_prompts]
    parameters: dict[str, Any] = {
        "params_version": 4, "width": width, "height": height, "scale": scale, "sampler": sampler, "steps": steps,
        "n_samples": 1, "ucPresetId": uc_id, "qualityPresetId": quality_id, "autoSmea": False, "dynamic_thresholding": False,
        "controlnet_strength": 1, "legacy": False, "add_original_image": True, "cfg_rescale": cfg_rescale,
        "legacy_v3_extend": False, "use_coords": True, "legacy_uc": False, "normalize_reference_strength_multiple": True,
        "inpaintImg2ImgStrength": 1, "seed": seed, "characterPrompts": chars, "straight_alpha": True,
    }
    if transparent:
        parameters["tag_hint_transparent_background"] = True
    parameters.update({
        "v4_prompt": {"caption": {"base_caption": base_prompt,
                                  "char_captions": [{"char_caption": c["prompt"], "centers": [c["center"]]} for c in chars]},
                      "use_coords": True, "use_order": True},
        "v4_negative_prompt": {"caption": {"base_caption": negative,
                                           "char_captions": [{"char_caption": c["uc"], "centers": [c["center"]]} for c in chars]},
                               "legacy_uc": False},
        "negative_prompt": negative, "deliberate_euler_ancestral_bug": False, "prefer_brownian": True,
        "noise_schedule": scheduler, "image_format": "png", "stream": "msgpack",
    })
    return {"input": base_prompt, "model": model, "action": "generate", "parameters": parameters, "use_new_shared_trial": True}


def parse_v5_stream(data: bytes) -> bytes:
    """V5 响应体 → final 帧里的 PNG。帧 = 4 字节大端长度 + MessagePack 对象；error 帧直接报错，intermediate 跳过。"""
    import msgpack

    if len(data) > V5_MAX_STREAM_BYTES:
        raise NovelAIError("NovelAI V5 响应超过大小限制", code="V5_STREAM_TOO_LARGE")
    pos = 0
    while pos + 4 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        pos += 4
        if not size or size > V5_MAX_FRAME_BYTES:
            raise NovelAIError("NovelAI V5 返回了无效的 MessagePack 帧长度", code="V5_BAD_FRAME")
        if pos + size > len(data):
            break
        try:
            frame = msgpack.unpackb(data[pos:pos + size], raw=False)
        except Exception as error:  # noqa: BLE001
            raise NovelAIError(f"NovelAI V5 MessagePack 解析失败：{error}", code="V5_BAD_FRAME") from error
        pos += size
        if not isinstance(frame, Mapping):
            raise NovelAIError("NovelAI V5 返回了无法识别的帧", code="V5_BAD_FRAME")
        event = str(frame.get("event_type") or "")
        if event == "error":
            raise NovelAIError(str(frame.get("message") or "NovelAI V5 生成失败"), code="V5_PROVIDER_ERROR")
        samp = frame.get("samp_ix")
        if samp not in (None, 0):
            raise NovelAIError(f"NovelAI V5 返回了意外的样本编号：{samp}", code="V5_BAD_FRAME")
        if event == "intermediate":
            continue
        if event != "final":
            raise NovelAIError(f"NovelAI V5 返回了未知事件：{event or '(empty)'}", code="V5_BAD_FRAME")
        image = frame.get("image")
        if not isinstance(image, (bytes, bytearray)) or not bytes(image).startswith(_PNG_MAGIC):
            raise NovelAIError("NovelAI V5 final 事件没有有效的 PNG 图片", code="V5_BAD_FRAME")
        return bytes(image)
    if pos != len(data):
        raise NovelAIError("NovelAI V5 响应在帧结束前被截断", code="V5_STREAM_TRUNCATED")
    raise NovelAIError("NovelAI V5 响应结束，但没有 final 图片", code="V5_NO_FINAL")


def build_request_body(*, scene: str, character_prompts: Sequence[Mapping[str, Any]], negative_prompt: str,
                       params: Mapping[str, Any], seed: int) -> dict[str, Any]:
    model = str(params.get("model") or "nai-diffusion-4-5-full").strip()
    if is_v5_model(model):
        return build_v5_request_body(scene=scene, character_prompts=character_prompts, negative_prompt=negative_prompt,
                                     params=params, seed=seed)
    width = int(params.get("width") or 832)
    height = int(params.get("height") or 1216)
    scale = float(params.get("scale", 6) if params.get("scale") is not None else 6)
    sampler = str(params.get("sampler") or "k_euler_ancestral")
    scheduler = str(params.get("scheduler") or params.get("noise_schedule") or "karras")
    steps = int(params.get("steps") or 28)
    chars = [{"prompt": str(c.get("prompt") or ""), "uc": str(c.get("uc") or ""),
              "center": _center(c.get("center")), "enabled": True} for c in character_prompts]
    is_v3 = "nai-diffusion-3" in model or "furry-3" in model
    is_v45 = "nai-diffusion-4-5" in model
    if is_v3:
        all_chars = ", ".join(c["prompt"] for c in chars if c["prompt"])
        full = f"{scene}, {all_chars}" if scene and all_chars else (scene or all_chars)
        negative = ", ".join(x for x in [negative_prompt, *(c["uc"] for c in chars)] if x)
        return {
            "action": "generate", "input": full, "model": model,
            "parameters": {
                "width": width, "height": height, "scale": scale, "seed": seed, "sampler": sampler,
                "noise_schedule": scheduler, "steps": steps, "n_samples": 1, "negative_prompt": negative,
                "ucPreset": int(params.get("ucPreset", 0) or 0), "qualityToggle": bool(params.get("qualityToggle", True)),
                "sm": bool(params.get("sm", False)), "sm_dyn": bool(params.get("sm_dyn", False)),
                "dynamic_thresholding": bool(params.get("decrisper", False)),
            },
        }
    use_coords = any(c["center"]["x"] != 0.5 or c["center"]["y"] != 0.5 for c in chars)
    skip_cfg = math.pow((width * height) / 1011712, 0.5) * 58 if is_v45 and params.get("variety_boost") else None
    return {
        "action": "generate", "input": scene or "", "model": model,
        "parameters": {
            "params_version": 3, "width": width, "height": height, "scale": scale, "seed": seed,
            "sampler": sampler, "noise_schedule": scheduler, "steps": steps, "n_samples": 1,
            "ucPreset": int(params.get("ucPreset", 0) or 0), "qualityToggle": bool(params.get("qualityToggle", True)),
            "autoSmea": bool(params.get("autoSmea", False)), "cfg_rescale": float(params.get("cfg_rescale", 0) or 0),
            "dynamic_thresholding": False, "controlnet_strength": 1, "legacy": False, "legacy_v3_extend": False,
            "use_coords": use_coords, "legacy_uc": False, "normalize_reference_strength_multiple": True,
            "deliberate_euler_ancestral_bug": False, "prefer_brownian": True, "image_format": "png",
            "skip_cfg_above_sigma": skip_cfg,
            "characterPrompts": chars,
            "v4_prompt": {"caption": {"base_caption": scene or "",
                                      "char_captions": [{"char_caption": c["prompt"], "centers": [c["center"]]} for c in chars]},
                          "use_coords": use_coords, "use_order": True},
            "v4_negative_prompt": {"caption": {"base_caption": negative_prompt or "",
                                               "char_captions": [{"char_caption": c["uc"], "centers": [c["center"]]} for c in chars]},
                                   "legacy_uc": False},
            "negative_prompt": negative_prompt or "",
        },
    }


def compile_task(task: Mapping[str, Any], *, preset: Mapping[str, Any], known_characters: Sequence[Mapping[str, Any]] | None,
                 seed: Any = None, override_size: Any = None) -> dict[str, Any]:
    """单张图：场景计划任务 + 预设 → {payload, prompt, negative_prompt, character_prompts, seed, width, height, title, orientation}。"""
    character_prompts = assemble_character_prompts(task.get("chars") or [], known_characters)
    scene = join_tags(preset.get("positive_prefix"), task.get("scene"), task.get("post_prompt"))
    negative = str(preset.get("negative_prefix") or "").strip()
    # 画幅：调用方传了 auto 或标准尺寸就用它，否则按预设自己的 override_size。
    size_mode = str(override_size or "").strip().lower()
    if size_mode != "auto" and not _SIZE_RE.match(size_mode):
        size_mode = str(preset.get("override_size") or "auto").strip().lower()
    params = dict(preset.get("params") or {})
    if size_mode != "default":  # default = 用预设 params 里的 width / height
        params = apply_size_override(params, size_mode, task.get("orientation"))
    if "width" not in params or "height" not in params:
        params = apply_size_override(params, "auto", task.get("orientation") or "portrait")
    # 种子：调用方 > 任务 > 预设；都没给（或预设是 -1）就随机。
    chosen_seed = seed if seed is not None else task.get("seed")
    resolved_seed = normalize_seed(chosen_seed if chosen_seed is not None else params.get("seed", -1))
    payload = build_request_body(scene=scene, character_prompts=character_prompts, negative_prompt=negative,
                                 params=params, seed=resolved_seed)
    return {
        "payload": payload, "prompt": scene, "negative_prompt": negative, "character_prompts": character_prompts,
        "seed": resolved_seed, "width": payload["parameters"]["width"], "height": payload["parameters"]["height"],
        "model": payload["model"], "title": str(task.get("title") or ""), "orientation": task.get("orientation") or "portrait",
        "index": task.get("index"), "transport": "v5" if is_v5_model(payload["model"]) else "json",
    }


# ---- execution ----

def _curl_command() -> list[str]:
    override = os.environ.get("NOVELAI_CURL_BIN", "").strip()
    if override.startswith("["):
        return [str(x) for x in json.loads(override)]
    return [override] if override else ["curl"]


def _should_retry(code: str, stderr: str) -> bool:
    if code in {"429", "500", "502", "503", "504"} or not code or code == "000":
        return True
    text = (stderr or "").lower()
    return any(k in text for k in ("timeout", "timed out", "connection", "reset"))


def _is_concurrent_lock(code: str, body: str) -> bool:
    return code == "429" and "concurrent generation is locked" in (body or "").lower()


def output_filename(prompt: str, seed: int) -> str:
    digest = hashlib.md5(f"{prompt}:{seed}".encode("utf-8")).hexdigest()[:10]
    return f"nai_{digest}_{uuid.uuid4().hex[:6]}.png"


def _acquire_lock(lock_path: Path, timeout_sec: float) -> int:
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd
        except FileExistsError:
            if time.time() - start >= timeout_sec:
                raise NovelAIError(f"NovelAI queue lock timeout after {timeout_sec}s", code="QUEUE_LOCK_TIMEOUT")
            try:
                if time.time() - lock_path.stat().st_mtime > max(timeout_sec, 3600):
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.2)


def _release_lock(fd: int | None, lock_path: Path) -> None:
    try:
        if fd is not None:
            os.close(fd)
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


_IMAGE_API_SUFFIX = re.compile(r"/ai/generate-image(?:-stream)?$", re.IGNORECASE)


def resolve_image_api(base_url: str, transport: str = "json") -> str:
    """出图接口地址（移自 LWB novel-request-config.js resolveNovelAIImageApi）：
    V5 的帧流走 /ai/generate-image-stream，其它模型走 /ai/generate-image；旧接口不认 parameters.stream。
    base_url 已经带了其中一个结尾时按 transport 换成对应的那个，? 和 # 之后的部分原样保留。"""
    endpoint = "generate-image-stream" if transport == "v5" else "generate-image"
    raw = str(base_url or "").strip() or "https://image.novelai.net"
    cut = re.search(r"[?#]", raw)
    path = (raw if cut is None else raw[:cut.start()]).rstrip("/")
    suffix = "" if cut is None else raw[cut.start():]
    path = _IMAGE_API_SUFFIX.sub(f"/ai/{endpoint}", path) if _IMAGE_API_SUFFIX.search(path) else f"{path}/ai/{endpoint}"
    return f"{path}{suffix}"


def _generate_sync(payload: Mapping[str, Any], *, api_key: str, base_url: str, workspace_root: Path,
                   settings: Mapping[str, Any], filename: str, transport: str = "json") -> dict[str, Any]:
    out_dir = workspace_root / "data" / "attachments" / "novelai"
    tmp_dir = workspace_root / "data" / "temp" / "novelai"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    tmp_zip = tmp_dir / f"nai_{token}.zip"
    tmp_payload = tmp_dir / f"nai_{token}.json"
    extract_dir = tmp_dir / f"out_{token}"
    lock_file = tmp_dir / "novelai_generate.lock"
    extract_dir.mkdir(parents=True, exist_ok=True)

    api_cfg = settings.get("api") if isinstance(settings.get("api"), Mapping) else {}
    gen_cfg = settings.get("generation") if isinstance(settings.get("generation"), Mapping) else {}
    timeout_sec = int(api_cfg.get("timeout_sec") or 130)
    retry_wait = float(gen_cfg.get("retry_wait_sec", 3) or 0)
    retry_max = int(gen_cfg.get("retry_max_attempts", 5) or 0)
    request_delay = float(gen_cfg.get("request_delay_sec", 0) or 0)
    lock_timeout = float(gen_cfg.get("lock_timeout_sec", 3600) or 3600)
    lock_retry_max = int(gen_cfg.get("lock_retry_max_attempts", 8) or 0)
    lock_min = float(gen_cfg.get("lock_retry_wait_min_sec", 6) or 0)
    lock_max = float(gen_cfg.get("lock_retry_wait_max_sec", 8) or 0)
    if lock_max < lock_min:
        lock_min, lock_max = lock_max, lock_min
    api_url = resolve_image_api(base_url, transport)
    curl = _curl_command()
    # V5：multipart 里一个叫 request 的 JSON 文件字段；其它模型直接发 JSON。
    if transport == "v5":
        body_args = ["-H", "Accept: application/octet-stream, */*",
                     "-F", f"request=@{tmp_payload};type=application/json;filename=blob"]
    else:
        body_args = ["-H", "Content-Type: application/json", "-d", f"@{tmp_payload}"]

    def error_body() -> str:
        try:
            return tmp_zip.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            return ""

    fd: int | None = None
    try:
        tmp_payload.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        fd = _acquire_lock(lock_file, lock_timeout)
        attempt = lock_attempt = 0
        last_error = ""
        for _ in range(retry_max + lock_retry_max + 2):
            tmp_zip.unlink(missing_ok=True)
            try:
                result = subprocess.run(
                    [*curl, "-s", "-m", str(timeout_sec), api_url,
                     "-H", f"Authorization: Bearer {api_key}", *body_args, "-o", str(tmp_zip), "-w", "%{http_code}"],
                    capture_output=True, text=True, timeout=timeout_sec + 10,
                )
            except subprocess.TimeoutExpired:
                last_error = f"NovelAI API request timed out after {timeout_sec}s"
                if attempt >= retry_max:
                    raise NovelAIError(last_error, code="REQUEST_TIMEOUT")
                attempt += 1
                time.sleep(retry_wait)
                continue
            except FileNotFoundError:
                raise NovelAIError("curl executable not found; NovelAI generation requires curl on PATH", code="CURL_MISSING")
            http_code = (result.stdout or "").strip()
            if len(http_code) == 3 and http_code.startswith("2"):
                break
            body = error_body()
            last_error = f"NovelAI API returned HTTP {http_code}: {body or result.stderr}"
            if _is_concurrent_lock(http_code, body):
                if lock_attempt >= lock_retry_max:
                    raise NovelAIError(last_error, code="CONCURRENT_LOCK", status_code=http_code)
                lock_attempt += 1
                time.sleep(random.uniform(lock_min, lock_max))
                continue
            if attempt >= retry_max or not _should_retry(http_code, result.stderr):
                raise NovelAIError(last_error, code="HTTP_ERROR", status_code=http_code)
            attempt += 1
            time.sleep(retry_wait)
        else:
            raise NovelAIError(last_error or "NovelAI API request failed", code="HTTP_ERROR")
        if request_delay > 0:
            time.sleep(request_delay)
        _release_lock(fd, lock_file)
        fd = None

        if not tmp_zip.exists() or tmp_zip.stat().st_size == 0:
            raise NovelAIError("NovelAI API returned an empty response body", code="EMPTY_BODY")
        if transport == "v5":
            raw = extract_dir / "image_0.png"
            raw.write_bytes(parse_v5_stream(tmp_zip.read_bytes()))
            images = [raw]
        elif zipfile.is_zipfile(tmp_zip):
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(extract_dir)
            images = sorted(p for p in extract_dir.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
        elif tmp_zip.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
            raw = extract_dir / "image_0.png"
            shutil.copyfile(tmp_zip, raw)
            images = [raw]
        else:
            raise NovelAIError(f"NovelAI API returned an unexpected body: {error_body() or 'not a zip/png'}", code="BAD_BODY")
        if not images:
            raise NovelAIError("No image found in NovelAI response zip", code="BAD_BODY")
        dst = out_dir / filename
        shutil.move(str(images[0]), str(dst))
        rel_path = dst.relative_to(workspace_root).as_posix()
        return {"path": rel_path, "attachment": {"type": "image", "path": rel_path, "mime_type": "image/png", "name": filename}}
    finally:
        _release_lock(fd, lock_file)
        try:
            tmp_zip.unlink(missing_ok=True)
            tmp_payload.unlink(missing_ok=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
        except OSError:
            pass


async def generate_image(compiled: Mapping[str, Any], *, api_key: str, base_url: str, workspace_root: Path,
                         settings: Mapping[str, Any]) -> dict[str, Any]:
    """执行一张已编译的请求；返回 {path, attachment, seed, width, height, prompt, negative_prompt, model, title}。"""
    if not api_key:
        raise NovelAIError("NovelAI API key is missing. Set NOVELAI_API_KEY or tools/drawtools/settings.yaml api.api_key",
                           code="API_KEY_MISSING")
    filename = output_filename(str(compiled.get("prompt") or ""), int(compiled.get("seed") or 0))
    saved = await asyncio.to_thread(
        _generate_sync, compiled["payload"], api_key=api_key, base_url=base_url or "https://image.novelai.net",
        workspace_root=Path(workspace_root), settings=settings, filename=filename,
        transport=str(compiled.get("transport") or ("v5" if is_v5_model(compiled.get("model")) else "json")),
    )
    return {
        **saved,
        "seed": compiled.get("seed"), "width": compiled.get("width"), "height": compiled.get("height"),
        "prompt": compiled.get("prompt"), "negative_prompt": compiled.get("negative_prompt"),
        "character_prompts": compiled.get("character_prompts"), "model": compiled.get("model"),
        "title": compiled.get("title"), "orientation": compiled.get("orientation"), "index": compiled.get("index"),
    }
