"""Images are task-scoped: history from other tasks carries paths, not image blocks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.runner import _message_to_history_dict, _strip_images_from_content  # noqa: E402
from engine.conversation_store import Message  # noqa: E402

_PATH = "data/attachments/qq_private_x/a.jpg"


def _content(annotated: bool = True) -> list[dict]:
    text = "看看这张图"
    if annotated:
        text += f"\n[Attached images: {_PATH}]"
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"file://{_PATH}"}},
    ]


def test_current_task_keeps_images():
    msg = Message(id="m1", role="user", content=_content(), source_task_id="task-a")
    d = _message_to_history_dict(msg, current_task_id="task-a")
    assert d["content"] == _content()
    assert "_stripped_images_count" not in d["_meta"]


def test_other_task_strips_images_and_keeps_path_once():
    msg = Message(id="m1", role="user", content=_content(), source_task_id="task-a")
    d = _message_to_history_dict(msg, current_task_id="task-b")
    assert isinstance(d["content"], str)
    assert "image_url" not in d["content"]
    assert d["content"].count(_PATH) == 1
    assert d["_meta"]["_stripped_images_count"] == 1
    assert d["_meta"]["source_task_id"] == "task-a"


def test_unannotated_image_gets_path_reference():
    out = _strip_images_from_content(_content(annotated=False))
    assert out == f"看看这张图\n[image: {_PATH}]"


def test_inline_image_marker():
    out = _strip_images_from_content([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    assert out == "[image: <inline>]"


def test_text_list_content_from_other_task_unchanged():
    content = [{"type": "text", "text": "hi"}]
    msg = Message(id="m1", role="user", content=content, source_task_id="task-a")
    assert _message_to_history_dict(msg, current_task_id="task-b")["content"] == content
