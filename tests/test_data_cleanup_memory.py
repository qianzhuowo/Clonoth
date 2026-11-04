"""data_cleanup: stale non-constant memory entries, transcripts and logs are pruned."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import yaml

import engine.data_cleanup as dc


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_memory_entries_pruned_by_age_constant_and_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dc, "MEMORY_ENTRY_MAX_AGE", 14 * 24 * 3600)
    book = tmp_path / "memory" / "conv_x" / "people.yaml"
    book.parent.mkdir(parents=True)
    book.write_text(yaml.safe_dump({"book": "people", "entries": [
        {"id": "old", "created_at": _iso(30)},
        {"id": "old_const", "constant": True, "created_at": _iso(30)},
        {"id": "old_but_hit", "created_at": _iso(30), "last_hit_at": _iso(1)},
        {"id": "fresh", "created_at": _iso(2)},
        {"id": "no_ts"},
    ]}, allow_unicode=True), encoding="utf-8")
    dc.purge_expired_memory_entries()
    ids = [e["id"] for e in yaml.safe_load(book.read_text(encoding="utf-8"))["entries"]]
    assert ids == ["old_const", "old_but_hit", "fresh", "no_ts"]


def test_empty_book_and_namespace_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DATA_DIR", tmp_path)
    book = tmp_path / "memory" / "conv_y" / "group.yaml"
    book.parent.mkdir(parents=True)
    book.write_text(yaml.safe_dump({"entries": [{"id": "a", "created_at": _iso(40)}]}), encoding="utf-8")
    dc.purge_expired_memory_entries()
    assert not book.exists() and not book.parent.exists() and (tmp_path / "memory").exists()


def test_disabled_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dc, "MEMORY_ENTRY_MAX_AGE", 0)
    book = tmp_path / "memory" / "rules.yaml"
    book.parent.mkdir(parents=True)
    book.write_text(yaml.safe_dump({"entries": [{"id": "a", "created_at": _iso(400)}]}), encoding="utf-8")
    dc.purge_expired_memory_entries()
    assert book.exists()


def test_old_logs_pruned_except_cleanup_log(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "DATA_DIR", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    old = time.time() - 10 * 24 * 3600
    for name in ("engine.log", "cleanup.log"):
        (logs / name).write_text("x")
        os.utime(logs / name, (old, old))
    (logs / "fresh.log").write_text("x")
    dc.purge_old_logs()
    assert sorted(p.name for p in logs.iterdir()) == ["cleanup.log", "fresh.log"]


def test_main_runs_new_tasks():
    import inspect
    src = inspect.getsource(dc.main)
    for name in ("purge_expired_memory_entries", "purge_expired_transcripts", "purge_old_logs", "llm_error_snapshots"):
        assert name in src
