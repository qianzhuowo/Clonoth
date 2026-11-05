"""The compact circuit breaker is shared between processes and reopens after a cooldown."""
from __future__ import annotations

import json

import engine.compact as compact


def test_failures_recorded_by_one_process_are_seen_by_another(monkeypatch):
    for _ in range(compact._MAX_CONSECUTIVE_FAILURES):
        compact.record_compact_failure("s1")
    # Another process has no in-memory state; it only sees the shared file.
    assert json.loads(compact._BREAKER_PATH.read_text())["s1"]["failures"] == 3
    assert compact.is_compact_circuit_open("s1") is True
    assert compact.is_compact_circuit_open("other") is False


def test_breaker_allows_retry_after_cooldown_and_success_resets(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(compact.time, "time", lambda: now[0])
    for _ in range(3):
        compact.record_compact_failure("s1")
    assert compact.is_compact_circuit_open("s1") is True
    now[0] += compact._BREAKER_COOLDOWN_SEC + 1
    assert compact.is_compact_circuit_open("s1") is False
    compact.record_compact_failure("s1")
    assert compact.is_compact_circuit_open("s1") is True
    compact.record_compact_success("s1")
    assert compact.is_compact_circuit_open("s1") is False


def test_below_threshold_stays_closed():
    compact.record_compact_failure("s1")
    compact.record_compact_failure("s1")
    assert compact.is_compact_circuit_open("s1") is False
