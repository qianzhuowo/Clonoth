"""Supervisor 内部共享工具函数与数据类。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionInfo:
    session_id: str
    channel: str
    conversation_key: str
    created_at: datetime
    updated_at: datetime
    # Why: inbound routing needs the session's own entry node after supervisor
    # restarts clear volatile overrides. How: store the selected node on the
    # shared SessionInfo object. Purpose: callers can persist and restore the
    # per-session default without changing existing constructor call sites.
    entry_node_id: str = ""
