"""绘图参数预设的规则：默认值、面板选项表、字段清洗与校验、和小白X 参数预设文件互转。

预设存在 settings.yaml 的 params.presets[]：
  id / name / aliases / positive_prefix / negative_prefix / max_images / max_characters_per_image
  override_size：auto = 按每张图的构图方向取竖图或横图；default = 用 params.width / params.height
  params：model / sampler / scheduler / steps / scale / seed / width / height / qualityToggle / ucPreset /
          autoSmea / cfg_rescale / variety_boost / sm / sm_dyn / decrisper /
          v5QualityPresetId / v5UcPresetId / transparentBackground（只对 V5 模型有效）

clean_preset 有两种用法：
- strict=True（面板保存、导出）：值不合法就抛 PresetError，说明是哪套预设的哪个字段。
- strict=False（读旧数据、导入文件）：不合法的值换成能用的值，并往 notes 里记一句。
没给的字段用 DEFAULT_PRESET 补齐；这里不认识的键原样保留（旧工具的键、以后才用到的键）。

本模块不 import 插件里的其它模块，agent / backends / admin 都可以引用。
"""
from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

# 与 tools/drawtools/common.py 的 DEFAULT_PRESET 同义。
DEFAULT_PRESET: dict[str, Any] = {
    "id": "default-v45-full", "name": "默认 (V4.5 Full)",
    "positive_prefix": "best quality, amazing quality, very aesthetic, absurdres",
    "negative_prefix": "lowres, bad anatomy, bad hands, missing fingers, extra digits, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry",
    "max_images": 0, "max_characters_per_image": 4,
    "params": {"model": "nai-diffusion-4-5-full", "sampler": "k_euler_ancestral", "scheduler": "karras", "steps": 28, "scale": 6,
               "seed": -1, "qualityToggle": True, "autoSmea": False, "ucPreset": 0, "cfg_rescale": 0, "variety_boost": False,
               "sm": False, "sm_dyn": False, "decrisper": False},
}

# 面板下拉里的选项；不在表里的值走“自定义”，照样能保存。
MODELS = (("nai-diffusion-5-full", "NAI V5 Full"), ("nai-diffusion-5-full-medium", "NAI V5 Full Medium（省点·14 步）"),
          ("nai-diffusion-5-curated", "NAI V5 Curated"),
          ("nai-diffusion-4-5-full", "NAI V4.5 Full"), ("nai-diffusion-4-5-curated", "NAI V4.5 Curated"),
          ("nai-diffusion-4-full", "NAI V4 Full"), ("nai-diffusion-3", "NAI V3"), ("nai-diffusion-furry-3", "Furry V3"))
# V5 走另一套传输协议（multipart 请求 + msgpack 帧流），质量词和负面预设由我们拼进提示词；只认这三个 id。
V5_MODELS = ("nai-diffusion-5-full", "nai-diffusion-5-full-medium", "nai-diffusion-5-curated")
V5_MAX_CHARACTERS = 10
# V5 的质量词 / 负面预设：文字由客户端拼接（移自小白X novel-v5-request.js）。
V5_QUALITY_PRESETS = (("standard", "Standard"), ("light", "Light"), ("none", "None"))
V5_QUALITY_TEXT = {"standard": "very aesthetic, masterpiece, no text", "light": "very aesthetic, amazing quality, no text", "none": ""}
V5_UC_PRESETS = (("heavy", "Heavy"), ("light", "Light"), ("humanFocus", "Human Focus"), ("furryFocus", "Furry Focus"), ("none", "None"))
_V5_UC_HEAVY = ("lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, "
                "chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page")
