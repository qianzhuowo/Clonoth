from __future__ import annotations

"""NapCat / OneBot 11 reverse WebSocket adapter for Clonoth.

This adapter is intentionally standalone: it does not require NoneBot2.  It
implements a reverse WebSocket endpoint for NapCat and talks to Clonoth
Supervisor through HTTP.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Deque
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

LOG = logging.getLogger("onebot11_adapter")

_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")
_REACT_RE = re.compile(r"\[REACT:([^\]]+)\]")
_AT_OUT_RE = re.compile(r"\[at:(\d+|all)\]|\[CQ:at,qq=(\d+|all)\]")
_QQ_EMOJI_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")
_DC_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.*?)\*\*", re.DOTALL)
_UNDERLINE_BOLD_RE = re.compile(r"__(.*?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", re.DOTALL)
_UNDERLINE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_SPLIT_SIGNAL = "[SPLIT]"
_SENSITIVE_ID_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int_set(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(v.strip()) for v in raw.split(",") if v.strip().isdigit()}


@dataclass(slots=True)
class AdapterConfig:
    supervisor_url: str
    host: str
    port: int
    ws_path: str
    access_token: str
    entry_node_id: str
    poll_interval: float
    workspace_root: Path
    state_file: Path
    action_timeout: float
    group_trigger: str
    trigger_prefixes: tuple[str, ...]
    group_history_max: int
    history_text_limit: int
    qq_message_limit: int
    enable_reactions: bool
    reply_to_trigger: bool
    enable_whitelist: bool
    enable_private: bool
    enable_qq_queue: bool
    qq_queue_interval: float
    qq_queue_reply_timeout: float
    qq_queue_workers: int
    qq_queue_wait_for_reply: bool
    enable_preempt: bool
    allowed_groups: set[int]
    allowed_private_users: set[int]
    admin_users: set[int]
    download_timeout: float
    enable_auto_like: bool
    auto_like_times: int
    custom_emoji_index_path: Path | None
    conversation_hash_secret: str

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        root = Path(__file__).resolve().parents[1]
        workspace_root = Path(os.getenv("ONEBOT_WORKSPACE_ROOT", str(root))).resolve()
        prefixes = tuple(p for p in os.getenv("ONEBOT_TRIGGER_PREFIXES", "!,！,/，/").split(",") if p)
        return cls(
            supervisor_url=os.getenv("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765").rstrip("/"),
            host=os.getenv("ONEBOT_ADAPTER_HOST", "0.0.0.0"),
            port=int(os.getenv("ONEBOT_ADAPTER_PORT", "8766")),
            ws_path=os.getenv("ONEBOT_WS_PATH", "/onebot/ws"),
            access_token=os.getenv("ONEBOT_ACCESS_TOKEN", ""),
            entry_node_id=os.getenv("ONEBOT_ENTRY_NODE_ID", os.getenv("CLONOTH_ENTRY_NODE", "qq.web_search")),
            poll_interval=float(os.getenv("ONEBOT_POLL_INTERVAL", "0.8")),
            workspace_root=workspace_root,
            state_file=Path(os.getenv("ONEBOT_STATE_FILE", str(workspace_root / "data" / "onebot11_adapter_state.json"))).resolve(),
            action_timeout=float(os.getenv("ONEBOT_ACTION_TIMEOUT", "15.0")),
            group_trigger=os.getenv("ONEBOT_GROUP_TRIGGER", "mention_only").strip().lower(),
            trigger_prefixes=prefixes,
            group_history_max=max(0, int(os.getenv("ONEBOT_GROUP_HISTORY_MAX", "20"))),
            history_text_limit=max(50, int(os.getenv("ONEBOT_HISTORY_TEXT_LIMIT", "400"))),
            qq_message_limit=max(500, int(os.getenv("ONEBOT_QQ_MESSAGE_LIMIT", "4300"))),
            enable_reactions=_env_bool("ONEBOT_ENABLE_REACTIONS", True),
            reply_to_trigger=_env_bool("ONEBOT_REPLY_TO_TRIGGER", False),
            enable_whitelist=_env_bool("ONEBOT_ENABLE_WHITELIST", False),
            enable_private=_env_bool("ONEBOT_ENABLE_PRIVATE", False),
            enable_qq_queue=_env_bool("ONEBOT_ENABLE_QQ_QUEUE", True),
            qq_queue_interval=max(0.0, float(os.getenv("ONEBOT_QQ_QUEUE_INTERVAL", "5.0"))),
            qq_queue_reply_timeout=max(1.0, float(os.getenv("ONEBOT_QQ_QUEUE_REPLY_TIMEOUT", "600.0"))),
            # [QQ parallel 2026-06-21] Why: a single queue worker that waits for a
            # reply can let one zombie/stuck conversation block every later QQ
            # message. How: allow multiple workers to drain the queue in parallel.
            # Purpose: QQ bot remains responsive even when one request is slow.
            qq_queue_workers=max(1, min(32, int(os.getenv("ONEBOT_QQ_QUEUE_WORKERS", "4")))),
            # [QQ parallel 2026-06-21] Why: waiting for an outbound reply before
            # draining the next queued QQ item lets one slow/stuck task serialize
            # chat. How: make reply waiting opt-in. Purpose: true multi-task QQ
            # parallelism by default.
            qq_queue_wait_for_reply=_env_bool("ONEBOT_QQ_QUEUE_WAIT_FOR_REPLY", False),
            # [QQ parallel 2026-06-21] Why: preempting a stale running task caused a
            # new user message to be swallowed by the old stuck task. How: make
            # preempt opt-in; default path always creates a fresh inbound task.
            # Purpose: user chat continues in parallel while old tasks are reaped.
            enable_preempt=_env_bool("ONEBOT_ENABLE_PREEMPT", False),
            allowed_groups=_env_int_set("ONEBOT_ALLOWED_GROUPS"),
            allowed_private_users=_env_int_set("ONEBOT_ALLOWED_PRIVATE_USERS"),
            admin_users=_env_int_set("ONEBOT_ADMIN_USERS"),
            download_timeout=float(os.getenv("ONEBOT_DOWNLOAD_TIMEOUT", "15.0")),
            enable_auto_like=_env_bool("ONEBOT_ENABLE_AUTO_LIKE", True),
            auto_like_times=max(1, min(20, int(os.getenv("ONEBOT_AUTO_LIKE_TIMES", "10")))),
            custom_emoji_index_path=(Path(p).expanduser().resolve() if (p := os.getenv("ONEBOT_CUSTOM_EMOJI_INDEX_PATH", os.getenv("CLONOTH_BQBS_PATH", "")).strip()) else None),
            conversation_hash_secret=os.getenv("ONEBOT_CONVERSATION_HASH_SECRET", "").strip(),
        )


@dataclass(slots=True)
class QueuedInbound:
    channel: str
    conversation_key: str
    merge_key: str
    message_id: str | None
    text: str
    attachments: list[dict[str, Any]]
    target: dict[str, Any]
    is_dm: bool


class OneBotConnection:
    """Owns the active NapCat WebSocket connection and pending action calls."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._websocket: WebSocket | None = None
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._timeout = timeout

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    async def attach(self, websocket: WebSocket) -> None:
        async with self._lock:
            old = self._websocket
            self._websocket = websocket
            if old is not None and old is not websocket:
                try:
                    await old.close(code=1000)
                except Exception:
                    pass
        LOG.info("NapCat connected")

    async def detach(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._websocket is websocket:
                self._websocket = None
                pending = list(self._pending.values())
                self._pending.clear()
                for fut in pending:
                    if not fut.done():
                        fut.set_exception(RuntimeError("OneBot WebSocket disconnected"))
        LOG.info("NapCat disconnected")

    def handle_response(self, data: dict[str, Any]) -> bool:
        echo = data.get("echo")
        if echo is None:
            return False
        fut = self._pending.pop(str(echo), None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(data)
        return True

    async def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        websocket = self._websocket
        if websocket is None:
            raise RuntimeError("NapCat is not connected")

        echo = f"clonoth-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = fut
        payload = {"action": action, "params": params, "echo": echo}

        try:
            async with self._send_lock:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            self._pending.pop(echo, None)


class ClonothOneBotAdapter:
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config
        self.connection = OneBotConnection(timeout=config.action_timeout)
        self.http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._state_lock = asyncio.Lock()
        self.session_to_conversation: dict[str, str] = {}
        self.conversation_to_session: dict[str, str] = {}
        self.inbound_to_conversation: dict[int, str] = {}
        self.session_targets: dict[str, dict[str, Any]] = {}
        self.inbound_targets: dict[int, dict[str, Any]] = {}
        self.conversation_last_user: dict[str, int] = {}
        self.after_seq = 0
        self.group_history: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=self.config.group_history_max))
        self._anon_users: dict[str, str] = {}
        self._anon_groups: dict[str, str] = {}
        self._anon_user_reverse: dict[str, str] = {}
        self._anon_group_reverse: dict[str, str] = {}
        self._anon_conversation_to_real: dict[str, str] = {}
        self._qq_queue: Deque[QueuedInbound] = deque()
        self._qq_queue_by_key: dict[str, QueuedInbound] = {}
        self._reply_message_cache: dict[str, dict[str, Any]] = {}
        self._reply_message_cache_order: Deque[str] = deque()
        self._auto_like_today: dict[int, str] = {}
        self._custom_emoji_names = self.load_custom_emoji_names(config.custom_emoji_index_path)
        self._custom_face_cache: list[Any] | None = None
        self._qq_queue_condition = asyncio.Condition()
        self._qq_waiting_replies: dict[str, asyncio.Event] = {}
        self.load_state()

    @staticmethod
    def load_custom_emoji_names(path: Path | None) -> list[str]:
        """Load QQ custom emoji names used to map [QQ_EMOJI:name] to fetch_custom_face items."""
        if path is None:
            return []
        try:
            names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            LOG.warning("custom emoji index file not found: %s", path)
            return []
        except OSError:
            LOG.warning("failed to read custom emoji index file: %s", path, exc_info=True)
            return []
        LOG.info("loaded %s QQ custom emoji names from %s", len(names), path)
        return names

    def load_state(self) -> None:
        path = self.config.state_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self.after_seq = max(0, int(data.get("after_seq") or 0))
            self.session_to_conversation = {
                str(k): str(v)
                for k, v in dict(data.get("session_to_conversation") or {}).items()
                if self._is_qq_conversation_key(str(v))
            }
            self.conversation_to_session = {
                str(k): str(v)
                for k, v in dict(data.get("conversation_to_session") or {}).items()
                if self._is_qq_conversation_key(str(k))
            }
            self.inbound_to_conversation = {
                int(k): str(v)
                for k, v in dict(data.get("inbound_to_conversation") or {}).items()
                if str(k).isdigit() and self._is_qq_conversation_key(str(v))
            }
            self.session_targets = {
                str(k): dict(v)
                for k, v in dict(data.get("session_targets") or {}).items()
                if isinstance(v, dict)
            }
            self.inbound_targets = {
                int(k): dict(v)
                for k, v in dict(data.get("inbound_targets") or {}).items()
                if str(k).isdigit() and isinstance(v, dict)
            }
            self.conversation_last_user = {
                str(k): int(v)
                for k, v in dict(data.get("conversation_last_user") or {}).items()
                if self._is_qq_conversation_key(str(k))
            }
            LOG.info("loaded OneBot routing state from %s", path)
        except Exception:
            LOG.exception("failed to load OneBot routing state from %s", path)

    async def save_state(self) -> None:
        async with self._state_lock:
            path = self.config.state_file
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 2,
                "after_seq": self.after_seq,
                "session_to_conversation": self.session_to_conversation,
                "conversation_to_session": self.conversation_to_session,
                "inbound_to_conversation": {str(k): v for k, v in self.inbound_to_conversation.items()},
                "session_targets": self.session_targets,
                "inbound_targets": {str(k): v for k, v in self.inbound_targets.items()},
                "conversation_last_user": self.conversation_last_user,
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    async def remember_route(
        self,
        *,
        session_id: str = "",
        conversation_key: str = "",
        inbound_seq: int = 0,
        target: dict[str, Any] | None = None,
    ) -> None:
        if session_id and conversation_key:
            self.session_to_conversation[session_id] = conversation_key
            self.conversation_to_session[conversation_key] = session_id
        if inbound_seq and conversation_key:
            self.inbound_to_conversation[int(inbound_seq)] = conversation_key
        if target:
            safe_target = self._serializable_target(target)
            if session_id:
                self.session_targets[session_id] = safe_target
            if inbound_seq:
                self.inbound_targets[int(inbound_seq)] = safe_target
            user_id = safe_target.get("last_user_id")
            if conversation_key and user_id is not None:
                self.conversation_last_user[conversation_key] = int(user_id)
        if conversation_key or target:
            await self.save_state()

    @staticmethod
    def _serializable_target(target: dict[str, Any]) -> dict[str, Any]:
        allowed = {"type", "group_id", "user_id", "last_message_id", "last_user_id", "conversation_key"}
        out: dict[str, Any] = {}
        for key in allowed:
            if key in target and target[key] is not None:
                out[key] = target[key]
        return out

    async def close(self) -> None:
        await self.http.aclose()

    async def handle_onebot_event(self, data: dict[str, Any]) -> None:
        post_type = data.get("post_type")
        if post_type == "message":
            await self.handle_message_event(data)
        elif post_type == "meta_event":
            LOG.debug("meta_event: %s", data.get("meta_event_type"))
        else:
            LOG.debug("ignored OneBot event post_type=%s", post_type)

    async def handle_message_event(self, data: dict[str, Any]) -> None:
        message_type = str(data.get("message_type") or "")
        user_id = self._to_int(data.get("user_id"))
        group_id = self._to_int(data.get("group_id"))
        self_id = str(data.get("self_id") or "")

        text, image_sources = self.extract_message(data.get("message"), data.get("raw_message"), bot_self_id=self_id)
        reply_message_id = self._extract_reply_message_id(data.get("message"), data.get("raw_message"))
        message_id = self._to_int(data.get("message_id"))
        if reply_message_id is not None:
            LOG.debug("detected reply segment message_id=%s current_message_id=%s", reply_message_id, message_id)
        self._remember_message_for_reply_context(data)
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        nickname = self._sanitize_name(str(sender.get("card") or sender.get("nickname") or user_id or "未知成员"))

        if self._handle_admin_approval_command(data, text):
            return

        if message_type == "group" and group_id is not None and user_id is not None:
            if not self._group_is_allowed(group_id):
                LOG.info("ignored group message because group is not whitelisted: group=%s user=%s", group_id, user_id)
                return
            # Auto-like: 自动给发言者名片点赞，每人每天一次，不依赖 AI 触发
            if self.config.enable_auto_like:
                asyncio.create_task(self._auto_like_user(user_id))
            conversation_key = f"qq_group:{group_id}"
            target = {
                "type": "group",
                "group_id": group_id,
                "last_message_id": message_id,
                "last_user_id": user_id,
                "conversation_key": conversation_key,
                "platform_user_id": str(user_id),
                "platform_is_admin": self._is_admin_user(user_id),
                "platform_name": "qq",
            }
            user_text = text.strip() or "你好"
            self._anonymize_conversation_key(conversation_key)
            should_trigger = self._group_should_trigger(data, user_text, self_id)
            self._record_group_message(group_id, nickname, user_id, user_text, data.get("time"))
            if not should_trigger:
                LOG.debug("group message recorded but not triggered group=%s", group_id)
                return
            attachments = await self.collect_image_attachments(image_sources, self._anonymize_conversation_key(conversation_key))
            inbound_text = await self._build_group_inbound_text(data, group_id, nickname, user_id, user_text, attachments, conversation_key, reply_message_id)
            await self.enqueue_or_submit(
                channel="qq_group",
                conversation_key=conversation_key,
                message_id=str(message_id) if message_id is not None else None,
                text=inbound_text,
                attachments=attachments,
                target=target,
                is_dm=False,
            )
            return

        if message_type == "private" and user_id is not None:
            if not self.config.enable_private:
                LOG.info("ignored private message because ONEBOT_ENABLE_PRIVATE is false: user=%s", user_id)
                return
            if not self._private_is_allowed(user_id):
                LOG.info("ignored private message because user is not whitelisted: user=%s", user_id)
                return
            conversation_key = f"qq_private:{user_id}"
            target = {
                "type": "private",
                "user_id": user_id,
                "last_message_id": message_id,
                "last_user_id": user_id,
                "conversation_key": conversation_key,
                "platform_user_id": str(user_id),
                "platform_is_admin": self._is_admin_user(user_id),
                "platform_name": "qq",
            }
            user_text = text.strip() or "你好"
            attachments = await self.collect_image_attachments(image_sources, self._anonymize_conversation_key(conversation_key))
            inbound_text = self._build_private_inbound_text(nickname, user_id, user_text, attachments)
            await self.enqueue_or_submit(
                channel="qq_private",
                conversation_key=conversation_key,
                message_id=str(message_id) if message_id is not None else None,
                text=inbound_text,
                attachments=attachments,
                target=target,
                is_dm=True,
            )
            return

        LOG.debug("ignored unsupported message event: %s", data)

    async def enqueue_or_submit(
        self,
        *,
        channel: str,
        conversation_key: str,
        message_id: str | None,
        text: str,
        attachments: list[dict[str, Any]],
        target: dict[str, Any],
        is_dm: bool,
    ) -> None:
        if not self.config.enable_qq_queue:
            await self.submit_or_preempt(
                channel=channel,
                conversation_key=conversation_key,
                message_id=message_id,
                text=text,
                attachments=attachments,
                target=target,
                is_dm=is_dm,
            )
            return

        item = QueuedInbound(
            channel=channel,
            conversation_key=conversation_key,
            merge_key=conversation_key,
            message_id=message_id,
            text=text,
            attachments=list(attachments or []),
            target=dict(target),
            is_dm=is_dm,
        )
        async with self._qq_queue_condition:
            existing = self._qq_queue_by_key.get(item.merge_key)
            if existing is not None:
                existing.text = self._merge_queued_text(existing.text, item.text)
                existing.attachments.extend(item.attachments)
                existing.message_id = item.message_id or existing.message_id
                existing.target = item.target
                LOG.info("merged QQ inbound into queue conversation=%s queue_size=%s", item.conversation_key, len(self._qq_queue))
            else:
                self._qq_queue.append(item)
                self._qq_queue_by_key[item.merge_key] = item
                LOG.info("queued QQ inbound conversation=%s queue_size=%s", item.conversation_key, len(self._qq_queue))
            self._qq_queue_condition.notify()

    async def qq_queue_worker_forever(self) -> None:
        while True:
            async with self._qq_queue_condition:
                while not self._qq_queue:
                    await self._qq_queue_condition.wait()
                item = self._qq_queue.popleft()
                self._qq_queue_by_key.pop(item.merge_key, None)

            reply_event: asyncio.Event | None = None
            if self.config.qq_queue_wait_for_reply:
                reply_event = asyncio.Event()
                self._qq_waiting_replies[item.conversation_key] = reply_event
            try:
                await self.submit_or_preempt(
                    channel=item.channel,
                    conversation_key=item.conversation_key,
                    message_id=item.message_id,
                    text=item.text,
                    attachments=item.attachments,
                    target=item.target,
                    is_dm=item.is_dm,
                )
                if reply_event is not None:
                    try:
                        await asyncio.wait_for(reply_event.wait(), timeout=self.config.qq_queue_reply_timeout)
                        LOG.info("QQ queue item finished conversation=%s", item.conversation_key)
                    except asyncio.TimeoutError:
                        LOG.warning("QQ queue reply wait timed out conversation=%s timeout=%ss", item.conversation_key, self.config.qq_queue_reply_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("failed to process QQ queue item conversation=%s", item.conversation_key)
            finally:
                if reply_event is not None:
                    self._qq_waiting_replies.pop(item.conversation_key, None)

            if self.config.qq_queue_interval > 0:
                await asyncio.sleep(self.config.qq_queue_interval)

    @staticmethod
    def _merge_queued_text(old: str, new: str) -> str:
        return f"{old}\n\n【排队期间追加消息】\n{new}"

    def _is_admin_user(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.config.admin_users

    def _handle_admin_approval_command(self, data: dict[str, Any], text: str) -> bool:
        user_id = self._to_int(data.get("user_id"))
        if not self._is_admin_user(user_id):
            return False
        raw = (text or "").strip()
        if not raw:
            return False
        parts = raw.split()
        if len(parts) < 2:
            return False
        cmd = parts[0].lower()
        if cmd not in {"/approve", "/deny"}:
            return False
        approval_id = parts[1].strip()
        if not approval_id:
            return False
        decision = "allow" if cmd == "/approve" else "deny"
        asyncio.create_task(self._submit_approval_decision(data, approval_id, decision))
        return True

    async def _submit_approval_decision(self, data: dict[str, Any], approval_id: str, decision: str) -> None:
        try:
            resp = await self.http.post(
                f"{self.config.supervisor_url}/v1/approvals/{approval_id}",
                json={
                    "decision": decision,
                    "comment": f"via qq admin {data.get('user_id')}",
                },
            )
            ok = resp.status_code == 200
        except Exception:
            LOG.exception("QQ approval command failed approval_id=%s", approval_id)
            ok = False
        target = None
        message_type = str(data.get("message_type") or "")
        if message_type == "group" and self._to_int(data.get("group_id")) is not None:
            target = {
                "type": "group",
                "group_id": int(data["group_id"]),
            }
        elif self._to_int(data.get("user_id")) is not None:
            target = {
                "type": "private",
                "user_id": int(data["user_id"]),
            }
        if target:
            await self.send_onebot_message(
                target,
                f"{'✅' if ok else '❌'} approval {decision}: {approval_id[:12]}",
            )

    def _mark_qq_reply_finished(self, conversation_key: str) -> None:
        event = self._qq_waiting_replies.get(conversation_key)
        if event is not None:
            event.set()

    async def submit_or_preempt(
        self,
        *,
        channel: str,
        conversation_key: str,
        message_id: str | None,
        text: str,
        attachments: list[dict[str, Any]],
        target: dict[str, Any],
        is_dm: bool,
    ) -> None:
        if self.config.enable_preempt:
            if await self.try_preempt_running_task(conversation_key, text, attachments, target, is_dm=is_dm):
                return
        await self.submit_inbound(
            channel=channel,
            conversation_key=conversation_key,
            message_id=message_id,
            text=text,
            attachments=attachments,
            target=target,
        )

    async def submit_inbound(
        self,
        *,
        channel: str,
        conversation_key: str,
        message_id: str | None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        target: dict[str, Any] | None = None,
    ) -> None:
        # Route sessions through a stable, non-reversible key.  The previous
        # in-memory aliases (GroupA/UserA) could be re-assigned after adapter
        # restart, making a different QQ chat reuse an old Supervisor session.
        # Keep the real conversation_key only inside adapter state for outbound
        # routing, while Supervisor persists the stable hash key.
        stable_conversation_key = self._stable_conversation_key(conversation_key)
        payload: dict[str, Any] = {
            "channel": channel,
            "conversation_key": stable_conversation_key,
            "message_id": message_id,
            "text": self._anonymize_text_for_ai(text),
            "attachments": attachments or [],
            "use_context": True,
            "platform_auth": {
                "platform": str(target.get("platform_name") or "qq"),
                "user_id": str(target.get("platform_user_id") or ""),
                "is_admin": bool(target.get("platform_is_admin")),
            },
        }
        if self.config.entry_node_id:
            payload["entry_node_id"] = self.config.entry_node_id

        resp = await self.http.post(f"{self.config.supervisor_url}/v1/inbound", json=payload)
        resp.raise_for_status()
        data = resp.json()
        session_id = str(data.get("session_id") or "")
        inbound_seq = int(data.get("inbound_seq") or 0)
        await self.remember_route(session_id=session_id, conversation_key=conversation_key, inbound_seq=inbound_seq, target=target)
        if self.config.enable_reactions and target:
            await self.set_reaction(target.get("last_message_id"), "281", True)
        LOG.info("inbound accepted channel=%s conversation=%s session=%s inbound_seq=%s", channel, conversation_key, session_id, inbound_seq)

    async def try_preempt_running_task(
        self,
        conversation_key: str,
        text: str,
        attachments: list[dict[str, Any]],
        target: dict[str, Any],
        *,
        is_dm: bool,
    ) -> bool:
        session_id = self.conversation_to_session.get(conversation_key, "")
        if not session_id:
            return False
        try:
            resp = await self.http.get(f"{self.config.supervisor_url}/v1/sessions/{session_id}/running_tasks")
            if resp.status_code != 200:
                return False
            tasks = resp.json().get("tasks", [])
        except Exception:
            LOG.debug("preempt skipped: failed to query running tasks", exc_info=True)
            return False

        for task in tasks:
            if not isinstance(task, dict) or not task.get("is_user_entry"):
                continue
            if not is_dm:
                old_user = self.conversation_last_user.get(conversation_key)
                new_user = target.get("last_user_id")
                if old_user is not None and new_user is not None and int(old_user) != int(new_user):
                    continue
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            body: dict[str, Any] = {"message": self._anonymize_text_for_ai(text), "attachments": attachments or []}
            try:
                preempt_resp = await self.http.post(f"{self.config.supervisor_url}/v1/tasks/{task_id}/preempt", json=body)
                ok = preempt_resp.status_code == 200 and bool(preempt_resp.json().get("ok"))
            except Exception:
                LOG.exception("preempt failed for task=%s", task_id)
                ok = False
            if ok:
                await self.remember_route(session_id=session_id, conversation_key=conversation_key, target=target)
                if self.config.enable_reactions:
                    await self.set_reaction(target.get("last_message_id"), "281", True)
                LOG.info("preempted running task=%s conversation=%s", task_id[:8], conversation_key)
                return True
        return False

    def extract_message(self, message: Any, raw_message: Any = None, *, bot_self_id: str = "") -> tuple[str, list[str]]:
        image_sources: list[str] = []
        if isinstance(message, list):
            parts: list[str] = []
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or "")
                seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                if seg_type == "text":
                    parts.append(str(seg_data.get("text") or ""))
                elif seg_type == "at":
                    qq = str(seg_data.get("qq") or "")
                    if bot_self_id and qq == bot_self_id:
                        continue
                    parts.append("@全体成员" if qq.lower() == "all" else f"@{qq}")
                elif seg_type == "reply":
                    rid = seg_data.get("id")
                    parts.append(f"[回复:{rid}]")
                elif seg_type == "image":
                    src = str(seg_data.get("url") or seg_data.get("path") or seg_data.get("file") or "").strip()
                    if src:
                        image_sources.append(src)
                    parts.append("[图片]")
                elif seg_type == "face":
                    parts.append(f"[QQ表情:{seg_data.get('id', '')}]" if seg_data.get("id") else "[QQ表情]")
                elif seg_type == "record":
                    parts.append("[语音]")
                elif seg_type == "video":
                    parts.append("[视频]")
                elif seg_type == "json":
                    parts.append(f"[JSON消息:{seg_data.get('data', '')}]")
                elif seg_type:
                    parts.append(f"[{seg_type}]")
            return "".join(parts).strip(), image_sources

        if isinstance(message, str):
            for match in _CQ_RE.finditer(message):
                if match.group(1) != "image":
                    continue
                params = self._parse_cq_params(match.group(2) or "")
                src = str(params.get("url") or params.get("path") or params.get("file") or "").strip()
                if src:
                    image_sources.append(src)
            return self._format_cq_message(message).strip(), image_sources
        if isinstance(raw_message, str):
            for match in _CQ_RE.finditer(raw_message):
                if match.group(1) != "image":
                    continue
                params = self._parse_cq_params(match.group(2) or "")
                src = str(params.get("url") or params.get("path") or params.get("file") or "").strip()
                if src:
                    image_sources.append(src)
            return self._format_cq_message(raw_message).strip(), image_sources
        if raw_message is not None:
            return str(raw_message).strip(), image_sources
        return "", image_sources

    def _extract_reply_message_id(self, message: Any, raw_message: Any = None) -> Any | None:
        def pick_id(seg_data: dict[str, Any]) -> Any | None:
            for key in ("id", "message_id", "messageId", "messageid", "message_seq", "seq"):
                value = seg_data.get(key)
                if value is not None and str(value).strip():
                    return value
            return None

        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict) or seg.get("type") != "reply":
                    continue
                seg_data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                reply_id = pick_id(seg_data)
                if reply_id is not None:
                    return reply_id
                LOG.debug("reply segment found but no usable message id: %s", seg_data)

        candidates: list[str] = []
        if isinstance(message, str):
            candidates.append(message)
        if isinstance(raw_message, str):
            candidates.append(raw_message)
        for candidate in candidates:
            for match in _CQ_RE.finditer(candidate):
                if match.group(1) != "reply":
                    continue
                reply_id = self._parse_cq_params(match.group(2) or "").get("id")
                if reply_id is not None and str(reply_id).strip():
                    return reply_id
        return None

    def _remember_message_for_reply_context(self, data: dict[str, Any]) -> None:
        raw_message_id = data.get("message_id")
        if raw_message_id is None or not str(raw_message_id).strip():
            return
        message_id = str(raw_message_id)
        self._reply_message_cache[message_id] = {
            "message": data.get("message"),
            "raw_message": data.get("raw_message"),
            "sender": data.get("sender") if isinstance(data.get("sender"), dict) else {},
            "user_id": data.get("user_id"),
            "time": data.get("time"),
        }
        if message_id not in self._reply_message_cache_order:
            self._reply_message_cache_order.append(message_id)
        while len(self._reply_message_cache_order) > 1000:
            old_id = self._reply_message_cache_order.popleft()
            self._reply_message_cache.pop(old_id, None)

    async def _get_reply_message(self, reply_message_id: Any) -> dict[str, Any] | None:
        message_id_param = self._to_int(reply_message_id) if self._to_int(reply_message_id) is not None else reply_message_id
        try:
            resp = await self.connection.call_action("get_msg", {"message_id": message_id_param})
        except Exception:
            LOG.warning("get_msg failed for reply_message_id=%s", reply_message_id, exc_info=True)
            return None

        reply = resp.get("data") if isinstance(resp.get("data"), dict) else None
        if reply and (resp.get("status") == "ok" or resp.get("retcode") in (0, None)):
            return reply
        LOG.warning(
            "get_msg returned unusable reply data for message_id=%s status=%r retcode=%r message=%r data_type=%s",
            reply_message_id, resp.get("status"), resp.get("retcode"), resp.get("message") or resp.get("wording"), type(resp.get("data")).__name__,
        )
        return None

    def _format_cq_message(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            cq_type = match.group(1)
            params = self._parse_cq_params(match.group(2) or "")
            if cq_type == "at":
                qq = params.get("qq", "")
                return "@全体成员" if qq.lower() == "all" else f"@{qq}"
            if cq_type == "reply":
                return f"[回复:{params.get('id', '')}]"
            if cq_type == "image":
                return "[图片]"
            if cq_type == "face":
                return f"[QQ表情:{params.get('id', '')}]"
            if cq_type == "record":
                return "[语音]"
            return match.group(0)
        return _CQ_RE.sub(repl, text)

    @staticmethod
    def _parse_cq_params(raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in raw.split(","):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            out[key] = value
        return out

    async def collect_image_attachments(self, image_sources: list[str], conversation_key: str) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for src in image_sources:
            att = await self.image_source_to_attachment(src, conversation_key)
            if att:
                attachments.append(att)
        return attachments

    async def image_source_to_attachment(self, src: str, conversation_key: str) -> dict[str, Any] | None:
        if not src:
            return None
        if src.startswith(("http://", "https://")):
            return await self.download_image_attachment(src, conversation_key)
        if src.startswith("file://"):
            src = src[7:]
        if src.startswith("base64://"):
            LOG.debug("base64 image inbound is not implemented; leaving as text only")
            return None
        path = Path(src)
        if path.is_absolute() and path.exists():
            return self.local_image_attachment(path, conversation_key)
        if not path.is_absolute():
            workspace_path = (self.config.workspace_root / path).resolve()
            if workspace_path.exists():
                return self.local_image_attachment(workspace_path, conversation_key)
            cwd_path = path.resolve()
            if cwd_path.exists():
                return self.local_image_attachment(cwd_path, conversation_key)
        try:
            resp = await self.connection.call_action("get_image", {"file": src})
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            image_file = str(data.get("file") or "").strip()
            if image_file:
                image_path = Path(image_file)
                if image_path.is_absolute() and image_path.exists():
                    return self.local_image_attachment(image_path, conversation_key)
        except Exception:
            LOG.debug("get_image fallback failed for QQ image file=%s", src, exc_info=True)
        return None

    async def download_image_attachment(self, url: str, conversation_key: str) -> dict[str, Any] | None:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", conversation_key)
        att_dir = self.config.workspace_root / "data" / "attachments" / safe_key
        try:
            att_dir.mkdir(parents=True, exist_ok=True)
            resp = await self.http.get(url, timeout=self.config.download_timeout)
            resp.raise_for_status()
            if not resp.content:
                return None
            content_type = resp.headers.get("content-type", "")
            mime_type = self._guess_image_mime(url, content_type)
            ext = self._image_ext_from_url_or_mime(url, mime_type)
            path = att_dir / f"{uuid.uuid4().hex}{ext}"
            path.write_bytes(resp.content)
            rel_path = path.relative_to(self.config.workspace_root).as_posix()
            return {"type": "image", "path": rel_path, "mime_type": mime_type, "name": f"image{ext}"}
        except Exception:
            LOG.exception("failed to download QQ image: %s", url)
            return None

    def local_image_attachment(self, path: Path, conversation_key: str = "") -> dict[str, Any] | None:
        try:
            resolved = path.resolve()
            if not resolved.is_file():
                return None
            try:
                rel = resolved.relative_to(self.config.workspace_root)
                attachment_path = str(rel).replace("\\", "/")
                if attachment_path.lstrip("/").startswith("data/"):
                    return {"type": "image", "path": attachment_path, "mime_type": self._guess_image_mime(str(resolved)), "name": resolved.name}
            except ValueError:
                pass

            # NapCat 的 data.path 通常是适配器工作区外的本地缓存路径。Clonoth 只会把
            # data/ 下的附件注入给 LLM，所以这里必须复制进 workspace/data/attachments/，
            # 不能把外部绝对路径直接塞进 attachments。
            safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", conversation_key or "qq_image")
            att_dir = self.config.workspace_root / "data" / "attachments" / safe_key
            att_dir.mkdir(parents=True, exist_ok=True)
            ext = resolved.suffix.lower() if resolved.suffix.lower() in _IMAGE_SUFFIXES else self._image_ext_from_url_or_mime(str(resolved), self._guess_image_mime(str(resolved)))
            copied = att_dir / f"{uuid.uuid4().hex}{ext}"
            shutil.copyfile(resolved, copied)
            rel_path = copied.relative_to(self.config.workspace_root).as_posix()
            return {"type": "image", "path": rel_path, "mime_type": self._guess_image_mime(str(copied)), "name": resolved.name or f"image{ext}"}
        except Exception:
            LOG.exception("failed to import local QQ image attachment: %s", path)
            return None

    @staticmethod
    def _guess_image_mime(url: str, content_type: str = "") -> str:
        val = f"{url} {content_type}".lower()
        if "png" in val:
            return "image/png"
        if "gif" in val:
            return "image/gif"
        if "webp" in val:
            return "image/webp"
        if "bmp" in val:
            return "image/bmp"
        return "image/jpeg"

    @staticmethod
    def _image_ext_from_url_or_mime(url: str, mime_type: str) -> str:
        ext = Path((url or "").split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            return ext
        return {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp"}.get(mime_type, ".jpg")

    def _group_should_trigger(self, data: dict[str, Any], text: str, self_id: str) -> bool:
        mode = self.config.group_trigger
        if mode == "all":
            return True
        if mode in {"mention_only", "at", "at_only"}:
            return self._message_mentions_self(data.get("message"), self_id)
        if mode == "prefix":
            return text.startswith(self.config.trigger_prefixes)
        if mode == "mention":
            if self._message_mentions_self(data.get("message"), self_id):
                return True
            return text.startswith(self.config.trigger_prefixes)
        return self._message_mentions_self(data.get("message"), self_id)

    def _group_is_allowed(self, group_id: int) -> bool:
        if not self.config.enable_whitelist:
            return True
        return group_id in self.config.allowed_groups

    def _private_is_allowed(self, user_id: int) -> bool:
        if not self.config.enable_whitelist:
            return True
        return user_id in self.config.allowed_private_users

    @staticmethod
    def _message_mentions_self(message: Any, self_id: str) -> bool:
        if not self_id or not isinstance(message, list):
            return False
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                if str(data.get("qq") or "") == str(self_id):
                    return True
        return False

    @staticmethod
    def _alias_from_index(prefix: str, index: int) -> str:
        letters = ""
        index = max(0, index)
        while True:
            index, rem = divmod(index, 26)
            letters = chr(ord("A") + rem) + letters
            if index == 0:
                break
            index -= 1
        return f"{prefix}{letters}"

    def _anonymize_user_id(self, user_id: Any) -> str:
        real = str(user_id or "").strip()
        if not real:
            return "UserUnknown"
        alias = self._anon_users.get(real)
        if alias is None:
            alias = self._alias_from_index("User", len(self._anon_users))
            self._anon_users[real] = alias
            self._anon_user_reverse[alias] = real
        return alias

    def _anonymize_group_id(self, group_id: Any) -> str:
        real = str(group_id or "").strip()
        if not real:
            return "GroupUnknown"
        alias = self._anon_groups.get(real)
        if alias is None:
            alias = self._alias_from_index("Group", len(self._anon_groups))
            self._anon_groups[real] = alias
            self._anon_group_reverse[alias] = real
        return alias

    def _anonymize_conversation_key(self, conversation_key: str) -> str:
        parsed = self._parse_conversation_key(conversation_key)
        if not parsed:
            return self._anonymize_text_for_ai(conversation_key)
        action, _id_key, id_value = parsed
        if action == "send_group_msg":
            safe_key = f"qq_group:{self._anonymize_group_id(id_value)}"
        else:
            safe_key = f"qq_private:{self._anonymize_user_id(id_value)}"
        self._anon_conversation_to_real[safe_key] = conversation_key
        return safe_key

    def _deanonymize_conversation_key(self, conversation_key: str) -> str:
        return self._anon_conversation_to_real.get(conversation_key, conversation_key)

    def _stable_conversation_key(self, conversation_key: str) -> str:
        parsed = self._parse_conversation_key(conversation_key)
        if not parsed:
            digest = self._conversation_digest(conversation_key)
            return f"qq_unknown:{digest}"
        action, _id_key, id_value = parsed
        prefix = "qq_group" if action == "send_group_msg" else "qq_private"
        digest = self._conversation_digest(conversation_key)
        return f"{prefix}:{digest}"

    def _conversation_digest(self, conversation_key: str) -> str:
        raw = str(conversation_key or "").strip().encode("utf-8")
        secret = str(self.config.conversation_hash_secret or "").encode("utf-8")
        if secret:
            return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:24]
        return hashlib.sha256(raw).hexdigest()[:24]

    def _anonymize_text_for_ai(self, text: str) -> str:
        if not text:
            return ""
        safe = str(text)
        # 先替换已知群号/QQ号，避免 AI 侧看到真实 conversation key、@QQ 或历史记录里的号码。
        for real, alias in sorted(self._anon_groups.items(), key=lambda item: len(item[0]), reverse=True):
            safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
        for real, alias in sorted(self._anon_users.items(), key=lambda item: len(item[0]), reverse=True):
            safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
        # 对上下文里新出现的 5-12 位数字也做兜底匿名化，避免 raw_message/引用消息泄漏 QQ 号。
        return _SENSITIVE_ID_RE.sub(lambda m: self._anonymize_user_id(m.group(0)), safe)


    def _record_group_message(self, group_id: int, name: str, user_id: int, text: str, ts: Any) -> None:
        if self.config.group_history_max <= 0 or not text.strip():
            return
        safe_name = self._anonymize_text_for_ai(name)
        safe_user = self._anonymize_user_id(user_id)
        safe_text = self._anonymize_text_for_ai(self._compact_text(text))
        line = f"[{self._format_hhmm(ts)}] {safe_name}({safe_user}): {safe_text}"
        self.group_history[group_id].append(line)

    def _record_bot_reply(self, conversation_key: str, text: str) -> None:
        target = self._parse_conversation_key(conversation_key)
        if not target or target[0] != "send_group_msg":
            return
        group_id = target[2]
        cleaned = self.strip_output_markers(text).replace(_SPLIT_SIGNAL, " ")
        cleaned = _QQ_EMOJI_RE.sub(lambda m: f"[表情:{m.group(1)}]", cleaned)
        cleaned = self._compact_text(cleaned)
        if cleaned:
            self.group_history[group_id].append(f"[{self._format_hhmm(None)}] Bot: {self._anonymize_text_for_ai(cleaned)}")

    async def _build_group_inbound_text(self, data: dict[str, Any], group_id: int, name: str, user_id: int, user_text: str, attachments: list[dict[str, Any]], conversation_key: str, reply_message_id: Any | None = None) -> str:
        for att in attachments:
            user_text = user_text.replace("[图片]", f"[图片: {att['path']}]", 1)
        parts: list[str] = ["【群聊上下文记录】"]
        parts.extend(list(self.group_history[group_id])[-self.config.group_history_max:] or ["（暂无）"])
        reply_context, quoted_attachments = await self._build_reply_context(data, conversation_key, reply_message_id)
        if quoted_attachments:
            attachments.extend(quoted_attachments)
        if reply_context:
            parts.extend(["", "【当前消息引用】", reply_context])
        parts.extend([
            "",
            f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "【当前用户指令】",
            f"{self._anonymize_text_for_ai(name)}（{self._anonymize_user_id(user_id)}）: {self._anonymize_text_for_ai(user_text)}",
            "",
            "请根据以上上下文，执行当前用户的指令并给出回复。",
        ])
        return "\n".join(parts)

    def _build_private_inbound_text(self, name: str, user_id: int, user_text: str, attachments: list[dict[str, Any]]) -> str:
        for att in attachments:
            user_text = user_text.replace("[图片]", f"[图片: {att['path']}]", 1)
        return "\n".join([
            f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "【当前用户指令】",
            f"{self._anonymize_text_for_ai(name)}（{self._anonymize_user_id(user_id)}）: {self._anonymize_text_for_ai(user_text)}",
            "",
            "请根据以上上下文，执行当前用户的指令并给出回复。",
        ])

    async def _build_reply_context(self, data: dict[str, Any], conversation_key: str, reply_message_id: Any | None = None) -> tuple[str, list[dict[str, Any]]]:
        raw_reply = data.get("reply")
        reply = raw_reply if isinstance(raw_reply, dict) else None

        if not reply and reply_message_id is not None:
            reply = self._reply_message_cache.get(str(reply_message_id))
            if reply:
                LOG.debug("using cached reply message for message_id=%s", reply_message_id)

        if not reply and reply_message_id is not None:
            reply = await self._get_reply_message(reply_message_id)

        if not reply:
            if reply_message_id is not None:
                LOG.warning("reply context is empty: no reply data for message_id=%s", reply_message_id)
                return self._anonymize_text_for_ai(f"（无法获取引用消息内容：message_id={reply_message_id}）"), []
            return "", []
        msg, images = self.extract_message(reply.get("message"), reply.get("raw_message"))
        if not msg:
            LOG.warning("reply context is empty: fetched reply has no text/image content for message_id=%s", reply_message_id)
            if reply_message_id is not None:
                return self._anonymize_text_for_ai(f"（引用消息为空或暂不支持的消息类型：message_id={reply_message_id}）"), []
            return "", []
        quoted_attachments: list[dict[str, Any]] = []
        if images:
            quoted_attachments = await self.collect_image_attachments(
                images,
                self._anonymize_conversation_key(conversation_key),
            )
            for att in quoted_attachments:
                msg = msg.replace("[图片]", f"[图片: {att['path']}]", 1)
        sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else {}
        sender_id = sender.get("user_id") or reply.get("user_id") or ""
        name = self._sanitize_name(str(sender.get("card") or sender.get("nickname") or sender_id or "原作者"))
        return f"[{self._format_hhmm(reply.get('time'))}] {self._anonymize_text_for_ai(name)}: {self._anonymize_text_for_ai(self._compact_text(msg))}", quoted_attachments

    async def poll_events_forever(self) -> None:
        types = "outbound_message,intermediate_reply,context_reset,task_created,approval_requested,approval_decided"
        while True:
            try:
                resp = await self.http.get(f"{self.config.supervisor_url}/v1/events", params={"after_seq": self.after_seq, "types": types, "limit": 200})
                resp.raise_for_status()
                events = resp.json()
                for event in events:
                    seq = int(event.get("seq") or 0)
                    should_save_watermark = False
                    if seq > self.after_seq:
                        self.after_seq = seq
                        should_save_watermark = True
                    await self.handle_clonoth_event(event)
                    if should_save_watermark:
                        await self.save_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("failed to poll Clonoth events")
                await asyncio.sleep(max(1.0, self.config.poll_interval))
                continue
            await asyncio.sleep(self.config.poll_interval)

    async def handle_clonoth_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "context_reset":
            conversation_key = str(payload.get("conversation_key") or "")
            if conversation_key:
                self.conversation_to_session.pop(conversation_key, None)
                target = self._parse_conversation_key(conversation_key)
                if target and target[0] == "send_group_msg":
                    self.group_history.pop(target[2], None)
                await self.save_state()
            return
        if event_type == "task_created":
            await self._handle_task_created(event)
            return
        if event_type == "approval_requested":
            await self._handle_approval_requested(event)
            return
        if event_type == "approval_decided":
            await self._handle_approval_decided(event)
            return
        if event_type not in {"outbound_message", "intermediate_reply"}:
            return

        session_id = str(event.get("session_id") or "")
        source_inbound_seq = payload.get("source_inbound_seq")
        conversation_key = await self.resolve_conversation_for_outbound(session_id, source_inbound_seq)
        if not conversation_key:
            LOG.warning("cannot route %s: session=%s payload=%s", event_type, session_id, payload)
            return

        target = self.resolve_target(conversation_key, session_id, source_inbound_seq)
        if not target:
            LOG.warning("cannot resolve target for conversation=%s session=%s", conversation_key, session_id)
            return
        text = str(payload.get("text") or "")
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        await self.send_text_and_attachments(conversation_key, target, text, attachments, record_history=(event_type == "outbound_message"))
        if event_type == "outbound_message":
            self._mark_qq_reply_finished(conversation_key)

    async def _handle_approval_requested(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        approval_id = str(payload.get("approval_id") or "")
        if not approval_id:
            return
        session_id = str(payload.get("session_id") or event.get("session_id") or "")
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        operation = str(details.get("tool_name") or payload.get("operation") or "sensitive operation")
        conversation_key = await self.resolve_conversation_for_outbound(session_id, payload.get("source_inbound_seq"))
        if not conversation_key:
            return
        target = self.resolve_target(conversation_key, session_id, payload.get("source_inbound_seq"))
        if not target:
            return
        path = str(details.get("path") or details.get("cwd") or "")
        reason = str(details.get("reason") or payload.get("reason") or "")
        msg = [
            "🔒 需要 QQ 管理员审批",
            f"操作: {operation}",
            f"审批ID: {approval_id}",
        ]
        if path:
            msg.append(f"目标: {path}")
        if reason:
            msg.append(f"原因: {reason}")
        msg.append(f"通过: /approve {approval_id}")
        msg.append(f"拒绝: /deny {approval_id}")
        await self.send_onebot_message(target, "\n".join(msg))

    async def _handle_approval_decided(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        approval_id = str(payload.get("approval_id") or "")
        session_id = str(event.get("session_id") or payload.get("session_id") or "")
        conversation_key = await self.resolve_conversation_for_outbound(session_id, payload.get("source_inbound_seq"))
        if not conversation_key:
            return
        target = self.resolve_target(conversation_key, session_id, payload.get("source_inbound_seq"))
        if not target:
            return
        decision = str(payload.get("decision") or "")
        await self.send_onebot_message(target, f"审批已处理: {approval_id[:12]} -> {decision}")

    async def _handle_task_created(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        source = payload.get("source_inbound_seq")
        task_id = str(payload.get("task_id") or "")
        if not source or not task_id:
            return
        try:
            source_i = int(source)
        except Exception:
            return
        target = self.inbound_targets.get(source_i)
        if target and self.config.enable_reactions:
            await self.set_reaction(target.get("last_message_id"), "178", True)

    async def resolve_conversation_for_outbound(self, session_id: str, source_inbound_seq: Any) -> str:
        conversation_key = ""
        if source_inbound_seq is not None:
            try:
                conversation_key = self.inbound_to_conversation.get(int(source_inbound_seq), "")
            except Exception:
                conversation_key = ""
        if not conversation_key and session_id:
            conversation_key = self.session_to_conversation.get(session_id, "")
        if not conversation_key and session_id:
            conversation_key = await self.resolve_conversation_key_from_supervisor(session_id)
        return conversation_key

    def resolve_target(self, conversation_key: str, session_id: str, source_inbound_seq: Any) -> dict[str, Any] | None:
        if source_inbound_seq is not None:
            try:
                target = self.inbound_targets.get(int(source_inbound_seq))
                if target:
                    return dict(target)
            except Exception:
                pass
        if session_id and session_id in self.session_targets:
            target = dict(self.session_targets[session_id])
            # Session fallback 只能用于定位会话，不能复用旧触发消息引用；否则会回复到上一条/更早消息。
            target.pop("last_message_id", None)
            target.pop("last_user_id", None)
            return target
        parsed = self._parse_conversation_key(conversation_key)
        if not parsed:
            return None
        action, _id_key, id_value = parsed
        if action == "send_group_msg":
            return {"type": "group", "group_id": id_value, "conversation_key": conversation_key}
        return {"type": "private", "user_id": id_value, "conversation_key": conversation_key}

    async def resolve_conversation_key_from_supervisor(self, session_id: str) -> str:
        try:
            resp = await self.http.get(f"{self.config.supervisor_url}/v1/sessions/{session_id}")
            if resp.status_code == 200:
                conversation_key = self._conversation_key_from_session_item(resp.json(), session_id)
                if conversation_key:
                    await self.save_state()
                    return conversation_key
        except Exception:
            LOG.debug("precise session lookup failed for session=%s", session_id, exc_info=True)

        for channel in ("qq_group", "qq_private"):
            try:
                resp = await self.http.get(f"{self.config.supervisor_url}/v1/sessions", params={"channel": channel, "limit": 200})
                resp.raise_for_status()
                sessions = resp.json()
            except Exception:
                LOG.exception("failed to query Supervisor sessions for channel=%s", channel)
                continue
            if not isinstance(sessions, list):
                continue
            for item in sessions:
                conversation_key = self._conversation_key_from_session_item(item, session_id)
                if conversation_key:
                    await self.save_state()
                    return conversation_key
        return ""

    def _conversation_key_from_session_item(self, item: Any, session_id: str) -> str:
        if not isinstance(item, dict) or str(item.get("session_id") or "") != session_id:
            return ""
        raw_conversation_key = str(item.get("conversation_key") or "")
        conversation_key = self._deanonymize_conversation_key(raw_conversation_key)
        if not self._is_qq_conversation_key(conversation_key):
            return ""
        self.session_to_conversation[session_id] = conversation_key
        self.conversation_to_session[conversation_key] = session_id
        LOG.info("resolved route from Supervisor session=%s conversation=%s", session_id, conversation_key)
        return conversation_key

    async def send_text_and_attachments(self, conversation_key: str, target: dict[str, Any], text: str, attachments: list[dict[str, Any]], *, record_history: bool) -> None:
        clean_text, reactions = self.extract_reactions(text or "")
        if self.config.enable_reactions:
            for emoji_id in reactions:
                await self.set_reaction(target.get("last_message_id"), emoji_id, True)
        sent_text = False
        for part in self.split_output_text(clean_text):
            segments = await self.output_text_to_segments(part)
            if not segments:
                continue
            if self.config.reply_to_trigger and not sent_text and target.get("type") == "group" and target.get("last_message_id"):
                prefix = [{"type": "reply", "data": {"id": str(target["last_message_id"])}}]
                if target.get("last_user_id"):
                    prefix.append({"type": "at", "data": {"qq": str(target["last_user_id"])}})
                    prefix.append({"type": "text", "data": {"text": " "}})
                segments = prefix + segments
            await self.send_onebot_message(target, segments)
            sent_text = True
            await asyncio.sleep(0.5)
        if record_history and clean_text:
            self._record_bot_reply(conversation_key, clean_text)
        for att in attachments or []:
            if isinstance(att, dict):
                await self.send_attachment(target, att)

    async def send_onebot_message(self, target: dict[str, Any], message: list[dict[str, Any]] | str) -> None:
        if target.get("type") == "group":
            group_id = int(target["group_id"])
            if not self._group_is_allowed(group_id):
                LOG.info("blocked outbound group message because group is not whitelisted: group=%s", group_id)
                return
            await self.connection.call_action("send_group_msg", {"group_id": group_id, "message": message})
        elif target.get("type") == "private":
            user_id = int(target["user_id"])
            if not self.config.enable_private:
                LOG.info("blocked outbound private message because ONEBOT_ENABLE_PRIVATE is false: user=%s", user_id)
                return
            if not self._private_is_allowed(user_id):
                LOG.info("blocked outbound private message because user is not whitelisted: user=%s", user_id)
                return
            await self.connection.call_action("send_private_msg", {"user_id": user_id, "message": message})
        else:
            raise ValueError(f"unknown target: {target!r}")

    async def send_attachment(self, target: dict[str, Any], att: dict[str, Any]) -> None:
        if target.get("type") == "private" and not self.config.enable_private:
            LOG.info("blocked outbound private attachment because ONEBOT_ENABLE_PRIVATE is false: user=%s", target.get("user_id"))
            return
        if target.get("type") == "private" and not self._private_is_allowed(int(target["user_id"])):
            LOG.info("blocked outbound private attachment because user is not whitelisted: user=%s", target.get("user_id"))
            return
        if target.get("type") == "group" and not self._group_is_allowed(int(target["group_id"])):
            LOG.info("blocked outbound group attachment because group is not whitelisted: group=%s", target.get("group_id"))
            return
        path = self.resolve_attachment_path(att)
        filename = str(att.get("name") or att.get("filename") or (path.name if path else "attachment"))
        if path and path.exists() and (str(att.get("type") or "") == "image" or path.suffix.lower() in _IMAGE_SUFFIXES):
            await self.send_onebot_message(target, [{"type": "image", "data": {"file": self.path_to_file_uri(path)}}])
            return
        if path and path.exists():
            try:
                if target.get("type") == "group":
                    await self.connection.call_action("upload_group_file", {"group_id": int(target["group_id"]), "file": str(path.resolve()), "name": filename})
                    return
                if target.get("type") == "private":
                    await self.connection.call_action("upload_private_file", {"user_id": int(target["user_id"]), "file": str(path.resolve()), "name": filename})
                    return
            except Exception:
                LOG.exception("file upload failed: %s", path)
                await self.send_onebot_message(target, f"Clonoth 生成了文件：{filename}（上传失败）")
                return
        await self.send_onebot_message(target, f"Clonoth 生成了文件：{filename}（文件不存在或不可访问）")

    def split_output_text(self, text: str) -> list[str]:
        text = self.strip_output_markers(text)
        raw_parts = text.split(_SPLIT_SIGNAL) if text else []
        parts: list[str] = []
        for raw in raw_parts:
            part = raw.strip()
            if not part:
                continue
            while len(part) > self.config.qq_message_limit:
                suffix = "\n（内容过长，已拆分）"
                limit = self.config.qq_message_limit - len(suffix)
                parts.append(part[:limit] + suffix)
                part = part[limit:]
            if part:
                parts.append(part)
        return parts

    async def output_text_to_segments(self, text: str) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        pos = 0
        emoji_matches = [(m.start(), m.end(), "emoji", m) for m in _QQ_EMOJI_RE.finditer(text)]
        at_matches = [(m.start(), m.end(), "at", m) for m in _AT_OUT_RE.finditer(text)]
        for start, end, kind, match in sorted(emoji_matches + at_matches, key=lambda item: item[0]):
            if start < pos:
                continue
            before = text[pos:start]
            if before:
                segments.append({"type": "text", "data": {"text": before}})
            if kind == "at":
                qq = match.group(1) or match.group(2) or ""
                segments.append({"type": "at", "data": {"qq": qq}})
            else:
                name = match.group(1).strip()
                emoji_segment = await self.custom_emoji_to_segment(name)
                if emoji_segment:
                    segments.append(emoji_segment)
                elif name:
                    segments.append({"type": "text", "data": {"text": f"[表情:{name}]"}})
            pos = end
        after = text[pos:]
        if after:
            segments.append({"type": "text", "data": {"text": after}})
        return segments

    async def custom_emoji_to_segment(self, name: str) -> dict[str, Any] | None:
        """Resolve [QQ_EMOJI:name] to a NapCat custom-face image segment when configured."""
        if not name or name not in self._custom_emoji_names:
            return None
        faces = await self.fetch_custom_faces()
        index = self._custom_emoji_names.index(name)
        if index >= len(faces):
            LOG.warning("custom emoji index out of range: name=%s index=%s faces=%s", name, index, len(faces))
            return None
        src = self.custom_face_source(faces[index])
        if not src:
            LOG.warning("custom emoji has no sendable source: name=%s index=%s face=%r", name, index, faces[index])
            return None
        data_key = "file" if self._looks_like_file_source(src) else "url"
        return {"type": "image", "data": {data_key: src}}

    async def fetch_custom_faces(self) -> list[Any]:
        if self._custom_face_cache is not None:
            return self._custom_face_cache
        try:
            resp = await self.connection.call_action("fetch_custom_face", {})
        except Exception:
            LOG.warning("fetch_custom_face failed", exc_info=True)
            self._custom_face_cache = []
            return self._custom_face_cache
        data = resp.get("data")
        if isinstance(data, list):
            self._custom_face_cache = data
        elif isinstance(data, dict):
            faces = data.get("faces") or data.get("items") or data.get("list")
            self._custom_face_cache = faces if isinstance(faces, list) else []
        else:
            self._custom_face_cache = []
        LOG.info("fetched %s QQ custom faces", len(self._custom_face_cache))
        return self._custom_face_cache

    @staticmethod
    def custom_face_source(face: Any) -> str:
        if isinstance(face, str):
            return face.strip()
        if isinstance(face, dict):
            for key in ("url", "file", "image", "path"):
                value = str(face.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _looks_like_file_source(src: str) -> bool:
        lower = src.lower()
        return lower.startswith("file://") or lower.startswith("base64://") or bool(re.match(r"^[a-zA-Z]:[\\/]", src)) or lower.startswith("/")

    @staticmethod
    def extract_reactions(text: str) -> tuple[str, list[str]]:
        reactions: list[str] = []
        def repl(match: re.Match[str]) -> str:
            val = match.group(1).strip()
            if val:
                reactions.append(val)
            return ""
        return _REACT_RE.sub(repl, text).strip(), reactions

    @staticmethod
    def strip_output_markers(text: str) -> str:
        if not text:
            return ""
        text = _DC_EMOJI_RE.sub("", text)
        text = _CODE_BLOCK_RE.sub(lambda m: m.group(1), text)
        text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
        text = _LINK_RE.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", text)
        text = _BOLD_RE.sub(lambda m: m.group(1), text)
        text = _UNDERLINE_BOLD_RE.sub(lambda m: m.group(1), text)
        text = _ITALIC_RE.sub(lambda m: m.group(1), text)
        text = _UNDERLINE_ITALIC_RE.sub(lambda m: m.group(1), text)
        text = _HEADING_RE.sub("", text)
        return text.strip()

    async def set_reaction(self, message_id: Any, emoji_id: str, enabled: bool = True) -> None:
        if not message_id or not self.config.enable_reactions:
            return
        try:
            await self.connection.call_action("set_msg_emoji_like", {"message_id": int(message_id), "emoji_id": str(emoji_id), "set": bool(enabled)})
        except Exception:
            LOG.debug("set_msg_emoji_like failed message_id=%s emoji=%s", message_id, emoji_id, exc_info=True)

    async def _auto_like_user(self, user_id: int) -> None:
        """Send QQ profile like (名片点赞), once per user per day."""
        today = time.strftime("%Y-%m-%d")
        if self._auto_like_today.get(user_id) == today:
            return
        try:
            await self.connection.call_action("send_like", {"user_id": user_id, "times": self.config.auto_like_times})
            self._auto_like_today[user_id] = today
            LOG.debug("auto-liked user %s (%d times)", user_id, self.config.auto_like_times)
        except Exception:
            LOG.debug("send_like failed for user %s", user_id, exc_info=True)

    def resolve_attachment_path(self, att: dict[str, Any]) -> Path | None:
        raw_path = str(att.get("original_path") or att.get("path") or att.get("file") or "")
        if not raw_path:
            return None
        if raw_path.startswith("file://"):
            raw_path = raw_path[7:]
        if raw_path.startswith(("http://", "https://", "base64://")):
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.config.workspace_root / path
        return path.resolve()

    @staticmethod
    def path_to_file_uri(path: Path) -> str:
        return "file:///" + quote(str(path.resolve()).replace("\\", "/"), safe="/:~._-")

    @staticmethod
    def _parse_conversation_key(conversation_key: str) -> tuple[str, str, int] | None:
        prefixes = [
            ("qq_private:", "send_private_msg", "user_id"),
            ("qq:dm:", "send_private_msg", "user_id"),
            ("qq_group:", "send_group_msg", "group_id"),
            ("qq:", "send_group_msg", "group_id"),
        ]
        for prefix, action, id_key in prefixes:
            if conversation_key.startswith(prefix):
                raw = conversation_key[len(prefix):]
                try:
                    return action, id_key, int(raw)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _is_qq_conversation_key(conversation_key: str) -> bool:
        return ClonothOneBotAdapter._parse_conversation_key(conversation_key) is not None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _sanitize_name(name: str, max_len: int = 32) -> str:
        name = (name or "").replace("\n", " ").replace("\r", " ")
        name = name.replace("[", "(").replace("]", ")").strip()
        return (name[:max_len] + "…") if len(name) > max_len else (name or "未知成员")

    def _compact_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        return text[: self.config.history_text_limit] + "…" if len(text) > self.config.history_text_limit else text

    @staticmethod
    def _format_hhmm(timestamp: Any) -> str:
        try:
            return time.strftime("%H:%M", time.localtime(int(timestamp or time.time())))
        except Exception:
            return time.strftime("%H:%M")


def token_is_valid(websocket: WebSocket, expected: str) -> bool:
    if not expected:
        return True
    auth = websocket.headers.get("authorization", "")
    if auth == f"Bearer {expected}":
        return True
    if websocket.query_params.get("access_token") == expected:
        return True
    return False


def create_app(adapter: ClonothOneBotAdapter) -> FastAPI:
    app = FastAPI(title="Clonoth OneBot 11 Adapter", version="0.2.0")

    @app.on_event("startup")
    async def startup() -> None:
        app.state.poller = asyncio.create_task(adapter.poll_events_forever())
        app.state.qq_queue_workers = (
            [asyncio.create_task(adapter.qq_queue_worker_forever()) for _ in range(adapter.config.qq_queue_workers)]
            if adapter.config.enable_qq_queue
            else []
        )
        LOG.info("polling Clonoth events from %s", adapter.config.supervisor_url)
        if adapter.config.enable_qq_queue:
            LOG.info(
                "QQ queue enabled: workers=%s interval=%ss wait_for_reply=%s reply_timeout=%ss preempt=%s",
                adapter.config.qq_queue_workers,
                adapter.config.qq_queue_interval,
                adapter.config.qq_queue_wait_for_reply,
                adapter.config.qq_queue_reply_timeout,
                adapter.config.enable_preempt,
            )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        tasks = [getattr(app.state, "poller", None)]
        tasks.extend(getattr(app.state, "qq_queue_workers", []) or [])
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await adapter.close()

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "onebot_connected": adapter.connection.connected,
            "after_seq": adapter.after_seq,
            "sessions": len(adapter.session_to_conversation),
        })

    @app.websocket(adapter.config.ws_path)
    async def onebot_ws(websocket: WebSocket) -> None:
        if not token_is_valid(websocket, adapter.config.access_token):
            await websocket.close(code=1008)
            LOG.warning("rejected OneBot WebSocket: invalid token")
            return

        await websocket.accept()
        await adapter.connection.attach(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    LOG.warning("invalid JSON from OneBot: %s", raw[:300])
                    continue
                if not isinstance(data, dict):
                    continue
                if adapter.connection.handle_response(data):
                    continue
                try:
                    await adapter.handle_onebot_event(data)
                except Exception:
                    LOG.exception("failed to handle OneBot event: %s", data)
        except WebSocketDisconnect:
            pass
        finally:
            await adapter.connection.detach(websocket)

    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("ONEBOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = AdapterConfig.from_env()
    adapter = ClonothOneBotAdapter(config)
    app = create_app(adapter)
    LOG.info("starting OneBot adapter at ws://%s:%s%s -> %s", config.host, config.port, config.ws_path, config.supervisor_url)
    uvicorn.run(app, host=config.host, port=config.port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
