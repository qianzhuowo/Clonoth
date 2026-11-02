import asyncio
import hashlib
import json
from pathlib import Path

import yaml

from engine.builtin import knowledge_inject as ki
from engine.builtin.dream import DreamHandler
from toolbox.context import ToolContext


# --------------------------------------------------------------------------- #
# namespace resolution
# --------------------------------------------------------------------------- #
def test_isolated_conversation_key_unwraps_dispatch_children() -> None:
    assert ki.isolated_conversation_key("qq_group:abc") == "qq_group:abc"
    assert ki.isolated_conversation_key("agent:w:qq_group:abc") == "qq_group:abc"
    assert ki.isolated_conversation_key("agent:w:qq_group:abc:7f1e") == "qq_group:abc"
    assert ki.isolated_conversation_key("agent:w:ctx1:qq_private:p1") == "qq_private:p1"
    assert ki.isolated_conversation_key("discord:123") == ""
    assert ki.isolated_conversation_key("") == ""


def test_isolation_prefixes_follow_runtime_config() -> None:
    cfg = {"memory": {"isolate_conversation_prefixes": ["discord"]}}
    assert ki.isolated_conversation_key("discord:123", cfg) == "discord:123"
    assert ki.isolated_conversation_key("qq_group:abc", cfg) == ""
    assert ki.isolated_conversation_key("qq_group:abc", {"memory": {"isolate_conversation_prefixes": []}}) == ""


def test_conversation_namespace_matches_legacy_digest() -> None:
    key = "qq_group:abc"
    legacy = "conv_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    assert ki.conversation_memory_namespace(key) == legacy
    assert ki.isolated_memory_namespace("agent:w:qq_group:abc") == legacy
    assert ki.is_isolated_memory_namespace(legacy)


def test_effective_namespace_precedence() -> None:
    conv = ki.conversation_memory_namespace("qq_group:abc")
    assert ki._effective_memory_ns({"memory_book": "book1"}, "n", "qq_group:abc") == "book1"
    assert ki._effective_memory_ns({}, "n", "qq_group:abc") == conv
    assert ki._effective_memory_ns({}, "n", "discord:1") == "n"


# --------------------------------------------------------------------------- #
# memory tools
# --------------------------------------------------------------------------- #
def _ctx(root: Path, conversation_key: str, node_id: str = "qq.orchestrator") -> ToolContext:
    return ToolContext(
        supervisor_url="", session_id="s", run_id="r", worker_id="w",
        workspace_root=root, http=None, registry=None,
        conversation_key=conversation_key, node_id=node_id,
    )


def _run(coro):
    return asyncio.run(coro)


def _write_root_book(root: Path) -> None:
    mem = root / "data" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "people.yaml").write_text(yaml.safe_dump({
        "book": "people", "entries": [{"id": "legacy", "content": "shared root memory"}],
    }), encoding="utf-8")


def test_save_list_delete_stay_inside_the_chat(tmp_path) -> None:
    _write_root_book(tmp_path)
    group_a = _ctx(tmp_path, "qq_group:aaa")
    group_b = _ctx(tmp_path, "agent:w:qq_group:bbb")

    out = _run(ki.save_memory({"id": "m1", "book": "people", "content": "A 的成员"}, group_a))
    assert out.get("ok", True) is not False
    ns_a = ki.conversation_memory_namespace("qq_group:aaa")
    assert (tmp_path / "data" / "memory" / ns_a / "people.yaml").is_file()

    listed_a = _run(ki.list_memories({}, group_a))["data"]["entries"]
    assert [e["id"] for e in listed_a] == ["m1"]

    # another chat sees neither A's memory nor the shared root books
    assert _run(ki.list_memories({}, group_b))["data"]["entries"] == []
    deleted = _run(ki.delete_memory({"id": "legacy", "book": "people"}, group_b))
    assert deleted.get("ok") is False
    assert (tmp_path / "data" / "memory" / "people.yaml").is_file()

    assert _run(ki.delete_memory({"id": "m1", "book": "people"}, group_a)).get("ok", True) is not False
    assert _run(ki.list_memories({}, group_a))["data"]["entries"] == []


def test_non_isolated_chat_keeps_root_fallback(tmp_path) -> None:
    _write_root_book(tmp_path)
    listed = _run(ki.list_memories({}, _ctx(tmp_path, "discord:1", node_id="assistant")))["data"]["entries"]
    assert [e["id"] for e in listed] == ["legacy"]


# --------------------------------------------------------------------------- #
# dream never mixes isolated chats
# --------------------------------------------------------------------------- #
def _dream() -> DreamHandler:
    return DreamHandler.__new__(DreamHandler)


def test_dream_skips_isolated_sessions(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sessions.json").write_text(json.dumps({
        "s1": {"session_id": "s1", "conversation_key": "qq_group:aaa", "channel": "qq_group"},
        "s2": {"session_id": "s2", "conversation_key": "qq_private:p1", "channel": "qq_private"},
        "s3": {"session_id": "s3", "conversation_key": "discord:1", "channel": "discord"},
    }), encoding="utf-8")
    assert _dream()._active_session_ids_from_registry(tmp_path) == {"s3"}


def test_dream_topology_and_books_skip_conv_namespaces(tmp_path) -> None:
    mem = tmp_path / "data" / "memory"
    conv = mem / ki.conversation_memory_namespace("qq_group:aaa")
    node = mem / "assistant"
    for d, book in ((conv, "secret_group"), (node, "public")):
        d.mkdir(parents=True)
        (d / f"{book}.yaml").write_text(yaml.safe_dump({
            "book": book, "entries": [{"id": "e1", "content": "x", "keywords": ["k"]}],
        }), encoding="utf-8")
    entries = _dream()._load_memory_topology_entries(tmp_path)
    assert {e["book"] for e in entries} == {"public"}
    books = _dream()._build_book_list(tmp_path)
    assert "public" in books and "secret_group" not in books
