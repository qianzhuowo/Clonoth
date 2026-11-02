"""Tests for QQ inbound attachments (no network: local files and a fake bot)."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

ia = importlib.import_module("adapters.onebot.inbound_attachments")

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
KEY = "qq_group:abcdef0123456789abcdef01"


def _event(user_id="1111111", message_id="m1", message=None, raw=None):
    ev = types.SimpleNamespace(user_id=user_id, message_id=message_id, raw_message=raw)
    ev.get_message = lambda: message or []
    return ev


def test_mime_ext_and_names():
    assert ia.guess_image_mime("x", "image/webp; q=1") == "image/webp"
    assert ia.guess_image_mime("x", "", PNG) == "image/png"
    assert ia.guess_image_mime("http://a/b.GIF?x=1") == "image/gif"
    assert ia.guess_image_mime("http://a/b") == "image/jpeg"
    assert ia.image_ext_from_url_or_mime("http://a/b.png?x", "image/jpeg") == ".png"
    assert ia.image_ext_from_url_or_mime("http://a/b", "image/webp") == ".webp"
    assert ia.safe_attachment_name("../../etc/pa?ss.txt") == "pa_ss.txt"
    assert ia.safe_attachment_name("..\\..\\") == "attachment"


def test_iter_sources_from_segments_and_cq():
    segs = [{"type": "image", "data": {"url": "http://i/1.png"}}, {"type": "mface", "data": {"file": "http://i/2"}},
            {"type": "text", "data": {"text": "x"}}, {"type": "file", "data": {"name": "a/b.pdf", "url": "http://f/1"}}]
    assert ia.iter_image_sources(segs) == ["http://i/1.png", "http://i/2"]
    assert ia.iter_image_sources("[CQ:image,file=x,url=http://i/3.jpg]") == ["http://i/3.jpg"]
    files = ia.iter_file_sources(segs)
    assert len(files) == 1 and files[0]["name"] == "b.pdf"


def test_local_images_saved_under_stable_key(tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(PNG)
    ws = tmp_path / "ws"
    atts, errors = asyncio.run(ia.image_sources_to_attachments([str(src), str(tmp_path / "missing.png")], KEY, workspace=ws))
    assert len(atts) == 1 and len(errors) == 1
    att = atts[0]
    assert att["type"] == "image" and att["mime_type"] == "image/png"
    assert att["path"].startswith("data/attachments/qq_group_abcdef")
    assert (ws / att["path"]).read_bytes() == PNG


def test_file_via_onebot_file_id_and_errors(tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_bytes(b"hello")

    class Bot:
        calls = []

        async def call_api(self, api, **kw):
            self.calls.append((api, kw))
            if "busid" in kw:
                raise RuntimeError("needs no busid")
            return {"data": {"url": str(doc)}}

    bot = Bot()
    ev = types.SimpleNamespace(group_id=123456, user_id=1)
    items = [{"file_id": "F1", "name": "doc.txt", "busid": 102}, {"file_id": "", "name": "x"}, {"name": "big", "size": 10**12}]
    atts, errors = asyncio.run(ia.file_sources_to_attachments(items, KEY, bot=bot, event=ev, workspace=tmp_path / "ws"))
    assert [a["name"] for a in atts] == ["doc.txt"]
    assert [c[0] for c in bot.calls] == ["get_group_file_url", "get_group_file_url"]
    assert any("太大" in e for e in errors) and any("没有提供可下载地址" in e for e in errors)


def test_reply_cache_persist_ttl_and_conversation_scope(tmp_path):
    path = tmp_path / "cache" / "reply.json"
    cache = ia.ReplyAttachmentCache(path, ttl=3600, max_items=2)
    img = {"type": "image", "path": "data/attachments/x/1.png"}
    assert cache.remember("100", KEY, "1111111", [img])
    assert cache.remember("101", KEY, "1111111", [{"type": "file"}]) is False  # no path
    assert cache.images_for("100", KEY) == [img]
    assert cache.images_for("100", "qq_group:other") == []
    cache.remember("old", KEY, "1", [img], created_at=time.time() - 7200)
    assert cache.get("old") is None  # expired on trim
    asyncio.run(cache.save())

    loaded = ia.ReplyAttachmentCache(path, ttl=3600)
    assert loaded.load() == 1 and loaded.images_for("100") == [img]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_recent_images_same_sender_latest_batch(tmp_path):
    cache = ia.ReplyAttachmentCache(tmp_path / "r.json")
    recent = ia.RecentImages(cache)
    a1, a2, b1 = ({"type": "image", "path": f"p/{n}.png"} for n in ("a1", "a2", "b1"))
    recent.remember(KEY, _event("1111111", "m1"), [a1])
    recent.remember(KEY, _event("1111111", "m2"), [a2, {"type": "file", "path": "f"}])
    recent.remember(KEY, _event("2222222", "m3"), [b1])
    assert recent.for_sender(KEY, "1111111") == [a2]
    assert recent.for_sender(KEY, "3333333") == []
    assert cache.images_for("m2", KEY) == [a2]

    atts: list = []
    asyncio.run(recent.merge_after_text(event=_event("1111111"), conversation_key=KEY,
                                        user_text="看看这张图", attachments=atts, wait=0))
    assert atts == [a2]
    quoted: list = []
    asyncio.run(recent.merge_after_text(event=_event("1111111", raw="[CQ:reply,id=9]"), conversation_key=KEY,
                                        user_text="看看这张图", attachments=quoted, wait=0))
    assert quoted == []  # a quote stays on its own chain


def test_custom_face_image_priority(tmp_path):
    cache = ia.ReplyAttachmentCache(tmp_path / "r.json")
    recent = ia.RecentImages(cache)
    old = {"type": "image", "path": "p/old.png"}
    recent.remember(KEY, _event("1111111", "m0"), [old])

    class Bot:
        async def call_api(self, api, **kw):
            return None

    cur = [{"type": "image", "path": "p/cur.png"}]
    got, _ = asyncio.run(ia.custom_face_command_images(bot=Bot(), event=_event(), conversation_key=KEY,
                                                       current=cur, recent=recent))
    assert got == cur
    got, _ = asyncio.run(ia.custom_face_command_images(bot=Bot(), event=_event(), conversation_key=KEY,
                                                       current=[], recent=recent))
    assert got == [old]
