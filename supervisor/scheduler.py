"""轻量定时调度器。

每分钟醒一次，扫描 data/schedules.yaml，到时间的往 inbound 队列注入消息。
调度线程挂了不影响 Supervisor 主进程。
"""
# 同时负责系统级定时任务（如记忆整理 dream），配置在 runtime.yaml 中。

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

from engine.context_store import cleanup_old_contexts
from clonoth_runtime import get_bool, get_int, get_str, load_runtime_config


if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  schedules.yaml CRUD
# ---------------------------------------------------------------------------

def _schedules_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "schedules.yaml"


def load_schedules(workspace_root: Path) -> list[dict[str, Any]]:
    p = _schedules_path(workspace_root)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    items = data.get("schedules")
    if not isinstance(items, list):
        return []
    return [s for s in items if isinstance(s, dict)]


def save_schedules(workspace_root: Path, schedules: list[dict[str, Any]]) -> None:
    p = _schedules_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"schedules": schedules}, sort_keys=False, allow_unicode=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
#  Cron 匹配（5 字段：minute hour day month weekday）
# ---------------------------------------------------------------------------

def _match_field(field: str, value: int, max_val: int) -> bool:
    """判断 cron 单个字段是否匹配。支持 * / , - 和 */step。"""
    field = field.strip()
    if field == "*":
        return True

    # */step
    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False

    # 逗号分隔
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                if int(lo) <= value <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue

    return False