V5_UC_TEXT = {
    "heavy": _V5_UC_HEAVY,
    "light": "lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, very displeasing, jpeg artifacts, 0::ai-generated::",
    "humanFocus": _V5_UC_HEAVY + ", @_@, mismatched pupils, glowing eyes, bad anatomy",
    "furryFocus": ("{worst quality}, distracting watermark, unfinished, bad quality, {widescreen}, upscale, {sequence}, {{grandfathered content}}, "
                   "blurred foreground, chromatic aberration, sketch, everyone, [sketch background], simple, [flat colors], ych (character), "
                   "outline, multiple scenes, [[horror (theme)]], comic"),
    "none": "",
}
# 旧预设没写 V5 字段时按 ucPreset 推导（小白X 的迁移规则）。
_UC_TO_V5 = {0: "heavy", 1: "light", 2: "humanFocus", 3: "none"}
# V5 Full Medium 是蒸馏模型，这几项固定；面板锁住，后端编译请求时也强制。
V5_MEDIUM_MODEL = "nai-diffusion-5-full-medium"
V5_MEDIUM_LOCK: dict[str, Any] = {"steps": 14, "sampler": "k_euler_ancestral", "cfg_rescale": 0, "v5UcPresetId": "heavy"}
SAMPLERS = (("k_euler_ancestral", "Euler Ancestral"), ("k_euler", "Euler"), ("k_dpmpp_2m", "DPM++ 2M"),
            ("k_dpmpp_2s_ancestral", "DPM++ 2S Ancestral"), ("k_dpmpp_sde", "DPM++ SDE"), ("ddim", "DDIM"))
SCHEDULERS = (("native", "Native"), ("karras", "Karras"), ("exponential", "Exponential"), ("polyexponential", "Polyexponential"))
SIZES = (("832x1216", "832×1216 竖图"), ("1216x832", "1216×832 横图"), ("1024x1024", "1024×1024 方图"),
         ("768x1280", "768×1280 竖图"), ("1280x768", "1280×768 横图"))
UC_PRESETS = ((0, "Heavy"), (1, "Light"), (2, "Human Focus"), (3, "None"))
BOOL_PARAMS = (("qualityToggle", "质量增强"), ("autoSmea", "自动 SMEA"), ("variety_boost", "多样性增强"),
               ("sm", "SMEA"), ("sm_dyn", "SMEA 动态"), ("decrisper", "Decrisper"))

NAME_MAX = 64
MAX_SEED = 0xFFFFFFFF
LIMITS: dict[str, tuple[int, int]] = {
    "steps": (1, 50), "scale": (0, 10), "cfg_rescale": (0, 1), "max_images": (0, 16), "max_characters_per_image": (0, 10),
}
DIMENSION_MIN, DIMENSION_MAX, DIMENSION_STEP = 64, 2048, 64
# 面板只在超出这两个值时提示“可能扣点数”，不参与校验。
FREE_PIXELS, FREE_STEPS = 1024 * 1024, 28

EXPORT_TYPE = "novel-draw-preset"
EXPORT_VERSION = 2
# 小白X 的 DEFAULT_PARAMS：导入的文件选了固定尺寸却没带宽高时用它。
IMPORT_DEFAULT_SIZE = (1216, 832)

_STANDARD_SIZES = {value: tuple(int(v) for v in value.split("x")) for value, _ in SIZES}
_ALIAS_SPLIT = re.compile(r"[,，]")


class PresetError(ValueError):
    pass


def is_v5_model(model: Any) -> bool:
    return str(model or "").strip() in V5_MODELS


def locked_params(model: Any) -> dict[str, Any]:
    """这个模型下固定不可改的参数（目前只有 V5 Full Medium）；其它模型返回空。"""
    return dict(V5_MEDIUM_LOCK) if str(model or "").strip() == V5_MEDIUM_MODEL else {}


def effective_max_characters(preset: Mapping[str, Any]) -> int:
    """规划时每张图的角色上限：V5 有硬上限 10（填 0 就是 10），其它模型按预设（0 = 不限）。"""
    try:
        user = int(preset.get("max_characters_per_image") or 0)
    except (TypeError, ValueError):
        user = 0
    params = preset.get("params") if isinstance(preset.get("params"), Mapping) else {}
    if is_v5_model(params.get("model")):
        return min(user, V5_MAX_CHARACTERS) if user > 0 else V5_MAX_CHARACTERS
    return max(user, 0)


def new_preset_id() -> str:
    return "preset-" + uuid.uuid4().hex[:8]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _tidy(number: float) -> int | float:
    return int(number) if number == int(number) else round(number, 4)


