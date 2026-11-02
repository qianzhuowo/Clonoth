"""Tests for QQ profiles and message parsing modules."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

anon = importlib.import_module("adapters.onebot.anon")
profiles = importlib.import_module("adapters.onebot.profiles")
mp = importlib.import_module("adapters.onebot.message_parse")


class Seg:
    def __init__(self, type_, **data):
        self.type = type_
        self.data = data


def _setup(tmp_path):
    a = anon.AnonMap(tmp_path / "anon.json", save_min_interval=0, hash_secret="")
    p = profiles.UserProfiles({"1111111": {"display_name": "小明", "address_as": "明哥", "title": "群主"}})
    at_name = lambda qq: p.display_name(qq) or a.anonymize_user_id(qq)
    return a, p, at_name


# ---- profiles ---------------------------------------------------------------

def test_load_profiles_yaml_and_aliases(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("users:\n  '1111111':\n    name: 小明\n    call_as: 明哥\n    role: 群主\n  bad: x\n", encoding="utf-8")
    loaded = profiles.load_profiles(f)
    assert loaded == {"1111111": {"display_name": "小明", "address_as": "明哥", "title": "群主"}}
    assert profiles.load_profiles(tmp_path / "missing.yaml") == {}
    up = profiles.UserProfiles(loaded)
    assert up.aliases(1111111) == ["小明", "明哥", "群主"]
    assert ("1111111", ["小明", "小明", "明哥", "群主"]) in list(up.name_index())


def test_sender_name_never_leaks_raw_id(tmp_path):
    a, p, _ = _setup(tmp_path)
    assert p.sender_display_name({}, "1111111", a) == "小明"
    assert p.sender_display_name({"card": "a[b]\nc"}, "2222222", a) == "a(b) c"
    assert p.sender_display_name({}, "3333333", a) == "UserA"
    assert profiles.sanitize_name("x" * 40) == "x" * 32 + "…"


def test_identity_lines_state_permission_from_code(tmp_path):
    a, p, _ = _setup(tmp_path)
    lines = p.identity_lines("1111111", "小明", a, is_admin=False)
    assert "称呼要求: 请称呼该用户为「明哥」" in lines
    assert "Clonoth 管理员: 否" in lines
    assert all("1111111" not in line for line in lines)


# ---- message_parse ------------------------------------------------------------

def test_segments_dicts_and_cq_render_the_same(tmp_path):
    a, _, at_name = _setup(tmp_path)
    segs = [Seg("reply", id="9"), Seg("at", qq="999"), Seg("at", qq="1111111"), Seg("text", text=" 看 "),
            Seg("image"), Seg("face", id="12"), Seg("file", name="/x/报告.pdf"), Seg("at", qq="5555555")]
    text = mp.message_to_text(segs, at_name, bot_self_id=999)
    assert text == "@小明 看 [图片][QQ表情:12][文件:报告.pdf]@UserA"
    dicts = [{"type": s.type, "data": s.data} for s in segs]
    assert mp.message_to_text(dicts, at_name, bot_self_id=999) == text
    cq = mp.message_to_text("[CQ:at,qq=all] hi [CQ:reply,id=5][CQ:image,file=x]", at_name)
    assert cq == "@全体成员 hi [回复:5][图片]"


def test_extract_reply_and_forward_ids():
    assert mp.extract_reply_message_id([Seg("reply", message_id=" 42 ")]) == " 42 "
    assert mp.extract_reply_message_id([], "[CQ:reply,id=77]") == "77"
    assert mp.extract_reply_message_id([Seg("text", text="x")]) is None
    assert mp.extract_forward_ids([{"type": "forward", "data": {"res_id": "R1"}}]) == ["R1"]
    assert mp.extract_forward_ids("[CQ:forward,id=R2]") == ["R2"]


def test_forward_expansion_limits_and_no_alias_registration(tmp_path):
    a, p, at_name = _setup(tmp_path)
    store = {
        "A": [{"sender": {"user_id": "8888888", "nickname": "路人"}, "message": [{"type": "text", "data": {"text": "第一条"}}]},
              {"sender": {"user_id": "8888889"}, "message": [{"type": "forward", "data": {"id": "B"}}]}],
        "B": [{"sender": {"user_id": "1111111"}, "message": [{"type": "forward", "data": {"id": "A"}}]}],
    }

    async def fetch(fid):
        return store.get(fid)

    exp = mp.ForwardExpander(fetch, at_name, p.display_name, enabled=True, max_depth=3, max_messages=10)
    out = asyncio.run(exp.to_text([{"type": "forward", "data": {"id": "A"}}]))
    assert "【合并转发 id=A】" in out and "路人: 第一条" in out
    assert "转发用户:" in out          # no name -> placeholder, never the raw id
    assert "小明:" in out              # profile name inside nested forward
    assert "已跳过循环引用" in out      # B -> A loop stopped
    assert "8888888" not in out and "8888888" not in a.users

    limited = mp.ForwardExpander(fetch, at_name, p.display_name, enabled=True, max_depth=3, max_messages=1)
    assert "条数" in asyncio.run(limited.to_text([{"type": "forward", "data": {"id": "A"}}]))
    off = mp.ForwardExpander(fetch, at_name, p.display_name, enabled=False)
    assert asyncio.run(off.to_text([{"type": "forward", "data": {"id": "A"}}])) == "[合并转发]"


def test_bot_api_wrappers_and_reply_cache():
    class Bot:
        def __init__(self, result):
            self.result, self.calls = result, []

        async def call_api(self, api, **kw):
            self.calls.append((api, kw))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    b = Bot({"data": {"messages": [{"x": 1}, "junk"]}})
    assert asyncio.run(mp.get_forward_messages(b, "R")) == [{"x": 1}]
    b2 = Bot({"message_id": 1})
    assert asyncio.run(mp.get_reply_message(b2, "123")) == {"message_id": 1}
    assert b2.calls == [("get_msg", {"message_id": 123})]
    assert asyncio.run(mp.get_reply_message(Bot(RuntimeError("x")), "1")) is None

    cache = mp.ReplyMessageCache(max_items=2)
    for mid in (1, 2, 3):
        cache.remember(types.SimpleNamespace(message_id=mid, raw_message=f"m{mid}", user_id=1, time=0, sender=None))
    assert cache.get(1) is None and cache.get(3)["raw_message"] == "m3"


def test_compact_and_truncate():
    assert mp.compact_text("a \n  b", limit=10) == "a b"
    assert mp.compact_text("x" * 20, limit=5) == "xxxxx…"
    assert mp.truncate_forward_text("y" * 600, limit=500).endswith("已截断）")
