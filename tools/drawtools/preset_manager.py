from __future__ import annotations

"""Drawtools preset management utility for QQ command routing."""

import sys
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import iter_presets, load_settings, set_selected_preset  # noqa: E402


def list_presets() -> list[dict[str, Any]]:
    settings = load_settings()
    selected = str((settings.get("params") or {}).get("selected_preset_id") or "")
    result = []
    for preset in iter_presets(settings):
        item = {
            "id": str(preset.get("id") or ""),
            "name": str(preset.get("name") or ""),
            "selected": str(preset.get("id") or "") == selected,
            "model": str((preset.get("params") or {}).get("model") or ""),
            "scale": (preset.get("params") or {}).get("scale"),
            "steps": (preset.get("params") or {}).get("steps"),
        }
        result.append(item)
    return result


def switch_preset(preset_ref: str) -> dict[str, Any]:
    return set_selected_preset(preset_ref)
