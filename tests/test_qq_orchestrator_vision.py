"""qq.orchestrator: text-only model that can still see images (read_media) and search."""
from __future__ import annotations

from pathlib import Path

import yaml

from engine.attachments import build_multimodal_content, strip_image_blocks

ROOT = Path(__file__).resolve().parents[1]


def _node() -> dict:
    return yaml.safe_load((ROOT / "config/nodes/qq.orchestrator.yaml").read_text(encoding="utf-8"))


def test_orchestrator_sees_images_natively_and_keeps_read_media_fallback():
    node = _node()
    assert node.get("strip_images") is False
    allow = node["tool_access"]["allow"]
    assert "read_media" in allow
    assert "search_web" in allow


def test_vision_routing_stays_disabled_by_default():
    cfg = yaml.safe_load((ROOT / "config/runtime.yaml").read_text(encoding="utf-8"))
    assert cfg["routing"]["vision"]["enabled"] is False


def test_image_path_survives_stripping_so_read_media_can_be_called():
    content = build_multimodal_content(
        "这是什么", [{"type": "image", "path": "data/attachments/x/pic.png"}]
    )
    stripped = strip_image_blocks([{"role": "user", "content": content}])[0]["content"]
    text = stripped if isinstance(stripped, str) else " ".join(p.get("text", "") for p in stripped)
    assert "image_url" not in str(stripped)
    assert "[Attached images: data/attachments/x/pic.png]" in text
