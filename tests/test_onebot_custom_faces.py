"""Tests for QQ custom faces (fake NapCat bot, no network)."""
from __future__ import annotations

import asyncio
import hashlib
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
    def __init__(self, faces, *, ignore_add=False, ignore_desc=False):
        self.faces = [dict(f) for f in faces]
        self.calls = []
        self.ignore_add = ignore_add
        self.ignore_desc = ignore_desc

    async def call_api(self, api, **kw):
        self.calls.append((api, kw))
        if api == "fetch_custom_face_detail":
            return {"data": {"emojiInfoList": [dict(f) for f in self.faces]}}
        if api == "set_custom_face_desc":
            for f in self.faces:
                if f.get("resId") == kw["res_id"] and not self.ignore_desc:
                    f["desc"] = kw["desc"]
            return {}
        if api == "delete_custom_face":
            self.faces = [f for f in self.faces if f.get("resId") != kw["res_id"]]
            return {}
        if api == "add_custom_face":
            if not self.ignore_add:
                self.faces.append({"emojiId": 9, "resId": "r9", "md5": kw["md5"].upper(), "desc": ""})
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


def test_sync_writes_names_and_metadata(setup):
    faces, bot, run, tmp = setup
    reply = run("同步表情列表")
    assert "已同步 3 个已命名收藏表情（AI 可见基础名 2 个）" in reply
    assert "检测到 1 组同名表情：开心(x2)" in reply
    assert faces.current_names() == ["开心", "难过"]
    assert len(faces.current_metadata()) == 3
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
    assert reply.startswith("已收藏表情：好耶\n模型可用 [表情:好耶] 调用。")
    add = next(kw for api, kw in bot.calls if api == "add_custom_face")
    assert add["file"] == str(img) and add["is_origin"] is True
    assert any(api == "set_custom_face_desc" and kw["desc"] == "好耶" and kw["res_id"] == "r9" for api, kw in bot.calls)
    assert "好耶" in faces.current_names()


def test_add_without_image(setup):
    _, bot, run, _ = setup
    assert run("收藏表情 好耶").startswith("没有找到可收藏的图片")


PNG = b"\x89PNG\r\n\x1a\nhello"


def _image(tmp, data=PNG):
    img = tmp / "data" / "attachments" / "x" / "a.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(data)
    return [{"type": "image", "path": "data/attachments/x/a.png"}]


def test_add_existing_image_renames_instead_of_duplicating(setup):
    faces, bot, run, tmp = setup
    md5 = hashlib.md5(PNG).hexdigest()
    bot.faces[0]["md5"] = md5.upper()  # QQ already has this image as “开心”
    reply = run("收藏表情 好耶", current=_image(tmp))
    assert reply.startswith("这张图片已在收藏中（原名「开心」），已改名为：好耶")
    assert not any(api == "add_custom_face" for api, _ in bot.calls)
    assert "好耶" in faces.current_names()
    assert run("收藏表情 好耶", current=_image(tmp)).startswith("这张图片已在收藏中，名称就是「好耶」")


def test_add_reports_failure_when_napcat_did_not_save(setup):
    faces, bot, run, tmp = setup
    bot.ignore_add = True
    reply = run("收藏表情 好耶", current=_image(tmp))
    assert reply.startswith("收藏表情失败：NapCat 返回成功，但收藏列表里找不到这张图片")
    assert not any(api == "set_custom_face_desc" for api, _ in bot.calls)


def test_add_reports_failure_when_name_not_applied(setup):
    faces, bot, run, tmp = setup
    bot.ignore_desc = True
    reply = run("收藏表情 好耶", current=_image(tmp))
    assert reply.startswith("图片已收藏，但命名为「好耶」失败：NapCat 没有应用新名称")
    assert "好耶" not in faces.current_names()


def test_usage_note_flags_names_beyond_prompt_limit(setup):
    faces, bot, run, tmp = setup
    faces.prompt_limit = 1
    reply = run("命名表情 3 惊讶")
    assert "超过提示上限 1，模型看不到" in reply


def test_overview_marks_duplicates_and_prompt_visibility(setup):
    faces, bot, run, _ = setup
    run("同步表情列表")
    faces.prompt_limit = 1
    data = asyncio.run(faces.overview(bot))
    assert data["prompt_limit"] == 1 and data["names_total"] == 2
    by_res = {f["res_id"]: f for f in data["faces"]}
    assert by_res["r1"]["duplicates"] == 2 and by_res["r1"]["in_prompt"] and by_res["r1"]["prompt_position"] == 1
    assert by_res["r3"]["base_name"] == "" and by_res["r3"]["prompt_position"] == 0
    assert by_res["r4"]["prompt_position"] == 2 and not by_res["r4"]["in_prompt"]
    assert by_res["r4"]["index"] == 4


def test_add_upload_validates_and_saves_under_attachments(setup):
    faces, bot, run, tmp = setup
    ok, message = asyncio.run(faces.add_upload(bot, "好耶", "x.txt", b"not an image"))
    assert not ok and "只能上传" in message
    ok, message = asyncio.run(faces.add_upload(bot, "好耶", "a.png", PNG))
    assert ok and message.startswith("已收藏表情：好耶")
    add = next(kw for api, kw in bot.calls if api == "add_custom_face")
    saved = Path(add["file"])
    assert saved.parent == tmp / "data" / "attachments" / "qq_faces_upload" and saved.suffix == ".png"


def test_delete_reports_failure_when_face_remains(setup):
    faces, bot, run, _ = setup
    original = bot.call_api

    async def stubborn(api, **kw):
        if api == "delete_custom_face":
            bot.calls.append((api, kw))
            return {}
        return await original(api, **kw)

    bot.call_api = stubborn
    assert run("删除表情 难过") == "NapCat 返回成功，但该表情仍在收藏列表中。"
