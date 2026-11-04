"""Image policy: node strip_images, model vision capability, and hand-off to the vision model."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import engine.vision as vision
from engine.attachments import build_multimodal_content


def _node(**extra):
    return SimpleNamespace(extra=extra)


def _write_caps(root, text: str) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "config.yaml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, None),
        ({"strip_images": True}, True),
        ({"no_vision": True}, True),
        ({"strip_images": "yes"}, True),
        ({"strip_images": "false"}, False),
        ({"strip_images": False}, False),
        ({"strip_images": 1}, None),
    ],
)
def test_node_strip_images_setting(extra, expected):
    assert vision.node_strip_images_setting(_node(**extra)) is expected


def test_node_without_extra_has_no_setting():
    assert vision.node_strip_images_setting(SimpleNamespace()) is None
    assert vision.node_strip_images_setting(SimpleNamespace(extra=None)) is None


def test_unlisted_models_can_see(tmp_path):
    assert vision.model_has_vision(tmp_path, "anything") is True
    _write_caps(tmp_path, "model_capabilities:\n  deepseek-flash: {vision: false}\n")
    assert vision.model_has_vision(tmp_path, "deepseek-flash") is False
    assert vision.model_has_vision(tmp_path, "LL/gemini-4-argon") is True


def test_images_allowed_rules(tmp_path):
    _write_caps(tmp_path, "model_capabilities:\n  text-only: {vision: false}\n")
    # Undeclared node follows the model.
    assert vision.images_allowed(_node(), "text-only", tmp_path) is False
    assert vision.images_allowed(_node(), "seer", tmp_path) is True
    # Declared node wins on the primary call.
    assert vision.images_allowed(_node(strip_images=True), "seer", tmp_path) is False
    assert vision.images_allowed(_node(strip_images=False), "text-only", tmp_path) is True
    # Replacement (fallback) call follows the replacement model only.
    assert vision.images_allowed(_node(strip_images=False), "text-only", tmp_path, replacement_call=True) is False
    assert vision.images_allowed(_node(strip_images=True), "seer", tmp_path, replacement_call=True) is True


def _image_msg(text: str, path: str) -> dict:
    return {"role": "user", "content": build_multimodal_content(text, [{"type": "image", "path": path}])}


def test_strip_and_describe_replaces_recent_images_and_caches(tmp_path, monkeypatch):
    for name in ("old.png", "a.png", "b.png"):
        (tmp_path / "data" / "attachments").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "attachments" / name).write_bytes(name.encode())
    calls: list[str] = []

    def fake_read_media(args, root):
        calls.append(args["path"])
        return {"ok": True, "data": {"description": f"desc of {args['path']}"}}

    monkeypatch.setattr(vision, "_read_media_fn", fake_read_media)
    messages = [
        {"role": "system", "content": "sys"},
        _image_msg("old", "data/attachments/old.png"),
        {"role": "assistant", "content": "ok"},
        _image_msg("one", "data/attachments/a.png"),
        _image_msg("two", "data/attachments/b.png"),
    ]
    out = asyncio.run(vision.strip_and_describe_images(messages, tmp_path))

    assert "image_url" not in str(out)
    assert sorted(calls) == ["data/attachments/a.png", "data/attachments/b.png"]  # old image not described
    assert "desc of data/attachments/b.png" in str(out[4]["content"])
    assert "[Attached images: data/attachments/old.png]" in str(out[1]["content"])
    assert out[0] is messages[0]
    assert messages[3]["content"][-1]["type"] == "image_url"  # input not mutated

    calls.clear()
    out2 = asyncio.run(vision.strip_and_describe_images(messages, tmp_path))
    assert calls == []  # served from cache
    assert out2 == out


def test_failed_description_is_reported(tmp_path, monkeypatch):
    (tmp_path / "data" / "attachments").mkdir(parents=True)
    (tmp_path / "data" / "attachments" / "x.png").write_bytes(b"x")

    def boom(args, root):
        raise RuntimeError("no key")

    monkeypatch.setattr(vision, "_read_media_fn", boom)
    out = asyncio.run(vision.strip_and_describe_images([_image_msg("hi", "data/attachments/x.png")], tmp_path))
    text = str(out[0]["content"])
    assert "识图模型识别失败" in text and "no key" in text
    assert "[Attached images: data/attachments/x.png]" in text
    assert not (tmp_path / "data" / "cache" / "vision").exists()


def test_fallback_provider_no_longer_strips_unconditionally():
    import inspect

    import engine.builtin.fallback_provider as fb

    src = inspect.getsource(fb)
    assert "strip_image_blocks" not in src
    assert "model_has_vision" in src


def test_qq_orchestrator_follows_model_vision():
    from pathlib import Path

    from engine.node import load_node

    node = load_node(Path(__file__).resolve().parents[1], "qq.orchestrator")
    assert node is not None
    assert vision.node_strip_images_setting(node) is None
