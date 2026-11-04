"""Node-level ``strip_images``: non-vision nodes drop image blocks before the provider call."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.attachments import node_strips_images, strip_image_blocks


def _node(**extra):
    return SimpleNamespace(extra=extra)


@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, False),
        ({"strip_images": True}, True),
        ({"no_vision": True}, True),
        ({"strip_images": "yes"}, True),
        ({"strip_images": "false"}, False),
        ({"strip_images": False}, False),
        ({"strip_images": 1}, False),
    ],
)
def test_node_strips_images_reads_extra(extra, expected):
    assert node_strips_images(_node(**extra)) is expected


def test_node_without_extra_does_not_strip():
    assert node_strips_images(SimpleNamespace()) is False
    assert node_strips_images(SimpleNamespace(extra=None)) is False


def test_strip_image_blocks_keeps_text_and_leaves_plain_messages_untouched():
    plain = {"role": "system", "content": "sys"}
    text_only_list = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    mixed = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    image_only = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
    multi_text = {
        "role": "user",
        "content": [
            {"type": "text", "text": "a"},
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "b"},
        ],
    }

    out = strip_image_blocks([plain, text_only_list, mixed, image_only, multi_text])

    assert out[0] is plain
    assert out[1] is text_only_list
    assert out[2] == {"role": "user", "content": "look"}
    assert out[3] == {"role": "user", "content": "[image content removed]"}
    assert out[4]["content"] == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    # The input list and its messages are not mutated.
    assert mixed["content"][1]["type"] == "image_url"


def test_fallback_provider_uses_shared_strip_function():
    import engine.builtin.fallback_provider as fb

    assert fb.strip_image_blocks is strip_image_blocks
    assert not hasattr(fb, "_strip_image_blocks")


def test_qq_orchestrator_declares_strip_images():
    from pathlib import Path

    from engine.node import load_node

    node = load_node(Path(__file__).resolve().parents[1], "qq.orchestrator")
    assert node is not None
    # Default reply model is vision-capable: images go in natively (read_media is the
    # fallback for text-only models, which set strip_images: true).
    assert node_strips_images(node) is False
