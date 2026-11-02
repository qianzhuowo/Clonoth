"""Tests for the split QQ base modules: access, approval, anon."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

# The package __init__ is the NoneBot plugin (needs nonebot); these modules do
# not. Register a bare package so submodules import without running __init__.
_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

access = importlib.import_module("adapters.onebot.access")
anon = importlib.import_module("adapters.onebot.anon")
approval = importlib.import_module("adapters.onebot.approval")


# ---- access -----------------------------------------------------------------

def test_group_allow_list_is_closed_when_empty():
    assert access.is_group_allowed(123456, allowed_groups=[]) is False
    assert access.is_group_allowed("123456", allowed_groups=[123456]) is True
    assert access.is_group_allowed("abc", allowed_groups=[123456]) is False


def test_admin_and_private_rules():
    assert access.is_admin_user("10001", admin_users=[10001]) is True
    assert access.is_admin_user(None, admin_users=[10001]) is False
    common = dict(admin_users=[10001], allowed_private_users=[20002])
    assert access.is_private_allowed(10001, "group", allow_friends=False, **common) is True
    assert access.is_private_allowed(20002, "", allow_friends=False, **common) is True
    assert access.is_private_allowed(30003, "friend", allow_friends=False, **common) is False
    assert access.is_private_allowed(30003, "friend", allow_friends=True, **common) is True
    assert access.is_private_allowed(30003, "group", allow_friends=True, **common) is False


def test_config_id_list_ignores_placeholders():
    from adapters.onebot.config import _parse_qq_id_list

    assert _parse_qq_id_list("[占位符], 12345 ,abc,67890") == [12345, 67890]
    assert _parse_qq_id_list("") == []


# ---- approval ---------------------------------------------------------------

AID = "0123abcd-0000-4000-8000-00000000beef"


def test_parse_commands_and_reply_verbs():
    assert approval.parse_approval_command(f"审批 同意 {AID}") == ("allow", AID)
    assert approval.parse_approval_command("拒绝 0123abcd") == ("deny", "0123abcd")
    assert approval.parse_approval_command("审批 同意") is None
    assert approval.parse_approval_command("同意 0123") is None  # prefix too short
    assert approval.parse_approval_reply_verb("同意") == "allow"
    assert approval.parse_approval_reply_verb("审批 拒绝") == "deny"
    assert approval.parse_approval_reply_verb("同意吗？") is None


def test_book_reply_mapping_prefix_and_forget():
    book = approval.ApprovalBook(max_message_ids=2)
    book.add(AID, "execute_command", {"command": "ls"}, conversation_key="qq_group:x")
    book.remember_message("-12.0", AID)
    assert book.resolve_by_reply(-12) == AID
    assert book.resolve_token("0123abcd") == (AID, "")
    assert book.resolve_token("ffff0000")[0] is None
    assert book.extract_from_text(f"ID: {AID}\n...") == AID

    book.remember_message(1, "other")
    book.remember_message(2, "other")
    assert book.resolve_by_reply(-12) is None  # evicted by the bound

    book.remember_message(3, AID)
    assert book.forget(AID)["operation"] == "execute_command"
    assert book.resolve_by_reply(3) is None
    assert "3" not in book.message_ids


def test_summary_has_origin_line_and_is_truncated(monkeypatch):
    origin = {"conversation_key": "qq_group:abc", "target_type": "group"}
    line = approval.origin_line_for(origin, group_name="测试群", user_name="小明")
    text = approval.approval_summary(AID, "execute_command", {"command": "ls"}, origin_line=line)
    assert "来源：【群】测试群（发起人：小明）" in text
    assert f"审批 同意 {AID}" in text

    long = approval.approval_summary(AID, "op", {"args": "x" * 9000})
    assert len(long) <= approval.config.QQ_MESSAGE_LIMIT
    assert "x" * 1501 not in long  # args capped at 1500 chars

    cut = approval.truncate_qq_text("y" * 800, limit=500)
    assert len(cut) == 500 and cut.endswith("（内容过长，已截断）")
    assert approval.truncate_qq_text("short", limit=500) == "short"


# ---- anon -------------------------------------------------------------------

def test_alias_index_roundtrip():
    for i in (0, 1, 25, 26, 27, 701, 702):
        assert anon.alias_to_index("User", anon.alias_from_index("User", i)) == i
    assert anon.alias_from_index("User", 26) == "UserAA"
    assert anon.alias_to_index("User", "Userx") == -1


def test_anonymize_registers_and_replaces_only_known_ids(tmp_path):
    m = anon.AnonMap(tmp_path / "anon.json", save_min_interval=0, hash_secret="")
    assert m.anonymize_user_id("1234567") == "UserA"
    assert m.anonymize_user_id("1234567") == "UserA"
    assert m.anonymize_user_id("1") == "UserUnknown"  # implausible id not stored
    assert m.anonymize_group_id("7654321") == "GroupA"
    text = m.anonymize_text("1234567 在 7654321 转了 12345678 元，验证码 1234")
    assert text == "UserA 在 GroupA 转了 12345678 元，验证码 1234"
    assert m.resolve_at_alias_to_real("@UserA") == "1234567"
    assert m.resolve_at_alias_to_real("小明", lambda: [("2222222", ["小明"])]) == "2222222"
    assert m.resolve_at_alias_to_real("nobody") == ""


def test_persistence_roundtrip_keeps_counters_and_drops_short_ids(tmp_path):
    path = tmp_path / "anon.json"
    path.write_text(json.dumps({
        "users": {"1234567": {"alias": "UserC"}, "1": {"alias": "UserD"}},
        "groups": {"7654321": {"alias": "GroupA"}},
        "user_next": 2,
    }), encoding="utf-8")
    m = anon.AnonMap(path, save_min_interval=0, hash_secret="")
    m.load()
    assert "1" not in m.users and m.dirty is True
    assert m.user_next == 3  # max(saved 2, UserC index 2 + 1)
    assert m.anonymize_user_id("7777777") == "UserD"

    asyncio.run(m.save())
    m2 = anon.AnonMap(path, save_min_interval=0, hash_secret="")
    m2.load()
    assert m2.users["7777777"] == "UserD" and m2.user_next == 4 and m2.groups["7654321"] == "GroupA"


def test_mark_dirty_throttles_into_one_save(tmp_path):
    async def run():
        m = anon.AnonMap(tmp_path / "anon.json", save_min_interval=0.05, hash_secret="")
        for i in range(5):
            m.anonymize_user_id(f"100000{i}")
        task = m._save_task
        assert task is not None
        await task
        return m

    m = asyncio.run(run())
    assert m.dirty is False
    assert len(json.loads((tmp_path / "anon.json").read_text(encoding="utf-8"))["users"]) == 5


def test_stable_conversation_key_hides_real_id_and_maps_back(tmp_path):
    m = anon.AnonMap(tmp_path / "anon.json", save_min_interval=0, hash_secret="s3cret")
    stable = m.stable_conversation_key("qq_group:7654321")
    assert stable.startswith("qq_group:") and "7654321" not in stable
    assert len(stable.split(":", 1)[1]) == 24
    assert stable == m.stable_conversation_key("qq_group:7654321")
    assert stable != anon.AnonMap(tmp_path / "b.json", hash_secret="").stable_conversation_key("qq_group:7654321")
    assert m.real_conversation_key(stable) == "qq_group:7654321"
    assert m.target_from_conversation_key(stable) == {"type": "group", "group_id": 7654321}

    p = m.stable_conversation_key("qq_private:1234567")
    assert m.target_from_conversation_key(p) == {"type": "private", "user_id": 1234567}
    assert m.target_from_conversation_key("discord:1") is None

    fresh = anon.AnonMap(tmp_path / "c.json", hash_secret="s3cret")
    fresh.restore_conversation_keys(dict(m.real_conversation_keys))
    assert fresh.target_from_conversation_key(stable) == {"type": "group", "group_id": 7654321}


def test_stable_key_feeds_isolated_memory_namespace(tmp_path):
    from engine.builtin.knowledge_inject import conversation_memory_namespace

    m = anon.AnonMap(tmp_path / "anon.json", hash_secret="s3cret")
    stable = m.stable_conversation_key("qq_group:7654321")
    ns = conversation_memory_namespace(stable)
    assert ns.startswith("conv_") and "7654321" not in ns
