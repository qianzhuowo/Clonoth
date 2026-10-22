"""Regression tests for async dispatch_result attachment forwarding.

[2026-07-07] Why: generated images returned by async child nodes (NovelAI)
arrive at the caller as dispatch_result inbound attachments. The caller may finish
with only text, so the engine must distinguish those attachments from ordinary
user-uploaded images and keep them available for platform delivery.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.runner import _build_task_context  # noqa: E402
from engine.inference.pseudo_handlers import (  # noqa: E402
    _dispatch_result_input_attachments,
    _dispatch_result_attachments_already_sent,
    _reject_already_sent_dispatch_paths,
)


def test_build_task_context_exposes_dispatch_result_attachments() -> None:
    attachment = {
        "type": "image",
        "path": "data/attachments/novelai/example.png",
        "mime_type": "image/png",
        "name": "example.png",
    }
    ctx = _build_task_context({
        "task_context": {"conversation_key": "qq_group:1"},
        "inbound_message_type": "dispatch_result",
        "inbound_child_node_id": "draw.novelai_planner",
        "inbound_attachments_outbound_sent": True,
        "attachments": [attachment],
    })

    assert ctx["conversation_key"] == "qq_group:1"
    assert ctx["inbound_message_type"] == "dispatch_result"
    assert ctx["inbound_child_node_id"] == "draw.novelai_planner"
    assert ctx["inbound_attachments_outbound_sent"] is True
    assert ctx["inbound_attachments"] == [attachment]


def test_dispatch_result_input_attachments_ignores_normal_user_images() -> None:
    attachment = {"type": "image", "path": "data/attachments/user/example.png"}

    normal_ls = SimpleNamespace(rctx=SimpleNamespace(task_context={
        "inbound_message_type": "user_input",
        "inbound_attachments": [attachment],
    }))
    dispatch_ls = SimpleNamespace(rctx=SimpleNamespace(task_context={
        "inbound_message_type": "dispatch_result",
        "inbound_attachments": [attachment, "bad-payload"],
    }))
    already_sent_ls = SimpleNamespace(rctx=SimpleNamespace(task_context={
        "inbound_message_type": "dispatch_result",
        "inbound_attachments": [attachment],
        "inbound_attachments_outbound_sent": True,
    }))

    assert _dispatch_result_input_attachments(normal_ls) == []
    assert _dispatch_result_input_attachments(dispatch_ls) == [attachment]
    assert _dispatch_result_input_attachments(already_sent_ls) == []


def test_reject_already_sent_dispatch_paths_blocks_double_send() -> None:
    """[2026-07-08 v2] On a dispatch_result turn whose images were already
    auto-forwarded, reply()/finish() must not hand-send ANY attachment, even
    when the supervisor stripped inbound_attachments from the payload."""
    # attachments_outbound_sent=True but no inbound_attachments (real payload
    # shape produced by the supervisor when it strips the already-sent images).
    already_sent_ls = SimpleNamespace(rctx=SimpleNamespace(task_context={
        "inbound_message_type": "dispatch_result",
        "inbound_attachments_outbound_sent": True,
    }))
    normal_ls = SimpleNamespace(rctx=SimpleNamespace(task_context={
        "inbound_message_type": "user_input",
    }))

    assert _dispatch_result_attachments_already_sent(already_sent_ls) is True
    assert _dispatch_result_attachments_already_sent(normal_ls) is False

    # Any path the caller tries to re-send on this turn is dropped, whether it is
    # the raw novelai path or a bare basename.
    assert _reject_already_sent_dispatch_paths(
        already_sent_ls, ["data/attachments/novelai/nai_d126dda159_d2ce37.png"]
    ) == []
    assert _reject_already_sent_dispatch_paths(
        already_sent_ls, ["nai_d126dda159_d2ce37.png", "data/attachments/user/other.png"]
    ) == []

    # When nothing was auto-sent, paths pass through untouched.
    assert _reject_already_sent_dispatch_paths(
        normal_ls, ["nai_d126dda159_d2ce37.png"]
    ) == ["nai_d126dda159_d2ce37.png"]
