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
