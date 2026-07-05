from __future__ import annotations

import re
from typing import Any

import yaml

from common import comma_join, load_settings, selected_preset, size_to_dimensions

GRID_X = {"A": 0.1, "B": 0.3, "C": 0.5, "D": 0.7, "E": 0.9}
GRID_Y = {"1": 0.1, "2": 0.3, "3": 0.5, "4": 0.7, "5": 0.9}


def strip_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:yaml|yml)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_plan(plan_text: str) -> dict[str, Any]:
    value = strip_code_fence(plan_text)
    data = yaml.safe_load(value)
    if not isinstance(data, dict):
        raise ValueError("plan must be a YAML object")
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("plan.images must be a non-empty list")
    return data


def center_from_grid(value: Any) -> dict[str, float]:
    text = str(value or "C3").strip().upper()
    if len(text) >= 2:
        x = GRID_X.get(text[0], 0.5)
        y = GRID_Y.get(text[1], 0.5)
        return {"x": x, "y": y}
    return {"x": 0.5, "y": 0.5}


def character_prompt(ch: dict[str, Any]) -> dict[str, Any]:
    parts = [
        ch.get("danbooru"),
        ch.get("type"),
        ch.get("appear"),
        ch.get("costume"),
        ch.get("action"),
        ch.get("interact"),
    ]
    return {
        "prompt": comma_join(*parts),
        "uc": comma_join(ch.get("uc")),
        "center": center_from_grid(ch.get("center")),
    }


def normalize_image_task(image: dict[str, Any], index: int, settings: dict[str, Any]) -> dict[str, Any]:
    preset_ref = str(image.get("preset") or image.get("preset_id") or image.get("style_preset") or "").strip()
    preset = selected_preset(settings, preset_ref)
    width, height, size_label = size_to_dimensions(str(image.get("size_label") or "竖图"), settings)
    params = dict(preset.get("params") or {})
    params["width"] = width
    params["height"] = height

    characters_raw = image.get("characters")
    characters = [c for c in characters_raw if isinstance(c, dict)] if isinstance(characters_raw, list) else []
    char_prompts = [character_prompt(c) for c in characters]

    scene = comma_join(preset.get("positive_prefix"), image.get("scene"))
    negative = comma_join(preset.get("negative_prefix"), image.get("negative_prompt"))
    char_negative = comma_join(*(cp.get("uc") for cp in char_prompts))
    if char_negative:
        negative = comma_join(negative, char_negative)

    return {
        "index": int(image.get("index") or index),
        "anchor": str(image.get("anchor") or "").strip(),
        "size_label": size_label,
        "width": width,
        "height": height,
        "prompt": scene,
        "negative_prompt": negative,
        "character_prompts": char_prompts,
        "params": params,
        "preset_id": str(preset.get("id") or ""),
        "preset_name": str(preset.get("name") or ""),
        "raw": image,
    }


def build_tasks_from_plan(plan_text: str) -> list[dict[str, Any]]:
    settings = load_settings()
    plan = parse_plan(plan_text)
    images = plan["images"]
    preset = selected_preset(settings)
    max_images = int(preset.get("max_images") or 0)
    if max_images > 0:
        images = images[:max_images]
    return [normalize_image_task(img, i + 1, settings) for i, img in enumerate(images) if isinstance(img, dict)]