def parse_size(value: Any) -> tuple[int, int] | None:
    """五种标准尺寸之一（如 "1024x1024"）→ (宽, 高)；其它值返回 None。"""
    return _STANDARD_SIZES.get(str(value or "").strip().lower())


def as_dimensions(width: Any, height: Any) -> tuple[int, int] | None:
    """宽高都是 DIMENSION_MIN–DIMENSION_MAX 之间 64 的倍数时返回整数对，否则 None。"""
    out: list[int] = []
    for value in (width, height):
        number = _number(value)
        if number is None or number != int(number):
            return None
        size = int(number)
        if size < DIMENSION_MIN or size > DIMENSION_MAX or size % DIMENSION_STEP:
            return None
        out.append(size)
    return out[0], out[1]


class _Cleaner:
    def __init__(self, label: str, strict: bool, notes: list[str] | None) -> None:
        self.label = label
        self.strict = strict
        self.notes = notes

    def bad(self, field: str, rule: str, fallback: Any) -> Any:
        if self.strict:
            raise PresetError(f"预设「{self.label}」的{field}{rule}")
        if self.notes is not None:
            if isinstance(fallback, bool):
                shown: Any = "开" if fallback else "关"
            else:
                shown = "空" if fallback in ("", [], {}) else fallback
            self.notes.append(f"{field}{rule}".strip() + f"，已改为 {shown}")
        return fallback

    def text(self, field: str, value: Any, default: str) -> str:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return self.bad(field, "必须是文字", default)
        return str(value).strip()

    def required_text(self, field: str, value: Any, default: str) -> str:
        text = self.text(field, value, default)
        return text or self.bad(field, "不能为空", default)

    def _ranged(self, field: str, value: Any, lo: int, hi: int, default: Any, whole: bool, rule: str = "") -> Any:
        if value is None or value == "":
            return default
        rule = rule or f"必须是 {lo}–{hi} 的{'整数' if whole else '数字'}"
        number = _number(value)
        if number is None or (whole and number != int(number)):
            return self.bad(field, rule, default)
        if number < lo or number > hi:
            return self.bad(field, rule, _tidy(min(max(number, lo), hi)))
        return _tidy(number)

    def integer(self, field: str, value: Any, lo: int, hi: int, default: int, rule: str = "") -> int:
        return self._ranged(field, value, lo, hi, default, True, rule)

    def number(self, field: str, value: Any, lo: int, hi: int, default: Any) -> Any:
        return self._ranged(field, value, lo, hi, default, False)

    def choice(self, field: str, value: Any, allowed: tuple[int, ...], default: int) -> int:
        if value is None or value == "":
            return default
        number = _number(value)
        if number is None or number not in allowed:
            return self.bad(field, "只能是 " + " / ".join(str(a) for a in allowed), default)
        return int(number)

    def word(self, field: str, value: Any, allowed: tuple[str, ...], default: str) -> str:
        if value is None or value == "":
            return default
        text = str(value).strip() if isinstance(value, str) else ""
        if text not in allowed:
            return self.bad(field, "只能是 " + " / ".join(allowed), default)
        return text

    def flag(self, field: str, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        return self.bad(field, "只能是开或关", default)


def clean_preset(raw: Any, *, strict: bool = True, legacy_size: Any = "auto", notes: list[str] | None = None) -> dict[str, Any]:
    """清洗一套预设。legacy_size：预设自己没写 override_size 时沿用的旧全局设置（planner.override_size）。"""
    if not isinstance(raw, Mapping):
        raise PresetError("预设必须是对象")
    preset_id = str(raw.get("id") or "").strip()
    if not preset_id:
        if strict:
            raise PresetError("预设缺少 id")
        preset_id = new_preset_id()
    clean = _Cleaner(str(raw.get("name") or "").strip() or preset_id, strict, notes)
    if len(preset_id) > NAME_MAX:
        preset_id = clean.bad(" id ", f"不能超过 {NAME_MAX} 个字", preset_id[:NAME_MAX])
    out = dict(raw)
    out["id"] = preset_id

    name = clean.text("名称", raw.get("name"), "")
    if len(name) > NAME_MAX:
        name = clean.bad("名称", f"不能超过 {NAME_MAX} 个字", name[:NAME_MAX])
    out["name"] = name or preset_id

    aliases = raw.get("aliases")
    if aliases is None:
        aliases = []
    elif isinstance(aliases, str):
        aliases = _ALIAS_SPLIT.split(aliases)
    elif not isinstance(aliases, (list, tuple)):
        aliases = clean.bad("别名", "必须是列表", [])
    seen: set[str] = set()
    out["aliases"] = []
    for alias in aliases:
        text = str(alias).strip() if isinstance(alias, (str, int, float)) and not isinstance(alias, bool) else ""
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out["aliases"].append(text)

    out["positive_prefix"] = clean.text("正向固定", raw.get("positive_prefix"), DEFAULT_PRESET["positive_prefix"])
    out["negative_prefix"] = clean.text("负向固定", raw.get("negative_prefix"), DEFAULT_PRESET["negative_prefix"])
    for key, field in (("max_images", "要求出图数"), ("max_characters_per_image", "每张图最多角色数")):
        out[key] = clean.integer(field, raw.get(key), *LIMITS[key], DEFAULT_PRESET[key])

    source = raw.get("params")
    if source is None:
        source = {}
    elif not isinstance(source, Mapping):
        source = clean.bad(" params ", "必须是对象", {})
    defaults = DEFAULT_PRESET["params"]
    params = dict(source)
    params["model"] = clean.required_text("模型", source.get("model"), defaults["model"])
    params["sampler"] = clean.required_text("采样器", source.get("sampler"), defaults["sampler"])
    scheduler = source.get("scheduler") if source.get("scheduler") is not None else source.get("noise_schedule")
    params["scheduler"] = clean.required_text("调度器", scheduler, defaults["scheduler"])
    params["steps"] = clean.integer("步数", source.get("steps"), *LIMITS["steps"], defaults["steps"])
    params["scale"] = clean.number(" CFG ", source.get("scale"), *LIMITS["scale"], defaults["scale"])
    params["seed"] = clean.integer("种子", source.get("seed"), -1, MAX_SEED, -1, f"必须是 -1（随机）或 0–{MAX_SEED} 的整数")
    params["cfg_rescale"] = clean.number(" CFG 重缩放", source.get("cfg_rescale"), *LIMITS["cfg_rescale"], defaults["cfg_rescale"])
    params["ucPreset"] = clean.choice("负面预设", source.get("ucPreset"), tuple(v for v, _ in UC_PRESETS), defaults["ucPreset"])
    for key, field in BOOL_PARAMS:
        params[key] = clean.flag(field, source.get(key), bool(defaults[key]))
    # V5 字段：没写就按旧开关推导，和小白X 迁移旧预设的规则一致。
    params["v5QualityPresetId"] = clean.word(" V5 质量预设", source.get("v5QualityPresetId"), tuple(v for v, _ in V5_QUALITY_PRESETS),
                                             "none" if params["qualityToggle"] is False else "standard")
    params["v5UcPresetId"] = clean.word(" V5 负面预设", source.get("v5UcPresetId"), tuple(v for v, _ in V5_UC_PRESETS),
                                        _UC_TO_V5.get(params["ucPreset"], "heavy"))
    params["transparentBackground"] = clean.flag("透明背景", source.get("transparentBackground"), False)
    # Medium 的固定参数：不管写了什么都改回固定值，不算错误。
    params.update(locked_params(params["model"]))

    mode = raw.get("override_size")
    if mode is None or mode == "":  # 预设自己没写：旧全局设置是固定尺寸就沿用，其它一律按 auto
        mode = legacy_size if parse_size(legacy_size) else "auto"
    mode = str(mode).strip().lower()
    size = as_dimensions(source.get("width"), source.get("height"))
    fixed = parse_size(mode)
    if fixed:  # 旧写法：直接写了一个固定尺寸，它一直优先于预设自己的宽高
        mode, size = "default", fixed
    if mode == "default" and size is None:
        mode = clean.bad("尺寸", f"选了固定值，但宽高不是 {DIMENSION_MIN}–{DIMENSION_MAX} 之间 {DIMENSION_STEP} 的倍数", "auto")
    elif mode not in ("auto", "default"):
        mode = clean.bad("尺寸", "只能是 auto 或 default", "auto")
    out["override_size"] = mode
    params.pop("width", None)
    params.pop("height", None)
    if size is not None:
        params["width"], params["height"] = size
    out["params"] = params
    return out


def panel_options() -> dict[str, Any]:
    """面板下拉选项和数值范围，与 clean_preset 的校验同源。"""
    def pairs(items: tuple[tuple[Any, str], ...]) -> list[dict[str, Any]]:
        return [{"value": value, "label": label} for value, label in items]

    return {
        "models": pairs(MODELS), "v5_models": list(V5_MODELS), "samplers": pairs(SAMPLERS), "schedulers": pairs(SCHEDULERS),
        "sizes": pairs(SIZES), "uc_presets": pairs(UC_PRESETS), "limits": {key: list(value) for key, value in LIMITS.items()},
        "v5_quality_presets": pairs(V5_QUALITY_PRESETS), "v5_uc_presets": pairs(V5_UC_PRESETS),
        "v5_max_characters": V5_MAX_CHARACTERS, "locked": {V5_MEDIUM_MODEL: dict(V5_MEDIUM_LOCK)},
        "dimension": {"min": DIMENSION_MIN, "max": DIMENSION_MAX, "step": DIMENSION_STEP},
        "free": {"pixels": FREE_PIXELS, "steps": FREE_STEPS}, "name_max": NAME_MAX, "max_seed": MAX_SEED,
        # “+ 新预设”的初始内容
        "blank": clean_preset(DEFAULT_PRESET, strict=False),
    }


# ---- 小白X 参数预设文件（novel-draw-preset v1 / v2） ----

def export_preset(raw: Any) -> dict[str, Any]:
    preset = clean_preset(raw, strict=True)
    return {
        "type": EXPORT_TYPE, "version": EXPORT_VERSION,
        "exportDate": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "name": preset["name"], "author": "", "简介": "",
        "preset": {
            "positivePrefix": preset["positive_prefix"], "negativePrefix": preset["negative_prefix"],
            "maxImages": preset["max_images"], "maxCharactersPerImage": preset["max_characters_per_image"],
            "overrideSize": preset["override_size"], "params": dict(preset["params"]),
        },
    }


def import_preset(data: Any) -> tuple[dict[str, Any], list[str]]:
    """文件内容 → (预设, 说明)。预设拿到一个新 id，不写盘；Vibe 参考图和缩略图不导入。"""
    if not isinstance(data, Mapping) or data.get("type") != EXPORT_TYPE or not isinstance(data.get("preset"), Mapping):
        raise PresetError(f"不是参数预设文件（需要 type 为 {EXPORT_TYPE}，并带 preset）")
    version = _number(data.get("version"))
    if version not in (1, 2):
        raise PresetError(f"不支持的参数预设版本：{data.get('version', '缺失')}")
    source = data["preset"]
    notes: list[str] = []
    params = dict(source["params"]) if isinstance(source.get("params"), Mapping) else {}
    fixed_size = source.get("overrideSize") != "auto"
    if fixed_size and params.get("width") is None and params.get("height") is None:
        params["width"], params["height"] = IMPORT_DEFAULT_SIZE
    raw = {
        "id": new_preset_id(),
        "name": str(data.get("name") or source.get("name") or "导入的预设"),
        "positive_prefix": str(source.get("positivePrefix") or ""),
        "negative_prefix": str(source.get("negativePrefix") or ""),
        # v1 文件没有数量限制
        "max_images": 0 if version == 1 else (source.get("maxImages") or 0),
        "max_characters_per_image": 0 if version == 1 else (source.get("maxCharactersPerImage") or 0),
        "override_size": "default" if fixed_size else "auto",
        "params": params,
    }
    preset = clean_preset(raw, strict=False, notes=notes)
    if source.get("vibe") is not None:
        notes.append("文件里的 Vibe 参考图没有导入")
    return preset, notes