def cron_match(expr: str, dt: datetime) -> bool:
    """判断 5 字段 cron 表达式是否匹配指定时间。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, dt.minute, 59)
        and _match_field(hour, dt.hour, 23)
        and _match_field(day, dt.day, 31)
        and _match_field(month, dt.month, 12)
        and _match_field(weekday, dt.weekday(), 6)  # 0=Monday
    )


# ---------------------------------------------------------------------------
#  调度线程
# ---------------------------------------------------------------------------

class SchedulerThread:
    """后台线程。每 60 秒检查一次 schedules.yaml，匹配的注入 inbound。"""

    def __init__(self, *, state: "SupervisorState", workspace_root: Path) -> None:
        self._state = state
        self._workspace_root = workspace_root
        self._last_fired: dict[str, str] = {}  # schedule_id -> "YYYY-MM-DD HH:MM"
        self._last_dream_fired: str = ""       # 防同分钟重复
        self._dream_last_session_count: int = 0  # 上次 dream 时的 session 数
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        log.info("[scheduler] started")

    def _loop(self) -> None:
        tick_count = 0
        while True:
            try:
                self._tick()
            except Exception as e:
                log.warning(f"[scheduler] tick error: {e}")
            tick_count += 1
            # 每 30 分钟清理一次旧上下文快照（30 ticks × 60s = 30min）
            if tick_count % 30 == 0:
                self._cleanup()
            time.sleep(60)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        now_key = now.strftime("%Y-%m-%d %H:%M")

        schedules = load_schedules(self._workspace_root)

        for s in schedules:
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            if not s.get("enabled", True):
                continue

            cron_expr = str(s.get("cron") or "").strip()
            if not cron_expr:
                continue

            # 防止同一分钟重复触发
            if self._last_fired.get(sid) == now_key:
                continue

            if not cron_match(cron_expr, now):
                continue

            # 匹配成功，注入 inbound
            self._last_fired[sid] = now_key
            once = bool(s.get("once", False))

            text = str(s.get("text") or f"[scheduled:{sid}]").strip()
            conv_key = str(s.get("conversation_key") or f"scheduler:{sid}").strip()
            msg_id = f"scheduler:{sid}:{uuid.uuid4()}"
            entry_node_id = str(s.get("entry_node_id") or "").strip()

            # 从 conversation_key 提取 channel（如 "discord:123" -> "discord"）
            if ":" in conv_key:
                channel = conv_key.split(":", 1)[0]
            else:
                channel = "scheduler"

            try:
                session_id = self._state.get_or_create_session(
                    channel=channel, conversation_key=conv_key,
                )
                evt = self._state.eventlog.append(
                    session_id=session_id,
                    component="scheduler",
                    type_="inbound_message",
                    payload={
                        "channel": channel,
                        "conversation_key": conv_key,
                        "message_id": msg_id,
                        "text": text,
                        "schedule_id": sid,
                        "entry_node_id": entry_node_id,
                    },
                )
                self._state.record_inbound_message_event(evt)
                log.info(f"[scheduler] fired: {sid} -> session={session_id}")
            except Exception as e:
                log.warning(f"[scheduler] inject failed for {sid}: {e}")

            if once:
                self._remove_schedule(sid)

        # 系统级定时任务：记忆整理（dream）
        self._tick_dream(now, now_key)

    def _remove_schedule(self, sid: str) -> None:
        try:
            items = load_schedules(self._workspace_root)
            items = [s for s in items if str(s.get("id") or "").strip() != sid]
            save_schedules(self._workspace_root, items)
            log.info(f"[scheduler] removed once-schedule: {sid}")
        except Exception as e:
            log.warning(f"[scheduler] failed to remove once-schedule {sid}: {e}")

    def _tick_dream(self, now: datetime, now_key: str) -> None:
        """系统级定时任务：记忆整理（dream）。配置在 runtime.yaml memory.dream 段。"""
        runtime_cfg = load_runtime_config(self._workspace_root)
        if not get_bool(runtime_cfg, "memory.dream.enabled", False):
            return

        cron_expr = get_str(runtime_cfg, "memory.dream.cron", "0 3 * * *").strip()
        if not cron_expr:
            return

        # 防止同一分钟重复触发
        if self._last_dream_fired == now_key:
            return

        if not cron_match(cron_expr, now):
            return

        # 可选门控：距上次 dream 后是否有足够的 session 活动
        min_sessions = get_int(runtime_cfg, "memory.dream.min_sessions", 0, min_value=0, max_value=1000)
        if min_sessions > 0:
            with self._state._lock:
                current_session_count = len(self._state.sessions)
            new_sessions = current_session_count - self._dream_last_session_count
            if new_sessions < min_sessions:
                log.debug(f"[scheduler] dream skip: {new_sessions} new sessions, need {min_sessions}")
                return

        self._last_dream_fired = now_key

        node_id = get_str(runtime_cfg, "memory.dream.node_id", "system.dream").strip()
        conv_key = get_str(runtime_cfg, "memory.dream.conversation_key", "system:dream").strip()
        channel = conv_key.split(":", 1)[0] if ":" in conv_key else "system"
        text = f"[auto_dream] 定期记忆整理 ({now.strftime('%Y-%m-%d %H:%M UTC')})"
        msg_id = f"dream:{uuid.uuid4()}"

        try:
            session_id = self._state.get_or_create_session(
                channel=channel, conversation_key=conv_key,
            )
            evt = self._state.eventlog.append(
                session_id=session_id,
                component="scheduler",
                type_="inbound_message",
                payload={
                    "channel": channel,
                    "conversation_key": conv_key,
                    "message_id": msg_id,
                    "text": text,
                    "entry_node_id": node_id,
                    "_system_task": True,
                },
            )
            self._state.record_inbound_message_event(evt)
            # 更新 session 计数游标
            with self._state._lock:
                self._dream_last_session_count = len(self._state.sessions)
            log.info(f"[scheduler] dream fired -> session={session_id}")
        except Exception as e:
            log.warning(f"[scheduler] dream inject failed: {e}")

    def _cleanup(self) -> None:
        """定期清理旧的上下文快照和已完成 task 的内存。"""
        try:
            from .types import TaskStatus

            # 在锁内收集 keep_refs 快照
            with self._state._lock:
                keep_refs: set[str] = set()
                for task in self._state.tasks.values():
                    if task.status not in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}:
                        ref = str(task.input.get("context_ref") or "").strip()
                        if ref:
                            keep_refs.add(ref)
                        cref = str(task.continuation.get("resume_context_ref") or "").strip()
                        if cref:
                            keep_refs.add(cref)
                        for frame in (task.continuation.get("resume_stack") or []):
                            if isinstance(frame, dict):
                                fref = str(frame.get("context_ref") or "").strip()
                                if fref:
                                    keep_refs.add(fref)

            # 文件清理在锁外执行（IO 操作）
            count = cleanup_old_contexts(
                self._workspace_root,
                max_age_seconds=3600.0,  # 1 小时前的快照
                keep_refs=keep_refs,
            )
            if count > 0:
                log.info(f"[scheduler] cleaned up {count} old context snapshots")

            # 在锁内清理内存中已终结的旧 task（保留最近 200 个）
            with self._state._lock:
                terminal = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
                finished_ids = [
                    tid for tid in self._state._task_order
                    if tid in self._state.tasks and self._state.tasks[tid].status in terminal
                ]
                if len(finished_ids) > 200:
                    to_remove = finished_ids[:-200]
                    for tid in to_remove:
                        self._state.tasks.pop(tid, None)
                    self._state._task_order = [
                        tid for tid in self._state._task_order if tid in self._state.tasks
                    ]
                    log.info(f"[scheduler] pruned {len(to_remove)} old tasks from memory")

                # 回收超时的 running 状态 task（安全网，防止 worker 崩溃后 task 永久孤立）
                _now_ts = datetime.now(timezone.utc)
                _stale_cutoff = _now_ts - timedelta(minutes=60)
                _stale_ids = [
                    tid for tid, t in self._state.tasks.items()
                    if t.status == TaskStatus.running
                    and t.updated_at < _stale_cutoff
                ]
                for tid in _stale_ids:
                    task = self._state.tasks.get(tid)
                    if task is None or task.status != TaskStatus.running:
                        continue
                    _stale_min = int((_now_ts - task.updated_at).total_seconds() / 60)
                    task.status = TaskStatus.failed
                    task.result = {
                        "action": "fail",
                        "node_id": task.node_id or "",
                        "error": f"任务运行超时（{_stale_min} 分钟无响应，疑似 worker 崩溃）",
                    }
                    task.updated_at = _now_ts
                    task.lease_expires_at = None
                    self._state._event_task_snapshot("task_completed", task, component="supervisor")
                    self._state._route_completed_task_locked(task)
                    log.warning(f"[scheduler] reaped stale running task {tid} (node={task.node_id}, stale {_stale_min}min)")
        except Exception as e:
            log.warning(f"[scheduler] cleanup error: {e}")

