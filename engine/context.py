from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class RunContext:
    """单个 task 执行片段的运行上下文。"""

    workspace_root: Path
    supervisor_url: str
    session_id: str
    worker_id: str
    http: httpx.AsyncClient
    llm_http: httpx.AsyncClient
    api_key: str = ""
    base_url: str = ""
    default_model: str = "gpt-4o-mini"
    user_text: str = ""
    task_id: str = ""
    session_generation: int = 0
    source_inbound_seq: int | None = None
    task_context: dict = field(default_factory=dict)
    # Child Session 隔离：dispatch 子节点使用独立 session ID。
    # 非空时，_shadow_write 和 ConversationStore 操作写入此 session 而非 parent session_id。
    # 主节点和无 child session 的场景下为空字符串。
    child_session_id: str = ""

    async def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.source_inbound_seq is not None:
            payload.setdefault("source_inbound_seq", self.source_inbound_seq)
        try:
            await self.http.post(
                f"{self.supervisor_url}/v1/sessions/{self.session_id}/events",
                json={"type": event_type, "payload": payload},
            )
        except Exception:
            pass

    async def check_cancelled(self) -> bool:
        try:
            if self.task_id:
                r = await self.http.get(f"{self.supervisor_url}/v1/tasks/{self.task_id}/cancelled")
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
            else:
                r = await self.http.get(f"{self.supervisor_url}/v1/sessions/{self.session_id}/cancelled")
                if r.status_code == 200:
                    return bool(r.json().get("cancelled", False))
        except Exception:
            pass
        return False

    async def check_preempted(self) -> dict:
        try:
            if self.task_id:
                r = await self.http.get(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempted"
                )
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {"preempted": False, "message": "", "attachments": []}

    async def consume_preempt(self) -> None:
        """通知 supervisor 已消费 preempt message，防止重复注入。"""
        try:
            if self.task_id:
                await self.http.post(
                    f"{self.supervisor_url}/v1/tasks/{self.task_id}/preempt_consumed"
                )
        except Exception:
            pass
