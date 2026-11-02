"""Tests for QQ custom faces (fake NapCat bot, no network)."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

cf = importlib.import_module("adapters.onebot.custom_faces")
eh = importlib.import_module("adapters.onebot.emoji_handler")
ia = importlib.import_module("adapters.onebot.inbound_attachments")

KEY = "qq_group:abcdef0123456789abcdef01"


class FakeNapCat:
    def __init__(self, faces):
        self.faces = [dict(f) for f in faces]
        self.calls = []

    async def call_api(self, api, **kw):
        self.calls.append((api, kw))
        if api == "fetch_custom_face_detail":
            return {"data": {"emojiInfoList": [dict(f) for f in self.faces]}}
        if api == "set_custom_face_desc":
            for f in self.faces:
                if f.get("resId") == kw["res_id"]:
                    f["desc"] = kw["desc"]
            return {}
        if api == "delete_custom_face":
            self.faces = [f for f in self.faces if f.get("resId") != kw["res_id"]]
            return {}
        if api == "add_custom_face":
            self.faces.append({"emojiId": 9, "resId": "r9", "md5": kw["md5"], "desc": ""})
            return {}
        raise AssertionError(api)


FACES = [
    {"emojiId": 1, "resId": "r1", "md5": "aaaaaaaa11", "desc": "开心"},
    {"emojiId": 2, "resId": "r2", "md5": "bbbbbbbb22", "desc": "开心"},
    {"emojiId": 3, "resId": "r3", "md5": "cccccccc33", "desc": ""},
    {"emojiId": 0, "resId": "r4", "md5": "dddddddd44", "desc": "难过"},
]


@pytest.fixture
def setup(tmp_path):
    eh.invalidate_custom_face_cache()
    faces = cf.CustomFaces(
        names_path=str(tmp_path / "faces.txt"),
        metadata_path=str(tmp_path / "faces.json"),
        workspace=tmp_path,
        sync_settle_seconds=0,
    )
    recent = ia.RecentImages(ia.ReplyAttachmentCache(tmp_path / "r.json"))
    bot = FakeNapCat(FACES)
    event = types.SimpleNamespace(user_id="1111111", message_id="m1", raw_message=None, get_message=lambda: [])

    def run(text, *, admin=True, current=None):
        return asyncio.run(faces.handle_command(
            bot=bot, event=event, text=text, conversation_key=KEY,
            current_attachments=current or [], recent=recent, is_admin=admin,
        ))

    return faces, bot, run, tmp_path


def test_non_command_and_admin_only(setup):
    faces, bot, run, _ = setup
    assert run("你好") is None
    for text in ("表情帮助", "同步表情列表", "表情列表", "表情详情列表", "命名表情 3 x", "删除表情 开心", "收藏表情 x"):
        assert "仅限 Clonoth 管理员" in run(text, admin=False)
    assert bot.calls == []


def test_sync_writes_names_and_metadata_and_prompt(setup):
    faces, bot, run, tmp = setup
    reply = run("同步表情列表")
    assert "已同步 3 个已命名收藏表情（AI 可见基础名 2 个）" in reply
    assert "检测到 1 组同名表情：开心(x2)" in reply
    assert faces.current_names() == ["开心", "难过"]
    assert len(faces.current_metadata()) == 3
    block = faces.prompt_block()
    assert "[表情:名称]" in block and "开心、难过" in block
    faces.prompt_limit = 1
    assert "另有 1 个未展示" in faces.prompt_block()
    assert run("表情列表").startswith("AI 可用收藏表情：\n1. 开心\n2. 难过")


def test_ambiguous_name_requires_index_then_rename_by_index(setup):
    faces, bot, run, _ = setup
    reply = run("删除表情 开心")
    assert "检测到 2 个同名表情“开心”" in reply and "删除表情 1" in reply
    assert not any(c[0] == "delete_custom_face" for c in bot.calls)
    reply = run("命名表情 3 惊讶")
    assert reply.startswith("已将收藏表情重命名为：惊讶")
    assert ("set_custom_face_desc", {"emoji_id": 3, "res_id": "r3", "md5": "cccccccc33", "desc": "惊讶"}) in bot.calls
    assert "惊讶" in faces.current_names()


def test_delete_unique_and_missing(setup):
    faces, bot, run, _ = setup
    assert run("删除表情 难过") == "已删除收藏表情：难过"
    assert ("delete_custom_face", {"res_id": "r4"}) in bot.calls
    assert "难过" not in faces.current_names()
    assert run("删除表情 不存在").startswith("没有找到收藏表情")


def test_face_value_keeps_zero(setup):
    assert cf.face_value({"emojiId": 0}, "emojiId") == 0
    assert cf.face_value({"emojiId": " "}, "emojiId", "emoji_id") is None


def test_add_from_current_image_sets_description(setup):
    faces, bot, run, tmp = setup
    img = tmp / "data" / "attachments" / "x" / "a.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\nhello")
    reply = run("收藏表情 好耶", current=[{"type": "image", "path": "data/attachments/x/a.png"}])
    assert reply.startswith("已尝试收藏表情：好耶")
    add = next(kw for api, kw in bot.calls if api == "add_custom_face")
    assert add["file"] == str(img) and add["is_origin"] is True
    assert any(api == "set_custom_face_desc" and kw["desc"] == "好耶" and kw["res_id"] == "r9" for api, kw in bot.calls)
    assert "好耶" in faces.current_names()


def test_add_without_image(setup):
    _, bot, run, _ = setup
    assert run("收藏表情 好耶").startswith("没有找到可收藏的图片")
