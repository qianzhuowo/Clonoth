"""Quoting a Bot-sent sticker shows its name instead of a bare placeholder."""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("nonebot")

_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

state = importlib.import_module("adapters.onebot.state")
history = importlib.import_module("adapters.onebot.history")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(state, "_sent_message_faces", type(state._sent_message_faces)())


def test_known_sticker_is_named():
    state.remember_sent_faces(101, ["旋覆打招呼"])
    assert history.label_bot_faces("[图片]", "101") == "[表情包: 旋覆打招呼]"


def test_multiple_and_extra_placeholders():
    state.remember_sent_faces("102", ["A", "B"])
    assert history.label_bot_faces("[图片][图片][图片]", 102) == "[表情包: A][表情包: B][表情包]"


def test_unknown_message_falls_back():
    assert history.label_bot_faces("hi [图片]", "999") == "hi [表情包]"


def test_registry_bounded_and_ignores_empty(monkeypatch):
    monkeypatch.setattr(state, "_SENT_FACE_MAX_ITEMS", 2)
    state.remember_sent_faces("", ["x"])
    state.remember_sent_faces("1", ["", " "])
    for i in range(3):
        state.remember_sent_faces(f"m{i}", ["n"])
    assert state.sent_faces("m0") == [] and state.sent_faces("m2") == ["n"]
    assert len(state._sent_message_faces) == 2


@pytest.mark.parametrize("marker", ["[表情:旋覆打招呼]", "[表情： 旋覆打招呼]", "[QQ_EMOJI:旋覆打招呼]", "[emoji:旋覆打招呼]"])
def test_bot_reply_history_shows_sticker_record(monkeypatch, marker):
    from collections import defaultdict, deque
    monkeypatch.setattr(state, "_group_history", defaultdict(lambda: deque(maxlen=20)))
    monkeypatch.setattr(state, "_group_content_records", defaultdict(lambda: deque(maxlen=20)))
    history._record_bot_reply(8888889, f"来啦~ {marker}")
    line = state._group_history[8888889][-1]
    assert line.endswith("Bot: 来啦~ [表情包: 旋覆打招呼]")
    # The history form is a record, not a send instruction.
    from adapters.onebot.emoji_handler import _QQ_EMOJI_RE
    assert not _QQ_EMOJI_RE.search(line)
