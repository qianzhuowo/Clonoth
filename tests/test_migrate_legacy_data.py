"""scripts/migrate_legacy_data.py: QQ sessions + memory from a legacy install."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "migrate_legacy_data", Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_data.py")
mig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mig)

GROUP_KEY = "qq_group:" + hashlib.sha256(b"qq_group:8888889").hexdigest()[:24]


def _session(sid, channel, key):
    return {"session_id": sid, "channel": channel, "conversation_key": key, "entry_node_id": "main", "reset": False}


@pytest.fixture
def roots(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for root in (old, new):
        (root / "data" / "conversations").mkdir(parents=True)
        (root / "config").mkdir()
    sessions = {
        "g1": _session("g1", "qq_group", GROUP_KEY),
        "p1": _session("p1", "qq_private", "qq_private:7654321"),
        "p2": _session("p2", "qq_private", "qq_private:" + "a" * 24),
        "a1": _session("a1", "agent", "agent:draw:x"),
    }
    (old / "data" / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    for sid in ("g1", "p1", "a1"):
        (old / "data" / "conversations" / f"{sid}.jsonl").write_text('{"id":"m","role":"user","content":"hi"}\n')
    mem = old / "data" / "memory" / "conv_abc"
    mem.mkdir(parents=True)
    (mem / "people.yaml").write_text("book: people\nentries: []\n", encoding="utf-8")
    (old / "data" / "onebot_anon_map.json").write_text('{"version": 1, "users": {"x": "UserA"}}')
    (old / "config" / "qq_user_profiles.yaml").write_text("users: {}\n")
    # new deployment already has a test session for the same group
    (new / "data" / "sessions.json").write_text(json.dumps({"t1": _session("t1", "qq_group", GROUP_KEY)}))
    (new / "data" / "conversations" / "t1.jsonl").write_text('{"id":"t"}\n')
    (new / "data" / "onebot_anon_map.json").write_text('{"version": 1, "users": {}}')
    return old, new


def _run(old, new, *extra):
    return mig.Migrator(old, new, apply="--apply" in extra,
                        on_conflict="replace" if "--replace" in extra else "skip",
                        entry_node="qq.orchestrator", secret="").run()


def test_raw_key_converted_like_adapter():
    assert mig.stable_key("qq_private:7654321") == "qq_private:" + hashlib.sha256(b"qq_private:7654321").hexdigest()[:24]
    assert mig.stable_key(GROUP_KEY) == GROUP_KEY


def test_dry_run_writes_nothing(roots):
    old, new = roots
    before = sorted(p.relative_to(new) for p in new.rglob("*"))
    result = _run(old, new)
    assert result["mode"] == "dry-run" and result["counts"]["sessions_migrated"] == 1
    assert sorted(p.relative_to(new) for p in new.rglob("*")) == before


def test_apply_skip_conflict(roots):
    old, new = roots
    result = _run(old, new, "--apply")
    c = result["counts"]
    assert c["sessions_migrated"] == 1 and c["sessions_key_converted"] == 1
    assert c["sessions_conflict_skipped"] == 1 and c["sessions_skipped_no_history"] == 1
    assert c["sessions_skipped_non_qq"] == 1
    sessions = json.loads((new / "data" / "sessions.json").read_text())
    assert set(sessions) == {"t1", "p1"} and sessions["p1"]["entry_node_id"] == "qq.orchestrator"
    assert sessions["p1"]["conversation_key"] == mig.stable_key("qq_private:7654321")
    assert (new / "data" / "conversations" / "p1.jsonl").exists()
    assert (new / "data" / "memory" / "conv_abc" / "people.yaml").exists()
    assert json.loads((new / "data" / "onebot_anon_map.json").read_text())["users"] == {"x": "UserA"}
    assert (new / "config" / "qq_user_profiles.yaml").exists()
    backups = list(new.glob("data/_migrate_backup_*/data/onebot_anon_map.json"))
    assert backups, "overwritten alias map must be backed up"


def test_apply_replace_conflict_backs_up(roots):
    old, new = roots
    result = _run(old, new, "--apply", "--replace")
    assert result["counts"]["sessions_conflict_replaced"] == 1
    sessions = json.loads((new / "data" / "sessions.json").read_text())
    assert set(sessions) == {"g1", "p1"}
    assert not (new / "data" / "conversations" / "t1.jsonl").exists()
    assert list(new.glob("data/_migrate_backup_*/data/conversations/t1.jsonl"))


def test_rerun_is_idempotent(roots):
    old, new = roots
    _run(old, new, "--apply", "--replace")
    result = _run(old, new, "--apply", "--replace")
    c = result["counts"]
    assert c.get("sessions_migrated", 0) == 0 and c["sessions_already_present"] == 2
    assert c["memory_identical"] == 1 and c["anon_map_identical"] == 1
