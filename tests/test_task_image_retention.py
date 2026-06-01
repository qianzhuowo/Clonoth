"""Regression tests for task-scoped multimodal history retention.

[AutoC 2026-06-01] Why: image attachments used to be stripped from every
ConversationStore reload, which made a later step in the same task unable to see
the original image. How: these tests assert that only cross-task history is
reduced to text and that multimodal list content survives JSONL and shadow
writes. Purpose: protect the task-local image locking behavior requested for the
runner.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.inference.ai_step import _shadow_write  # noqa: E402
from engine.runner import _message_to_history_dict  # noqa: E402


def _image_content() -> list[dict[str, object]]:
    """Build a minimal multimodal content list used by all regression cases."""
    return [
        {"type": "text", "text": "please inspect this image"},
        {"type": "image_url", "image_url": {"url": "file:///tmp/example.png"}},
    ]


def test_message_to_history_dict_keeps_current_task_images_and_strips_others() -> None:
    """Current task history keeps images; external or other task history strips them."""
    message = Message(
        id="msg_1",
        role="user",
        content=_image_content(),
        source_task_id="task-a",
    )

    same_task = _message_to_history_dict(message, current_task_id="task-a")
    other_task = _message_to_history_dict(message, current_task_id="task-b")
    default_call = _message_to_history_dict(message)

    assert same_task["content"] == _image_content()
    assert same_task["_meta"]["source_task_id"] == "task-a"
    assert other_task["content"] == "please inspect this image\n[图片附件已省略]"
    assert default_call["content"] == "please inspect this image\n[图片附件已省略]"


def test_conversation_store_round_trips_multimodal_content(tmp_path: Path) -> None:
    """ConversationStore must serialize and reload list content without stringifying it."""
    store = ConversationStore(tmp_path)
    store.append(
        "session-a",
        Message(
            id="msg_1",
            role="user",
            content=_image_content(),
            message_type=MessageType.USER_INPUT,
            source_task_id="task-a",
        ),
    )

    loaded = store.load("session-a")

    assert loaded[0].content == _image_content()


def test_shadow_write_preserves_multimodal_content(tmp_path: Path) -> None:
    """Shadow writes must keep list content so a task reload can still see images."""
    store = ConversationStore(tmp_path)
    rctx = SimpleNamespace(
        conversation_store=store,
        child_session_id="",
        session_id="session-a",
        task_id="task-a",
        first_shadow_message_id="",
        last_shadow_message_id="",
    )
    loop_state = SimpleNamespace(
        rctx=rctx,
        node=SimpleNamespace(id="node-a"),
        last_shadow_message_id="",
    )

    _shadow_write(
        loop_state,
        {"role": "user", "content": _image_content()},
        MessageType.USER_INPUT,
    )

    loaded = store.load("session-a")
    assert loaded[0].content == _image_content()
