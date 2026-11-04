"""qq.orchestrator: text-only model that can still see images (read_media) and search."""
from __future__ import annotations

from pathlib import Path

import yaml

from engine.attachments import build_multimodal_content

ROOT = Path(__file__).resolve().parents[1]


def _node() -> dict:
    return yaml.safe_load((ROOT / "config/nodes/qq.orchestrator.yaml").read_text(encoding="utf-8"))


def test_orchestrator_sees_images_natively_and_keeps_read_media_fallback():
    node = _node()
    assert "strip_images" not in node  # follows the reply model's vision capability
    allow = node["tool_access"]["allow"]
    assert "read_media" in allow
    assert "search_web" in allow


def test_vision_routing_stays_disabled_by_default():
    cfg = yaml.safe_load((ROOT / "config/runtime.yaml").read_text(encoding="utf-8"))
    assert cfg["routing"]["vision"]["enabled"] is False
