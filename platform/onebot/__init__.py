"""TangQiu 的 Clonoth Agent QQ 接入插件。

本插件采用纯对话模式：QQ群成员 @Bot 或用户私聊 Bot 后，插件把当前请求提交到
ClonothZX；ClonothZX 返回最终结果后，插件再把最终回复发回对应 QQ 会话。
Clonoth 主动发出的中间回复会展示给 QQ；工具调用、进度日志、审批请求和子任务状态
仍不展示给 QQ。
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Deque, Dict, List, Optional

import httpx
import yaml
from nonebot import get_bot, get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.rule import Rule, to_me

from .config import (
    ADMIN_QQ_USERS,
    ALLOWED_GROUPS,
    ALLOWED_PRIVATE_USERS,
    ALLOW_PRIVATE_FRIENDS,
    AUTO_LIKE_TIMES,
    BQBS_PATH,
    CLONOTH_BASE_URL,
    CLONOTH_WORKSPACE,
    CONVERSATION_HASH_SECRET,
    CUSTOM_FACE_METADATA_PATH,
    CUSTOM_FACE_NAMES_PATH,
    CUSTOM_FACE_PROMPT_LIMIT,
    ENABLE_AUTO_LIKE,
    ENABLE_FORWARD_MSG_INPUT,
    ENABLE_IMAGE_INPUT,
    ENABLE_PREEMPT,
    ENABLE_QQ_QUEUE,
    ENABLE_REACTIONS,
    ENTRY_NODE_ID,
    FORWARD_MSG_MAX_DEPTH,
    FORWARD_MSG_MAX_MESSAGES,
    FORWARD_MSG_TEXT_LIMIT,
    GROUP_HISTORY_MAX,
    GROUP_TRIGGER,
    HISTORY_TEXT_LIMIT,
    IMAGE_CACHE_TTL_SECONDS,
    IMAGE_DOWNLOAD_TIMEOUT,
    IMAGE_MAX_BYTES,
    IMAGE_PREFER_SAME_SENDER,
    IMAGE_WAIT_AFTER_TEXT_SECONDS,
    MAX_IMAGES_PER_TURN,
    QQ_MESSAGE_LIMIT,
    QQ_QUEUE_INTERVAL,
    QQ_QUEUE_REPLY_TIMEOUT,
    QQ_QUEUE_WAIT_FOR_REPLY,
    QQ_QUEUE_WORKERS,
    RECENT_IMAGE_MAX_AGE_SECONDS,
    RECENT_IMAGE_MAX_ITEMS,
    REPLY_TO_TRIGGER,
    TRIGGER_PREFIXES,
    USER_PROFILES_PATH,
    ONEBOT_STATE_FILE,
)
from .emoji_handler import (
    count_duplicate_face_names,
    extract_named_custom_face_metadata,
    extract_named_custom_face_names,
    fetch_custom_face_details,
    find_custom_faces_by_base_name,
    invalidate_custom_face_cache,
    list_custom_face_aliases,
    load_bqbs,
    load_custom_face_metadata,
    load_custom_face_names,
    process_emojis,
    resolve_custom_face,
    strip_output_markers,
    write_custom_face_metadata,
    write_custom_face_names,
)

# clonoth_sdk 安装在 ClonothZX 工作区内。插件加载时先加入 sys.path，
# 目的是让 TangQiu 进程不需要额外安装同名包也能导入 SDK。
if CLONOTH_WORKSPACE not in sys.path:
    sys.path.insert(0, CLONOTH_WORKSPACE)

from clonoth_sdk import (  # noqa: E402  # sys.path 必须先插入 ClonothZX 工作区。
    BotConfig,
    ChildTaskState,
    ClonothClient,
    EventRouter,
    MainTaskState,
    SessionState,
    TriggerInfo,
)

logger = logging.getLogger("nonebot.plugin.clonoth_agent")
driver = get_driver()

_CST = dt.timezone(dt.timedelta(hours=8))
_SPLIT_SIGNAL = "[SPLIT]"
_HISTORY_MAX_LEN = GROUP_HISTORY_MAX
_HISTORY_TEXT_LIMIT = HISTORY_TEXT_LIMIT
_QQ_MESSAGE_LIMIT = QQ_MESSAGE_LIMIT
_FORWARD_MSG_MAX_DEPTH = FORWARD_MSG_MAX_DEPTH
_FORWARD_MSG_MAX_MESSAGES = FORWARD_MSG_MAX_MESSAGES
_FORWARD_MSG_TEXT_LIMIT = FORWARD_MSG_TEXT_LIMIT
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_QQ_EMOJI_MARK_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")
_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")
_AT_QQ_RE = re.compile(r"\[(?:CQ:)?at[,:][^\]]*?qq=([^,\]\s]+)", re.IGNORECASE)
_CUSTOM_FACE_LIST_RE = re.compile(r"^(?:表情列表|收藏表情列表|emoji列表|可用表情)(?:\s+(\d+))?$", re.IGNORECASE)
_CUSTOM_FACE_DETAIL_LIST_RE = re.compile(r"^(?:表情详情列表|收藏表情详情|表情管理列表)(?:\s+(\d+))?$", re.IGNORECASE)
_CUSTOM_FACE_SYNC_RE = re.compile(r"^(?:同步表情列表|刷新表情列表|更新表情列表)$", re.IGNORECASE)
_CUSTOM_FACE_HELP_RE = re.compile(r"^(?:表情包帮助|表情帮助|表情包命令|表情命令帮助)$", re.IGNORECASE)
_CUSTOM_FACE_ADD_RE = re.compile(r"^(?:收藏表情|添加表情|保存表情)\s+(.+?)\s*$", re.IGNORECASE)
_CUSTOM_FACE_RENAME_RE = re.compile(r"^(?:命名表情|重命名表情|改名表情)\s+(\S+)\s+(.+?)\s*$", re.IGNORECASE)
_CUSTOM_FACE_DELETE_RE = re.compile(r"^(?:删除表情|移除表情|取消收藏表情)\s+(.+?)\s*$", re.IGNORECASE)
_SENSITIVE_ID_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
# 2026-05-03 修改原因：QQ 端需要把 Clonoth 任务生命周期映射为离散 React 阶段。
# 做法是集中维护阶段到 emoji_id 的映射和阶段顺序，目的在于后续回调只声明
# 目标阶段，避免 stream_delta 高频到达时重复调用 OneBot React API。
_REACT_STAGE_EMOJIS = {
    "received": "76",
    "submitted": "281",
    "thinking": "178",
    "tool": "97",
    "writing": "326",
}
_REACT_STAGE_ORDER = {
    "received": 1,
    "submitted": 2,
    "thinking": 3,
    "tool": 4,
    "writing": 5,
}
_REACT_CLEANUP_EMOJIS = tuple(_REACT_STAGE_EMOJIS.values())
_SEARCH_PROGRESS_FIRST_NOTICE = "已收到联网搜索请求，正在检索网页资料，可能需要几秒钟……"
_SEARCH_PROGRESS_STILL_RUNNING_NOTICE = "还在联网搜索中，我会拿到结果后马上整理回复。"
_SEARCH_PROGRESS_STILL_RUNNING_INTERVAL_SEC = 20.0
_SEARCH_PROGRESS_KEYWORDS = ("web_search", "exa_search", "x_search")

# 群聊上下文只保留最近 N 条。这样可以给入口节点提供社交语境，
# 同时避免每次 inbound 发送过长历史。
_group_history: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=_HISTORY_MAX_LEN))

# EventRouter 回调只拿到 session/trigger，因此这里保存发送最终回复所需的平台对象。
_session_targets: Dict[str, Dict[str, Any]] = {}
_conversation_bots: Dict[str, Bot] = {}
_real_conversation_keys: Dict[str, str] = {}
_stable_conversation_keys: Dict[str, str] = {}
_persisted_session_targets: Dict[str, Dict[str, Any]] = {}
_last_bot: Optional[Bot] = None


@dataclass
class RecentImageEntry:
    attachment: Dict[str, Any]
    created_at: float
    sender_id: str
    message_id: str


_recent_images: DefaultDict[str, Deque[RecentImageEntry]] = defaultdict(lambda: deque(maxlen=RECENT_IMAGE_MAX_ITEMS))
_last_attachment_cleanup_at = 0.0


@dataclass
class QueuedInbound:
    matcher: Any
    bot: Bot
    event: Event
    channel: str
    real_conversation_key: str
    stable_conversation_key: str
    text: str
    attachments: List[Dict[str, Any]]
    is_dm: bool
    platform_updates: Dict[str, Any]
    user_text: str


_qq_queue: Deque[QueuedInbound] = deque()
_qq_queue_by_key: Dict[str, QueuedInbound] = {}
_qq_queue_condition = asyncio.Condition()
_qq_waiting_replies: Dict[str, asyncio.Event] = {}
_auto_like_today: Dict[int, str] = {}
_reply_message_cache: Dict[str, Dict[str, Any]] = {}
_reply_message_cache_order: Deque[str] = deque()
_sent_reply_cache: Dict[str, float] = {}
_sent_reply_cache_order: Deque[str] = deque()
_route_state_lock = asyncio.Lock()
_anon_users: Dict[str, str] = {}
_anon_groups: Dict[str, str] = {}
_anon_user_reverse: Dict[str, str] = {}
_anon_group_reverse: Dict[str, str] = {}

# 待管理员审批的 Clonoth 操作。key 为 approval_id，value 保存操作、详情和来源会话。
_pending_approvals: Dict[str, Dict[str, Any]] = {}


# QQ 用户身份/称呼 Profile。只影响模型可见的称呼和身份说明，不授予任何权限。
# 权限仍只由 CLONOTH_ADMIN_QQ_USERS / _is_admin_user 判定。
_QQ_USER_PROFILES: Dict[str, Dict[str, Any]] = {}

_client: Optional[ClonothClient] = None
_session_state: Optional[SessionState] = None
_event_router: Optional[EventRouter] = None
_router_task: Optional[asyncio.Task] = None
_qq_queue_tasks: List[asyncio.Task] = []
_callbacks: Optional["TangQiuCallbacks"] = None
_bqbs: List[str] = []
_custom_face_names: List[str] = []
_custom_face_metadata: List[Dict[str, Any]] = []


def _is_group_allowed(group_id: int) -> bool:
    """判断群是否允许接入 Clonoth；空列表表示不允许任何群。"""
    return group_id in ALLOWED_GROUPS


def _is_admin_user(user_id: Any) -> bool:
    """判断 QQ 用户是否为 Clonoth 审批管理员。"""
    try:
        return int(user_id) in ADMIN_QQ_USERS
    except Exception:
        return False


def _is_private_allowed(event: PrivateMessageEvent) -> bool:
    """判断 QQ 私聊是否允许接入 Clonoth。"""
    user_id = int(event.user_id)
    if user_id in ADMIN_QQ_USERS or user_id in ALLOWED_PRIVATE_USERS:
        return True
    if not ALLOW_PRIVATE_FRIENDS:
        return False
    return str(getattr(event, "sub_type", "") or "").lower() == "friend"


async def _allowed_group_rule(event: Event) -> bool:
    """把群类型和白名单放在规则层过滤，避免无关消息进入上下文缓存。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    return _is_group_allowed(int(event.group_id))


async def _agent_group_rule(bot: Bot, event: Event) -> bool:
    """匹配允许群里的 Agent 触发消息，兼容 @Bot / 前缀 / 全量触发。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    if not _is_group_allowed(int(event.group_id)):
        return False
    text = _message_to_text(event.get_message(), getattr(bot, "self_id", None)).strip()
    return _group_should_trigger(event, bot, text)


async def _private_message_rule(event: Event) -> bool:
    """只匹配 QQ 私聊消息，避免私聊请求被群聊白名单逻辑误拦截。"""
    # 2026-05-01 修改原因：私聊没有群号，也不需要 @Bot；这里单独识别
    # PrivateMessageEvent，使私聊入口和现有群聊入口互不影响。
    return isinstance(event, PrivateMessageEvent)


def _approval_summary(approval_id: str, operation: str, details: Dict[str, Any]) -> str:
    """生成发给 QQ 管理员的审批摘要，限制长度并避免刷屏。"""
    lines = [
        "【Clonoth 审批请求】",
        f"ID: {approval_id}",
        f"操作: {operation or 'unknown'}",
    ]
    for key in ("tool_name", "path", "command", "reason", "safety_level"):
        value = details.get(key)
        if value is not None and value != "":
            lines.append(f"{key}: {str(value)[:1000]}")
    args = details.get("args") or details.get("parameters")
    if args:
        lines.append(f"参数: {str(args)[:1500]}")
    lines.extend([
        "",
        f"同意：审批 同意 {approval_id}",
        f"拒绝：审批 拒绝 {approval_id}",
    ])
    return _truncate_qq_text("\n".join(lines))


def _parse_approval_command(text: str) -> Optional[tuple[str, str]]:
    """解析管理员私聊审批命令，返回 (decision, approval_id_or_prefix)。"""
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized:
        return None
    parts = normalized.split(" ")
    if len(parts) < 2:
        return None
    verbs_allow = {"同意", "批准", "通过", "允许", "allow", "approve", "yes", "y"}
    verbs_deny = {"拒绝", "驳回", "deny", "reject", "no", "n"}
    if parts[0] in {"审批", "approval"} and len(parts) >= 3:
        verb = parts[1].lower()
        token = parts[2]
    else:
        verb = parts[0].lower()
        token = parts[1]
    if verb in verbs_allow:
        return "allow", token
    if verb in verbs_deny:
        return "deny", token
    return None


def _resolve_pending_approval_id(token: str) -> tuple[Optional[str], str]:
    """允许管理员用完整 approval_id 或唯一前缀审批。"""
    token = (token or "").strip()
    if not token:
        return None, "缺少审批 ID。"
    if token in _pending_approvals:
        return token, ""
    matches = [aid for aid in _pending_approvals if aid.startswith(token)]
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, f"没有找到待审批 ID：{token}"
    return None, f"审批 ID 前缀不唯一：{token}"


def _sanitize_name(name: str, max_len: int = 32) -> str:
    """清洗成员名称，避免换行和结构符号破坏输入格式。"""
    name = (name or "").replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")").strip()
    return (name[:max_len] + "…") if len(name) > max_len else (name or "未知成员")


def _load_qq_user_profiles(path_text: str) -> Dict[str, Dict[str, Any]]:
    """从 JSON/YAML 加载 QQ 用户称呼 Profile；仅用于展示和提示，不授予权限。"""
    path_text = str(path_text or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (Path(CLONOTH_WORKSPACE) / path).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("failed to load QQ user profiles: %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    users = data.get("users") if isinstance(data.get("users"), dict) else data
    profiles: Dict[str, Dict[str, Any]] = {}
    for raw_uid, raw_profile in users.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_profile, dict):
            continue
        profile = {
            "display_name": str(raw_profile.get("display_name") or raw_profile.get("name") or "").strip(),
            "address_as": str(raw_profile.get("address_as") or raw_profile.get("call_as") or "").strip(),
            "title": str(raw_profile.get("title") or raw_profile.get("role") or "").strip(),
            "note": str(raw_profile.get("note") or raw_profile.get("style_note") or "").strip(),
        }
        profiles[uid] = {k: v for k, v in profile.items() if v}
    return profiles


def _qq_user_profile(user_id: Any) -> Dict[str, Any]:
    raw = str(user_id or "").strip()
    return dict(_QQ_USER_PROFILES.get(raw) or {})


def _qq_profile_display_name(user_id: Any) -> str:
    name = str(_qq_user_profile(user_id).get("display_name") or "").strip()
    return _sanitize_name(name) if name else ""


def _sender_display_name(sender: Any, fallback_user_id: Any = "") -> str:
    """优先使用 QQ Profile 显示名，其次使用群名片/昵称，最后回退到 QQ 号。"""
    profile_name = _qq_profile_display_name(fallback_user_id)
    if profile_name:
        return profile_name
    card = getattr(sender, "card", "") or ""
    nickname = getattr(sender, "nickname", "") or ""
    return _sanitize_name(card or nickname or str(fallback_user_id or "未知成员"))


def _user_identity_lines(user_id: Any, display_name: str) -> List[str]:
    """生成模型可见的用户身份/称呼提示；权限仍由代码侧 platform_auth 控制。"""
    profile = _qq_user_profile(user_id)
    stable_user = _anonymize_user_id(user_id)
    lines = [
        f"稳定用户标识: {stable_user}",
        f"显示名: {_sanitize_name(display_name)}",
    ]
    address_as = str(profile.get("address_as") or "").strip()
    if address_as:
        lines.append(f"称呼要求: 请称呼该用户为「{_sanitize_name(address_as)}」")
    title = str(profile.get("title") or "").strip()
    if title:
        lines.append(f"身份标签: {_sanitize_name(title)}")
    note = str(profile.get("note") or "").strip()
    if note:
        lines.append(f"称呼/语气备注: {_sanitize_name(note, max_len=80)}")
    lines.append(f"Clonoth 管理员: {'是' if _is_admin_user(user_id) else '否'}")
    lines.append("权限说明: 管理员权限仅由系统配置判定，以上称呼配置不授予任何权限。")
    return lines


def _format_hhmm(timestamp: Optional[int]) -> str:
    """把 OneBot 秒级时间戳格式化为入口节点要求的 HH:MM。"""
    try:
        return dt.datetime.fromtimestamp(int(timestamp or time.time()), _CST).strftime("%H:%M")
    except Exception:
        return dt.datetime.now(_CST).strftime("%H:%M")


def _compact_text(text: str, limit: int = _HISTORY_TEXT_LIMIT) -> str:
    """把历史消息压缩到单行，避免每轮请求携带过长上下文。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


def _message_to_text(message: Message, bot_self_id: Any = None) -> str:
    """把 OneBot 消息段转换为模型可读文本，同时用占位符保留非文本内容。"""
    parts: List[str] = []
    for segment in message:
        seg_type = getattr(segment, "type", "")
        data = getattr(segment, "data", {}) or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = data.get("qq", "")
            if str(qq) == str(bot_self_id):
                continue
            profile_name = _qq_profile_display_name(qq)
            parts.append("@全体成员" if str(qq).lower() == "all" else f"@{profile_name or qq}")
        elif seg_type == "image":
            # 2026-05-03 修改原因：图片现在会被下载为 Clonoth 附件；这里仍
            # 保留文本占位符，做法是不改变原有消息转文本输出，目的是让模型
            # 在读取附件的同时也能从正文知道用户发送过图片。
            parts.append("[图片]")
        elif seg_type == "face":
            face_id = data.get("id", "")
            parts.append(f"[QQ表情:{face_id}]" if face_id else "[QQ表情]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "forward":
            parts.append("[合并转发]")
        elif seg_type == "reply":
            continue
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()



def _message_to_text_generic(message: Any, bot_self_id: Any = None) -> str:
    """把 NoneBot Message、OneBot segment list 或 CQ 字符串转换为模型可读文本。"""
    if message is None:
        return ""
    if isinstance(message, Message):
        return _message_to_text(message, bot_self_id)
    if isinstance(message, list):
        parts: List[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                qq = str(data.get("qq") or "")
                if bot_self_id is not None and qq == str(bot_self_id):
                    continue
                parts.append("@全体成员" if qq.lower() == "all" else f"@{qq}")
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "face":
                parts.append(f"[QQ表情:{data.get('id', '')}]" if data.get("id") else "[QQ表情]")
            elif seg_type == "record":
                parts.append("[语音]")
            elif seg_type == "video":
                parts.append("[视频]")
            elif seg_type == "forward":
                parts.append("[合并转发]")
            elif seg_type == "reply":
                continue
            elif seg_type:
                parts.append(f"[{seg_type}]")
        return "".join(parts).strip()
    if isinstance(message, str):
        return _format_cq_message(message).strip()
    try:
        return _message_to_text(Message(message), bot_self_id)
    except Exception:
        return str(message).strip()


def _parse_cq_params(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in (raw or "").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _extract_forward_ids(message: Any) -> List[str]:
    """从 OneBot forward segment / CQ forward 中提取合并转发 res_id。"""
    forward_ids: List[str] = []

    def pick_id(data: Dict[str, Any]) -> str:
        for key in ("id", "res_id", "resId", "forward_id", "forwardId"):
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for segment in segments:
        seg_type, data = _segment_type_and_data(segment)
        if seg_type != "forward":
            continue
        forward_id = pick_id(data)
        if forward_id:
            forward_ids.append(forward_id)

    if isinstance(message, str):
        for match in _CQ_RE.finditer(message):
            if match.group(1) != "forward":
                continue
            forward_id = pick_id(_parse_cq_params(match.group(2) or ""))
            if forward_id:
                forward_ids.append(forward_id)

    return forward_ids


async def _get_forward_messages(bot: Bot, forward_id: str) -> List[Dict[str, Any]] | None:
    """通过 NapCat get_forward_msg 读取合并转发消息列表。"""
    if not forward_id:
        return None
    try:
        data = await bot.call_api("get_forward_msg", id=forward_id)
    except Exception:
        logger.warning("get_forward_msg failed for forward_id=%s", forward_id, exc_info=True)
        return None
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if isinstance(messages, list):
        return [m for m in messages if isinstance(m, dict)]
    return None


def _format_forward_sender(sender: Any) -> tuple[str, str]:
    if isinstance(sender, dict):
        user_id = str(sender.get("user_id") or "")
        name = _sanitize_name(str(sender.get("card") or sender.get("nickname") or user_id or "未知用户"))
    else:
        user_id = str(getattr(sender, "user_id", "") or "")
        name = _sanitize_name(str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or user_id or "未知用户"))
    return name, (_anonymize_user_id(user_id) if user_id else "UserUnknown")


def _truncate_forward_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _FORWARD_MSG_TEXT_LIMIT:
        return text
    return text[:_FORWARD_MSG_TEXT_LIMIT] + "\n…（合并转发内容过长，已截断）"


async def _format_forward_msg(
    bot: Bot,
    forward_id: str,
    bot_self_id: Any = None,
    *,
    depth: int,
    visited: set[str],
    remaining: List[int],
) -> str:
    """递归读取并格式化单个合并转发 res_id。"""
    if not ENABLE_FORWARD_MSG_INPUT:
        return f"[合并转发:id={forward_id}]"
    if not forward_id:
        return "[合并转发:缺少id]"
    if depth <= 0:
        return f"[合并转发:id={forward_id}, 已达到读取深度上限]"
    if forward_id in visited:
        return f"[合并转发:id={forward_id}, 已跳过循环引用]"
    if remaining[0] <= 0:
        return "[合并转发:已达到读取条数上限]"

    visited.add(forward_id)
    messages = await _get_forward_messages(bot, forward_id)
    if messages is None:
        visited.discard(forward_id)
        return f"[合并转发:id={forward_id}, 读取失败]"

    lines: List[str] = [f"【合并转发 id={forward_id}】"]
    for index, item in enumerate(messages, start=1):
        if remaining[0] <= 0:
            lines.append("…（合并转发条数过多，已截断）")
            break
        remaining[0] -= 1
        sender_name, sender_user = _format_forward_sender(item.get("sender"))
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = await _message_to_text_with_forward(
            bot,
            content,
            bot_self_id,
            depth=depth - 1,
            visited=visited,
            remaining=remaining,
        )
        text = _compact_text(text or "[暂不支持的消息类型]", limit=max(_HISTORY_TEXT_LIMIT, 1200))
        lines.append(f"{index}. [{_format_hhmm(item.get('time'))}] {sender_name}({sender_user}): {text}")
    lines.append("【合并转发结束】")
    visited.discard(forward_id)
    return _truncate_forward_text("\n".join(lines))


async def _message_to_text_with_forward(
    bot: Bot,
    message: Any,
    bot_self_id: Any = None,
    *,
    depth: int | None = None,
    visited: set[str] | None = None,
    remaining: List[int] | None = None,
) -> str:
    """把消息转为文本，并递归展开 NapCat/OneBot 合并转发记录。"""
    text = _message_to_text_generic(message, bot_self_id)
    if not ENABLE_FORWARD_MSG_INPUT:
        return text
    forward_ids = _extract_forward_ids(message)
    if not forward_ids:
        return text

    depth_value = _FORWARD_MSG_MAX_DEPTH if depth is None else depth
    visited_set = visited if visited is not None else set()
    remaining_box = remaining if remaining is not None else [_FORWARD_MSG_MAX_MESSAGES]
    expanded: List[str] = []
    for forward_id in forward_ids:
        expanded.append(await _format_forward_msg(
            bot,
            forward_id,
            bot_self_id,
            depth=depth_value,
            visited=visited_set,
            remaining=remaining_box,
        ))
    if not text:
        return "\n".join(expanded).strip()
    return (text + "\n" + "\n".join(expanded)).strip()


def _format_cq_message(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        cq_type = match.group(1)
        params = _parse_cq_params(match.group(2) or "")
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
        if cq_type == "forward":
            return "[合并转发]"
        return match.group(0)
    return _CQ_RE.sub(repl, text)


def _extract_reply_message_id(message: Any, raw_message: Any = None) -> Any | None:
    """从 reply segment / CQ reply 中提取被引用消息 ID。"""
    def pick_id(data: Dict[str, Any]) -> Any | None:
        for key in ("id", "message_id", "messageId", "messageid", "message_seq", "seq"):
            value = data.get(key)
            if value is not None and str(value).strip():
                return value
        return None

    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for seg in segments:
        seg_type = getattr(seg, "type", "") if not isinstance(seg, dict) else str(seg.get("type") or "")
        if seg_type != "reply":
            continue
        data = (getattr(seg, "data", {}) or {}) if not isinstance(seg, dict) else (seg.get("data") if isinstance(seg.get("data"), dict) else {})
        rid = pick_id(data)
        if rid is not None:
            return rid

    for candidate in (message, raw_message):
        if not isinstance(candidate, str):
            continue
        for match in _CQ_RE.finditer(candidate):
            if match.group(1) != "reply":
                continue
            rid = _parse_cq_params(match.group(2) or "").get("id")
            if rid is not None and str(rid).strip():
                return rid
    return None


def _remember_message_for_reply_context(event: Event) -> None:
    """缓存最近消息，弥补 NapCat/NoneBot reply 字段不完整的情况。"""
    message_id = getattr(event, "message_id", None)
    if message_id is None or not str(message_id).strip():
        return
    sender = getattr(event, "sender", None)
    _reply_message_cache[str(message_id)] = {
        "message": event.get_message() if hasattr(event, "get_message") else None,
        "raw_message": getattr(event, "raw_message", None),
        "sender": sender,
        "user_id": getattr(event, "user_id", None),
        "time": getattr(event, "time", None),
    }
    if str(message_id) not in _reply_message_cache_order:
        _reply_message_cache_order.append(str(message_id))
    while len(_reply_message_cache_order) > 1000:
        old_id = _reply_message_cache_order.popleft()
        _reply_message_cache.pop(old_id, None)


async def _get_reply_message(bot: Bot, reply_message_id: Any) -> Dict[str, Any] | None:
    """通过 NapCat get_msg 兜底获取被引用消息。"""
    if reply_message_id is None:
        return None
    raw_id = str(reply_message_id).strip()
    message_id_param: Any = int(raw_id) if raw_id.isdigit() else reply_message_id
    try:
        data = await bot.call_api("get_msg", message_id=message_id_param)
    except Exception:
        logger.warning("get_msg failed for reply_message_id=%s", reply_message_id, exc_info=True)
        return None
    if isinstance(data, dict):
        return data
    return None



def _guess_image_mime(url: str, content_type: str = "", content: bytes = b"") -> str:
    """根据响应头、文件头魔数和 URL 推断图片 MIME。"""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith("image/"):
        return ct
    head = bytes(content[:16] or b"")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    url_lower = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    if url_lower.endswith(".png"):
        return "image/png"
    if url_lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if url_lower.endswith(".gif"):
        return "image/gif"
    if url_lower.endswith(".webp"):
        return "image/webp"
    if url_lower.endswith(".bmp"):
        return "image/bmp"
    return "image/jpeg"


def _image_ext_from_url_or_mime(url: str, mime_type: str) -> str:
    """为保存到本地的 QQ 图片选择稳定后缀。"""
    # 2026-05-03 修改原因：附件文件名需要随机生成，但后缀会影响后续图片
    # 识别。这里优先复用 URL 路径中的常见图片后缀，其次用推断出的 MIME
    # 映射后缀，目的是避免没有扩展名的 QQ 临时 URL 生成不可识别文件。
    clean_url_path = (url or "").split("?", 1)[0].split("#", 1)[0]
    ext = Path(clean_url_path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ext
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/gif":
        return ".gif"
    if mime_type == "image/webp":
        return ".webp"
    if mime_type == "image/bmp":
        return ".bmp"
    return ".jpg"


def _attachment_error_text(error: str) -> str:
    if error == "too_large":
        return f"图片太大，已超过当前限制 {IMAGE_MAX_BYTES // 1024 // 1024}MB。"
    if error == "download_failed":
        return "我收到了图片，但下载失败了，可能是 QQ 临时链接已过期。"
    if error == "unsupported_mime":
        return "我收到了图片，但图片格式暂不支持。"
    return "我收到了图片，但处理图片时失败了。"


def _cleanup_old_qq_attachments(now: float | None = None) -> None:
    """清理 OneBot 附件目录中过期图片；默认保留 1 天。"""
    global _last_attachment_cleanup_at
    if IMAGE_CACHE_TTL_SECONDS <= 0:
        return
    now = time.time() if now is None else now
    if now - _last_attachment_cleanup_at < 3600:
        return
    _last_attachment_cleanup_at = now
    root = Path(CLONOTH_WORKSPACE) / "data" / "attachments"
    if not root.exists():
        return
    cutoff = now - IMAGE_CACHE_TTL_SECONDS
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                continue
        dirs = [x for x in root.rglob("*") if x.is_dir()]
        for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("QQ attachment cleanup skipped: %s", exc)


def _remember_recent_images(conversation_key: str, event: Event, attachments: List[Dict[str, Any]]) -> None:
    if not attachments:
        return
    sender_id = str(getattr(event, "user_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    now = time.time()
    q = _recent_images[conversation_key]
    for att in attachments:
        if str(att.get("type") or "") == "image" and att.get("path"):
            q.append(RecentImageEntry(dict(att), now, sender_id, message_id))


def _recent_images_for_text(conversation_key: str, event: Event) -> List[Dict[str, Any]]:
    now = time.time()
    sender_id = str(getattr(event, "user_id", "") or "")
    entries = [
        item for item in _recent_images.get(conversation_key, ())
        if now - item.created_at <= RECENT_IMAGE_MAX_AGE_SECONDS
    ]
    if IMAGE_PREFER_SAME_SENDER:
        same_sender = [item for item in entries if item.sender_id == sender_id]
        if same_sender:
            entries = same_sender
    return [dict(item.attachment) for item in entries[-MAX_IMAGES_PER_TURN:]]


def _text_looks_like_image_query(text: str) -> bool:
    value = (text or "").strip().lower()
    if not value:
        return False
    keywords = (
        "图", "图片", "截图", "照片", "看下", "看看", "看一下", "识别", "读一下",
        "ocr", "文字", "这是什么", "什么意思", "表情包", "image", "photo", "screenshot",
    )
    return any(k in value for k in keywords)


async def _merge_recent_images_after_text(
    *,
    event: Event,
    conversation_key: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
) -> None:
    if attachments or not ENABLE_IMAGE_INPUT or not _text_looks_like_image_query(user_text):
        return
    if IMAGE_WAIT_AFTER_TEXT_SECONDS > 0:
        await asyncio.sleep(IMAGE_WAIT_AFTER_TEXT_SECONDS)
    recent = _recent_images_for_text(conversation_key, event)
    if recent:
        attachments.extend(recent)


def _iter_qq_image_urls(message: Any) -> List[str]:
    """从 OneBot Message、segment list 或 CQ 字符串中提取 image 下载地址。"""
    urls: List[str] = []
    if message is None:
        return urls
    if isinstance(message, str):
        for match in _CQ_RE.finditer(message):
            if match.group(1) != "image":
                continue
            params = _parse_cq_params(match.group(2) or "")
            src = str(params.get("url") or params.get("path") or params.get("file") or "").strip()
            if src:
                urls.append(src)
        return urls
    try:
        segments = list(message)
    except Exception:
        return urls
    for segment in segments:
        if isinstance(segment, dict):
            seg_type = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        else:
            seg_type = getattr(segment, "type", "")
            data = getattr(segment, "data", {}) or {}
        if seg_type != "image":
            continue
        url = str(data.get("url") or data.get("path") or data.get("file") or "").strip()
        if url:
            urls.append(url)
    return urls


async def _image_sources_to_attachments(image_urls: List[str], conversation_key: str) -> tuple[list[dict], list[str]]:
    """下载 OneBot 图片 URL/path 列表，并返回 Clonoth 附件描述与用户可读错误。"""
    result: list[dict] = []
    errors: list[str] = []
    if not ENABLE_IMAGE_INPUT or not image_urls:
        return result, errors

    _cleanup_old_qq_attachments()
    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("collect QQ attachments cannot create directory %s: %s", att_dir, exc)
        return result, [_attachment_error_text("download_failed")]

    async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for url in image_urls[:MAX_IMAGES_PER_TURN]:
            try:
                source = str(url or "").strip()
                if not source:
                    continue
                if source.startswith("file://"):
                    local = Path(source[7:])
                    content = local.read_bytes()
                    content_type = ""
                elif re.match(r"^[a-zA-Z]:[\\/]", source) or source.startswith("/"):
                    local = Path(source)
                    content = local.read_bytes()
                    content_type = ""
                else:
                    response = await client.get(source)
                    response.raise_for_status()
                    content = response.content
                    content_type = response.headers.get("content-type", "")
                if not content:
                    logger.warning("collect QQ image attachment skipped empty response: %s", source)
                    errors.append(_attachment_error_text("download_failed"))
                    continue
                if len(content) > IMAGE_MAX_BYTES:
                    logger.warning("collect QQ image attachment too large: %s bytes url=%s", len(content), source)
                    errors.append(_attachment_error_text("too_large"))
                    continue
                mime_type = _guess_image_mime(source, content_type, content)
                if not mime_type.startswith("image/"):
                    errors.append(_attachment_error_text("unsupported_mime"))
                    continue
                ext = _image_ext_from_url_or_mime(source, mime_type)
                filename = f"{os.urandom(16).hex()}{ext}"
                file_path = att_dir / filename
                file_path.write_bytes(content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "image",
                    "path": rel_path,
                    "mime_type": mime_type,
                    "name": f"image{ext}",
                    "source": "onebot",
                })
            except Exception as exc:
                logger.warning("collect QQ image attachment failed: url=%s error=%s", url, exc)
                errors.append(_attachment_error_text("download_failed"))
    return result, errors


async def _collect_qq_attachments(event, conversation_key: str) -> tuple[list[dict], list[str]]:
    """下载 QQ 当前消息中的图片，并返回 Clonoth 附件列表。引用消息由增强 reply 逻辑单独处理。"""
    try:
        image_urls = _iter_qq_image_urls(event.get_message())
    except Exception as exc:
        logger.warning("collect QQ attachments skipped current message: %s", exc)
        image_urls = []
    return await _image_sources_to_attachments(image_urls, conversation_key)


def _attachment_abs_path(attachment: Dict[str, Any]) -> Path | None:
    """把 Clonoth 附件描述解析成本地绝对路径，用于 NapCat add_custom_face。"""
    raw = str(attachment.get("path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / raw
    return path


async def _collect_reply_image_attachments(bot: Bot, event: Event, conversation_key: str) -> tuple[list[dict], list[str]]:
    """为“收藏表情”命令补充引用消息中的图片。"""
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    if reply_message_id is None:
        return [], []
    reply_obj = await _get_reply_message(bot, reply_message_id)
    if not reply_obj:
        return [], []
    image_urls: list[str] = []
    for key in ("message", "raw_message"):
        value = reply_obj.get(key)
        if value:
            image_urls.extend(_iter_qq_image_urls(value))
    # 去重，避免同一引用图被重复下载。
    image_urls = list(dict.fromkeys(image_urls))
    return await _image_sources_to_attachments(image_urls, conversation_key)


async def _custom_face_command_attachments(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> tuple[list[dict], list[str]]:
    """为收藏表情命令选择图片：当前消息 > 引用消息 > 最近图片。"""
    if current_attachments:
        return current_attachments, []

    reply_attachments, reply_errors = await _collect_reply_image_attachments(bot, event, conversation_key)
    if reply_attachments:
        return reply_attachments, reply_errors

    recent = _recent_images_for_text(conversation_key, event)
    if recent:
        return recent, []
    return [], reply_errors


def _load_custom_face_names_file() -> List[str]:
    """读取 AI 可见收藏表情名称文件。"""
    return load_custom_face_names(CUSTOM_FACE_NAMES_PATH)


def _load_custom_face_metadata_file() -> List[Dict[str, Any]]:
    """读取程序内部使用的收藏表情元数据文件（md5/resId/emojiId/url 等）。"""
    return load_custom_face_metadata(CUSTOM_FACE_METADATA_PATH)


def _current_custom_face_names() -> List[str]:
    """读取当前 AI 可见表情名，并同步进内存缓存。手动编辑文件后无需重启。"""
    global _custom_face_names
    _custom_face_names = _load_custom_face_names_file()
    return list(_custom_face_names)


def _current_custom_face_metadata() -> List[Dict[str, Any]]:
    """读取当前收藏表情内部元数据，并同步进内存缓存。"""
    global _custom_face_metadata
    _custom_face_metadata = _load_custom_face_metadata_file()
    return list(_custom_face_metadata)


def _custom_face_prompt_block() -> str:
    """构造注入给 AI 的 QQ 收藏表情使用说明。"""
    current_names = _current_custom_face_names()
    if CUSTOM_FACE_PROMPT_LIMIT <= 0 or not current_names:
        return ""
    names = current_names[:CUSTOM_FACE_PROMPT_LIMIT]
    more = "" if len(current_names) <= CUSTOM_FACE_PROMPT_LIMIT else f"（另有 {len(current_names) - CUSTOM_FACE_PROMPT_LIMIT} 个未展示）"
    return (
        "【QQ可用收藏表情】\n"
        "你可以在回复中用 [表情:名称] 发送 QQ 收藏表情。"
        "只使用下列名称，不要臆造未列出的表情名。\n"
        f"可用名称：{'、'.join(names)}{more}"
    )


async def _sync_custom_face_names_file(bot: Bot) -> List[str]:
    """从 NapCat 收藏表情详情同步已命名表情到 AI 名称文件和内部元数据文件。

    AI 名称文件只写去重后的基础名；内部元数据保留全部同名项（带 (1)/(2) 后缀）。
    """
    global _custom_face_names, _custom_face_metadata
    faces = await fetch_custom_face_details(bot, force=True)
    metadata = extract_named_custom_face_metadata(faces, _bqbs)
    names = extract_named_custom_face_names(faces, _bqbs)
    write_custom_face_names(CUSTOM_FACE_NAMES_PATH, names)
    write_custom_face_metadata(CUSTOM_FACE_METADATA_PATH, metadata)
    _custom_face_names = names
    _custom_face_metadata = metadata
    return names


def _custom_face_value(face: Dict[str, Any], *keys: str) -> Any:
    """按多个可能字段名取收藏表情字段；保留 0 这类合法值。"""
    if not isinstance(face, dict):
        return None
    for key in keys:
        if key not in face:
            continue
        value = face.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


async def _set_custom_face_desc(bot: Bot, face: Dict[str, Any], desc: str) -> tuple[bool, str]:
    """调用 NapCat set_custom_face_desc 给已有收藏表情设置描述。"""
    emoji_id = _custom_face_value(face, "emojiId", "emoji_id", "emoId", "emoid")
    res_id = _custom_face_value(face, "resId", "res_id", "id")
    md5 = _custom_face_value(face, "md5", "MD5")
    if emoji_id is None or not res_id or not md5:
        missing = []
        if emoji_id is None:
            missing.append("emoji_id")
        if not res_id:
            missing.append("res_id")
        if not md5:
            missing.append("md5")
        return False, "找到了该表情，但缺少重命名所需字段：" + "、".join(missing)
    try:
        await bot.call_api(
            "set_custom_face_desc",
            emoji_id=emoji_id,
            res_id=str(res_id),
            md5=str(md5),
            desc=desc,
        )
    except Exception as exc:
        logger.warning("set_custom_face_desc failed: %s", exc, exc_info=True)
        return False, "重命名收藏表情失败：当前 OneBot 实现可能不支持 set_custom_face_desc，或该表情资源信息已失效。"
    invalidate_custom_face_cache(bot)
    try:
        await _sync_custom_face_names_file(bot)
    except Exception:
        logger.warning("sync custom face names after rename failed", exc_info=True)
    return True, f"已将收藏表情重命名为：{desc}\n之后模型可用 [表情:{desc}] 调用。"


async def _add_custom_face_from_attachment(bot: Bot, alias: str, attachment: Dict[str, Any]) -> str:
    """调用 NapCat add_custom_face，并尽量把收藏描述设置为 alias。"""
    path = _attachment_abs_path(attachment)
    if path is None or not path.exists():
        return "找不到要收藏的图片文件，请重新发送图片后再试。"

    content = path.read_bytes()
    md5 = hashlib.md5(content).hexdigest()
    try:
        await bot.call_api(
            "add_custom_face",
            file=str(path),
            md5=md5,
            file_name=path.name,
            is_origin=True,
        )
    except Exception as exc:
        logger.warning("add_custom_face failed: %s", exc, exc_info=True)
        return (
            "收藏表情失败：NapCat add_custom_face 调用失败。\n"
            "提示：NapCat 要求 file 是 NapCat 运行环境可访问的本地路径；"
            "如果 NoneBot 和 NapCat 分容器部署，请把 Clonoth data/attachments 挂载到相同路径。"
        )

    invalidate_custom_face_cache(bot)

    # NapCat 修改描述需要 emoji_id + res_id + md5。add_custom_face 的返回不一定包含这些字段，
    # 因此刷新列表后用 md5 反查新表情，再调用 set_custom_face_desc。
    try:
        await asyncio.sleep(0.8)
        faces = await fetch_custom_face_details(bot, force=True)
        target_face = None
        for face in faces:
            if isinstance(face, dict) and str(face.get("md5") or "").lower() == md5.lower():
                target_face = face
                break
        if isinstance(target_face, dict):
            emoji_id = _custom_face_value(target_face, "emojiId", "emoji_id", "emoId", "emoid")
            res_id = _custom_face_value(target_face, "resId", "res_id")
            if emoji_id is not None and res_id:
                await bot.call_api(
                    "set_custom_face_desc",
                    emoji_id=emoji_id,
                    res_id=str(res_id),
                    md5=md5,
                    desc=alias,
                )
                invalidate_custom_face_cache(bot)
                try:
                    await _sync_custom_face_names_file(bot)
                except Exception:
                    logger.warning("sync custom face names after add failed", exc_info=True)
    except Exception:
        # 描述设置失败不影响“已收藏”的主体结果，模型仍可用序号/md5/文件名兜底调用。
        logger.warning("set_custom_face_desc after add_custom_face failed", exc_info=True)

    return f"已尝试收藏表情：{alias}\n之后模型可用 [表情:{alias}] 调用；如描述设置失败，也可用“表情列表”查看实际名称。"


async def _maybe_handle_custom_face_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理 QQ 侧收藏表情管理命令；返回回复文本，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None

    is_admin = _is_admin_user(getattr(event, "user_id", ""))

    if _CUSTOM_FACE_HELP_RE.match(text):
        if not is_admin:
            return "表情包命令仅限 Clonoth 管理员使用。"
        return (
            "【表情包管理命令示例（仅管理员）】\n"
            "1) 同步表情列表\n"
            "   从 NapCat 收藏同步已命名表情到本地文件。\n"
            "2) 表情列表 / 表情列表 50\n"
            "   查看 AI 当前可用的表情名称。\n"
            "3) 表情详情列表 / 表情详情列表 50\n"
            "   查看收藏详情（含未命名项）与序号。\n"
            "4) 收藏表情 开心\n"
            "   收藏当前消息/引用/最近的一张图片，并命名为“开心”。\n"
            "5) 命名表情 3 开心 或 重命名表情 3 开心\n"
            "   给第 3 个收藏表情命名/改名（也可用 md5/resId/文件名定位）。\n"
            "6) 删除表情 开心\n"
            "   删除名为“开心”的收藏表情。\n"
            "提示：AI 发送表情用 [表情:名称]；未命名表情不会给 AI 使用。"
        )

    # 以下写操作命令仅限管理员：同步 / 收藏 / 命名 / 删除。
    if _CUSTOM_FACE_SYNC_RE.match(text):
        if not is_admin:
            return "同步表情列表仅限 Clonoth 管理员使用。"
        try:
            names = await _sync_custom_face_names_file(bot)
        except Exception as exc:
            logger.warning("sync custom face names failed: %s", exc, exc_info=True)
            return "同步表情列表失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
        if not names:
            return f"已同步，但没有发现已命名收藏表情。未命名表情不会写入 AI 表情列表文件：{CUSTOM_FACE_NAMES_PATH}"
        duplicates = count_duplicate_face_names(_custom_face_metadata)
        total = len(_custom_face_metadata)
        dup_note = ""
        if duplicates:
            dup_desc = "、".join(f"{name}(x{count})" for name, count in list(duplicates.items())[:20])
            dup_note = (
                f"\n⚠ 检测到 {len(duplicates)} 组同名表情：{dup_desc}\n"
                "同名表情已全部保留，AI 名称列表仅显示一个基础名；发送时会在同名表情中随机选择一个。"
            )
        return (
            f"已同步 {total} 个已命名收藏表情（AI 可见基础名 {len(names)} 个）。\n"
            f"AI 名称文件：{CUSTOM_FACE_NAMES_PATH}\n"
            f"内部元数据文件：{CUSTOM_FACE_METADATA_PATH}\n"
            "AI 只看到名称文件里的基础名，md5/resId/emojiId 保存在元数据文件里。"
            + dup_note
        )

    list_match = _CUSTOM_FACE_LIST_RE.match(text)
    if list_match:
        if not is_admin:
            return "表情列表仅限 Clonoth 管理员使用。"
        limit = int(list_match.group(1) or "30")
        limit = max(1, min(100, limit))
        names = _load_custom_face_names_file()
        if not names:
            return f"AI 表情列表文件为空：{CUSTOM_FACE_NAMES_PATH}\n可发送“同步表情列表”从 NapCat 写入已命名表情；未命名表情不会写入。"
        shown = names[:limit]
        lines = [f"{index}. {name}" for index, name in enumerate(shown, start=1)]
        more = "" if len(names) <= limit else f"\n……还有 {len(names) - limit} 个，可发送“表情列表 {min(len(names), 100)}”查看更多。"
        return "AI 可用收藏表情：\n" + "\n".join(lines) + more + "\n模型可用格式：[表情:名称]"

    detail_match = _CUSTOM_FACE_DETAIL_LIST_RE.match(text)
    if detail_match:
        if not is_admin:
            return "表情详情列表仅限 Clonoth 管理员使用。"
        limit = int(detail_match.group(1) or "50")
        limit = max(1, min(100, limit))
        try:
            names = await list_custom_face_aliases(bot, _bqbs, count=max(limit, 48))
        except Exception as exc:
            logger.warning("list custom face details failed: %s", exc, exc_info=True)
            return "获取收藏表情详情失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
        if not names:
            return "当前没有可识别的收藏表情。"
        shown = names[:limit]
        lines = [f"{index}. {name}" for index, name in enumerate(shown, start=1)]
        more = "" if len(names) <= limit else f"\n……还有 {len(names) - limit} 个，可发送“表情详情列表 {min(len(names), 100)}”查看更多。"
        return "收藏表情详情（含未命名项）：\n" + "\n".join(lines) + more + "\n未命名表情可用：命名表情 <序号> <新名字>"

    rename_match = _CUSTOM_FACE_RENAME_RE.match(text)
    if rename_match:
        if not is_admin:
            return "命名/重命名表情仅限 Clonoth 管理员使用。"
        target = rename_match.group(1).strip()
        desc = rename_match.group(2).strip()
        if not target or not desc:
            return "请指定要命名的表情和新名字，例如：命名表情 3 开心"
        # 若定位词是纯基础名且存在多个同名，不直接改，列序号让管理员指定具体一个。
        try:
            siblings = await find_custom_faces_by_base_name(bot, target, _bqbs)
        except Exception as exc:
            logger.warning("find custom faces (rename) failed: %s", exc, exc_info=True)
            return "命名表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if len(siblings) > 1:
            lines = []
            for s in siblings:
                extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
                extra_note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
                lines.append(f"{s['index']}. {s['base_name']}{extra_note}")
            return (
                f"检测到 {len(siblings)} 个同名表情“{target}”，为避免改错，请指定要命名哪一个：\n"
                + "\n".join(lines)
                + f"\n请改用序号，例如：命名表情 {siblings[0]['index']} {desc}"
                + "\n也可用 md5 或 resId 精确定位。"
            )
        try:
            face = await resolve_custom_face(bot, target, _bqbs)
        except Exception as exc:
            logger.warning("resolve custom face (rename) failed: %s", exc, exc_info=True)
            return "命名表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if face is not None and not isinstance(face, dict):
            return (
                f"找到了收藏表情：{target}，但当前 OneBot/NapCat 只返回图片 URL，没有 emojiId/resId/md5，无法命名。\n"
                "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 set_custom_face_desc。"
            )
        if not isinstance(face, dict):
            return f"没有找到收藏表情：{target}\n可先发送“表情详情列表 50”查看序号，再用“命名表情 <序号> <新名字>”。"
        ok, message = await _set_custom_face_desc(bot, face, desc)
        return message

    delete_match = _CUSTOM_FACE_DELETE_RE.match(text)
    if delete_match:
        if not is_admin:
            return "删除表情仅限 Clonoth 管理员使用。"
        name = delete_match.group(1).strip()
        if not name:
            return "请指定要删除的表情名称，例如：删除表情 开心"
        # 若输入是纯基础名（不是序号/md5/resId 这类唯一定位），且存在多个同名，
        # 则不直接删除，改为列出同名项的序号，让管理员明确指定删哪个。
        try:
            siblings = await find_custom_faces_by_base_name(bot, name, _bqbs)
        except Exception as exc:
            logger.warning("find custom faces (delete) failed: %s", exc, exc_info=True)
            return "删除表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if len(siblings) > 1:
            lines = []
            for s in siblings:
                extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
                extra_note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
                lines.append(f"{s['index']}. {s['base_name']}{extra_note}")
            return (
                f"检测到 {len(siblings)} 个同名表情“{name}”，为避免误删，请指定要删除哪一个：\n"
                + "\n".join(lines)
                + "\n请改用序号删除，例如：删除表情 " + str(siblings[0]["index"])
                + "\n也可用 md5 或 resId 精确删除。"
            )
        try:
            face = await resolve_custom_face(bot, name, _bqbs)
        except Exception as exc:
            logger.warning("resolve custom face (delete) failed: %s", exc, exc_info=True)
            return "删除表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if face is not None and not isinstance(face, dict):
            return (
                f"找到了收藏表情：{name}，但当前 OneBot/NapCat 只返回图片 URL，没有 resId，无法删除。\n"
                "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 delete_custom_face。"
            )
        if not isinstance(face, dict):
            return f"没有找到收藏表情：{name}"
        res_id = face.get("resId") or face.get("res_id") or face.get("id")
        if not res_id:
            return f"找到了表情 {name}，但没有 resId，无法删除。"
        try:
            await bot.call_api("delete_custom_face", res_id=str(res_id))
            invalidate_custom_face_cache(bot)
            try:
                await _sync_custom_face_names_file(bot)
            except Exception:
                logger.warning("sync custom face names after delete failed", exc_info=True)
            return f"已删除收藏表情：{name}"
        except Exception as exc:
            logger.warning("delete_custom_face failed: %s", exc, exc_info=True)
            return "删除收藏表情失败：当前 OneBot 实现可能不支持 delete_custom_face，或 resId 已失效。"

    add_match = _CUSTOM_FACE_ADD_RE.match(text)
    if add_match:
        if not is_admin:
            return "收藏表情仅限 Clonoth 管理员使用。"
        alias = add_match.group(1).strip()
        if not alias:
            return "请给表情起一个名字，例如：收藏表情 开心"
        attachments, errors = await _custom_face_command_attachments(
            bot=bot,
            event=event,
            conversation_key=conversation_key,
            current_attachments=current_attachments,
        )
        if not attachments:
            suffix = "\n" + "\n".join(dict.fromkeys(errors)) if errors else ""
            return "没有找到可收藏的图片。请在同一条消息里带图，或引用/紧接着回复一张图片：收藏表情 名称" + suffix
        return await _add_custom_face_from_attachment(bot, alias, attachments[0])

    return None


def _format_history_line(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> str:
    """把群消息格式化为 tangqiu_main 提示词要求的历史行，并匿名化 QQ ID。"""
    text = _anonymize_text_for_ai(_compact_text(override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))))
    name = _anonymize_text_for_ai(_sender_display_name(event.sender, event.user_id))
    user = _anonymize_user_id(event.user_id)
    return f"[{_format_hhmm(getattr(event, 'time', None))}] {name}({user}): {text}"


def _record_group_message(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> None:
    """记录群最近消息；@Bot 触发消息由 Agent matcher 手动记录，避免被 block 跳过。"""
    text = override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))
    if text.strip():
        _group_history[int(event.group_id)].append(_format_history_line(event, bot, override_text=text))


def _record_bot_reply(group_id: int, text: str) -> None:
    """把 Bot 最终回复写回群历史，保持后续对话连续性。"""
    if not text:
        return
    text = strip_output_markers(text).replace(_SPLIT_SIGNAL, " ")
    text = _QQ_EMOJI_MARK_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
    text = _anonymize_text_for_ai(_compact_text(text))
    if text:
        _group_history[int(group_id)].append(f"[{dt.datetime.now(_CST).strftime('%H:%M')}] Bot: {text}")


async def _build_reply_context(event: Event, bot: Bot, conversation_key: str) -> tuple[str, List[Dict[str, Any]]]:
    """增强引用消息解析：event.reply → 本地缓存 → NapCat get_msg，并采集引用图片附件。"""
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply_obj: Any = getattr(event, "reply", None)
    reply: Dict[str, Any] | None = None

    if reply_obj is not None:
        reply = {
            "message": getattr(reply_obj, "message", None),
            "raw_message": getattr(reply_obj, "raw_message", None),
            "sender": getattr(reply_obj, "sender", None),
            "user_id": getattr(reply_obj, "user_id", None),
            "time": getattr(reply_obj, "time", None),
        }
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        reply = _reply_message_cache.get(str(reply_message_id))
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        reply = await _get_reply_message(bot, reply_message_id)

    if not reply:
        if reply_message_id is not None:
            return _anonymize_text_for_ai(f"（无法获取引用消息内容：message_id={reply_message_id}）"), []
        return "", []

    message = reply.get("message")
    raw_message = reply.get("raw_message")
    text = await _message_to_text_with_forward(bot, message, getattr(bot, "self_id", None))
    if not text:
        text = await _message_to_text_with_forward(bot, raw_message, getattr(bot, "self_id", None))
    images = _iter_qq_image_urls(message) or _iter_qq_image_urls(raw_message)
    quoted_attachments, _quoted_errors = await _image_sources_to_attachments(images, conversation_key)
    for att in quoted_attachments:
        text = text.replace("[图片]", f"[图片: {att['path']}]", 1)

    text = _anonymize_text_for_ai(_compact_text(text))
    if not text:
        if reply_message_id is not None:
            return _anonymize_text_for_ai(f"（引用消息为空或暂不支持的消息类型：message_id={reply_message_id}）"), quoted_attachments
        return "", quoted_attachments

    sender = reply.get("sender")
    sender_id = ""
    if isinstance(sender, dict):
        sender_id = str(sender.get("user_id") or reply.get("user_id") or "")
        name_raw = _sanitize_name(str(sender.get("card") or sender.get("nickname") or sender_id or "原作者"))
    elif sender is not None:
        sender_id = str(getattr(sender, "user_id", "") or "")
        name_raw = _sender_display_name(sender, sender_id)
    else:
        sender_id = str(reply.get("user_id") or "")
        name_raw = _sanitize_name(sender_id or "原作者")
    name = _anonymize_text_for_ai(name_raw)
    user = _anonymize_user_id(sender_id) if sender_id else "UserUnknown"
    return f"[{_format_hhmm(reply.get('time'))}] {name}({user}): {text}", quoted_attachments


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


def _anonymize_user_id(user_id: Any) -> str:
    real = str(user_id or "").strip()
    if not real:
        return "UserUnknown"
    alias = _anon_users.get(real)
    if alias is None:
        alias = _alias_from_index("User", len(_anon_users))
        _anon_users[real] = alias
        _anon_user_reverse[alias] = real
    return alias


def _anonymize_group_id(group_id: Any) -> str:
    real = str(group_id or "").strip()
    if not real:
        return "GroupUnknown"
    alias = _anon_groups.get(real)
    if alias is None:
        alias = _alias_from_index("Group", len(_anon_groups))
        _anon_groups[real] = alias
        _anon_group_reverse[alias] = real
    return alias


def _anonymize_text_for_ai(text: str) -> str:
    if not text:
        return ""
    safe = str(text)
    for real, alias in sorted(_anon_groups.items(), key=lambda item: len(item[0]), reverse=True):
        safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
    for real, alias in sorted(_anon_users.items(), key=lambda item: len(item[0]), reverse=True):
        safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
    return _SENSITIVE_ID_RE.sub(lambda m: _anonymize_user_id(m.group(0)), safe)


def _conversation_digest(conversation_key: str) -> str:
    raw = str(conversation_key or "").strip().encode("utf-8")
    secret = CONVERSATION_HASH_SECRET.encode("utf-8")
    if secret:
        return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:24]
    return hashlib.sha256(raw).hexdigest()[:24]


def _stable_conversation_key(real_conversation_key: str) -> str:
    """把真实 QQ 会话键转换为可持久化的稳定哈希键，避免 Supervisor 泄漏群号/QQ号。"""
    if real_conversation_key.startswith("qq_group:"):
        prefix = "qq_group"
        _anonymize_group_id(real_conversation_key.split(":", 1)[1])
    elif real_conversation_key.startswith("qq_private:"):
        prefix = "qq_private"
        _anonymize_user_id(real_conversation_key.split(":", 1)[1])
    else:
        prefix = "qq_unknown"
    stable = f"{prefix}:{_conversation_digest(real_conversation_key)}"
    _real_conversation_keys[stable] = real_conversation_key
    _stable_conversation_keys[real_conversation_key] = stable
    return stable


def _real_conversation_key(conversation_key: str) -> str:
    return _real_conversation_keys.get(conversation_key, conversation_key)


def _serializable_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """提取可持久化的 QQ 回复目标，避免把 Bot/Event 对象写进状态文件。"""
    allowed = {"type", "group_id", "user_id", "conversation_key"}
    return {key: target[key] for key in allowed if key in target and target[key] is not None}


def _load_route_state() -> None:
    """加载 stable conversation/session 到真实 QQ 目标的本地路由状态。"""
    path = Path(ONEBOT_STATE_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load OneBot route state: %s", path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    real_map = data.get("real_conversation_keys")
    if isinstance(real_map, dict):
        _real_conversation_keys.update({str(k): str(v) for k, v in real_map.items() if str(k) and str(v)})
        _stable_conversation_keys.update({str(v): str(k) for k, v in _real_conversation_keys.items() if str(k) and str(v)})

    targets = data.get("session_targets")
    if isinstance(targets, dict):
        for sid, target in targets.items():
            if isinstance(target, dict):
                _persisted_session_targets[str(sid)] = _serializable_target(target)

    logger.info("loaded OneBot route state from %s", path)


async def _save_route_state() -> None:
    """保存本地路由状态，保障 bot.py 重启后能把 stable key 映射回真实 QQ 目标。"""
    async with _route_state_lock:
        path = Path(ONEBOT_STATE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "real_conversation_keys": dict(_real_conversation_keys),
                "session_targets": {
                    sid: _serializable_target(target)
                    for sid, target in {**_persisted_session_targets, **_session_targets}.items()
                    if isinstance(target, dict)
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("failed to save OneBot route state: %s", path, exc_info=True)


async def _remember_route_state(
    *,
    stable_conversation_key: str,
    real_conversation_key: str,
    session_id: str = "",
    target: Optional[Dict[str, Any]] = None,
) -> None:
    if stable_conversation_key and real_conversation_key:
        _real_conversation_keys[stable_conversation_key] = real_conversation_key
        _stable_conversation_keys[real_conversation_key] = stable_conversation_key
    if session_id and target:
        _persisted_session_targets[session_id] = _serializable_target(target)
    await _save_route_state()


def _bot_self_id_candidates(event: Event | None, bot: Bot | None) -> set[str]:
    """收集 Bot 自身 QQ 号候选值，兼容不同 OneBot/NoneBot 对 self_id 的暴露方式。"""
    candidates: set[str] = set()
    for source in (bot, event):
        if source is None:
            continue
        for attr in ("self_id", "bot_id"):
            value = getattr(source, attr, None)
            if value is not None and str(value).strip():
                candidates.add(str(value).strip())
    return candidates


def _qq_matches_bot(qq: Any, bot_ids: set[str]) -> bool:
    value = str(qq or "").strip()
    return bool(value and value in bot_ids)


def _segment_type_and_data(segment: Any) -> tuple[str, dict[str, Any]]:
    """兼容 NoneBot MessageSegment 与 OneBot dict segment。"""
    if isinstance(segment, dict):
        seg_type = str(segment.get("type") or "")
        raw_data = segment.get("data")
        return seg_type, raw_data if isinstance(raw_data, dict) else {}
    seg_type = str(getattr(segment, "type", "") or "")
    raw_data = getattr(segment, "data", {}) or {}
    return seg_type, raw_data if isinstance(raw_data, dict) else {}


def _group_message_mentions_bot(message: Any, bot_ids: set[str]) -> bool:
    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for segment in segments:
        seg_type, data = _segment_type_and_data(segment)
        if seg_type != "at":
            continue
        if _qq_matches_bot(data.get("qq"), bot_ids):
            return True
    if isinstance(message, str):
        return _raw_message_mentions_bot(message, bot_ids)
    return False


def _raw_message_mentions_bot(raw_message: Any, bot_ids: set[str]) -> bool:
    raw = str(raw_message or "")
    if not raw or not bot_ids:
        return False
    for match in _AT_QQ_RE.finditer(raw):
        if _qq_matches_bot(match.group(1), bot_ids):
            return True
    return False


def _event_mentions_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    """判断群消息是否 @ 当前 Bot。

    NapCat/OneBot 实现和 NoneBot 版本不同，@Bot 可能表现为：
    1. 标准 MessageSegment.at；
    2. event.to_me / event.is_tome()；
    3. raw_message 中的 CQ/日志风格 at 标记。

    这里把三种信号都纳入判断，避免控制台能看到 [at:qq=...] 但
    mention_only 规则没有触发正式回复。
    """
    bot_ids = _bot_self_id_candidates(event, bot)
    try:
        if bool(getattr(event, "to_me", False)):
            return True
    except Exception:
        pass
    try:
        is_tome = getattr(event, "is_tome", None)
        if callable(is_tome) and bool(is_tome()):
            return True
    except Exception:
        pass
    try:
        if _group_message_mentions_bot(event.get_message(), bot_ids):
            return True
    except Exception:
        pass
    return _raw_message_mentions_bot(getattr(event, "raw_message", ""), bot_ids)


def _group_should_trigger(event: GroupMessageEvent, bot: Bot, text: str) -> bool:
    mode = (GROUP_TRIGGER or "mention_only").lower()
    if mode in {"all", "always"}:
        return True
    mentions_bot = _event_mentions_bot(event, bot)
    if mode in {"prefix", "prefix_or_mention", "mention_or_prefix"}:
        return mentions_bot or any((text or "").strip().startswith(prefix) for prefix in TRIGGER_PREFIXES)
    return mentions_bot


def _strip_trigger_prefix(text: str) -> str:
    value = (text or "").strip()
    for prefix in TRIGGER_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):].strip() or value
    return value


async def _auto_like_user(bot: Bot, user_id: int) -> None:
    if not ENABLE_AUTO_LIKE:
        return
    today = dt.datetime.now(_CST).strftime("%Y-%m-%d")
    if _auto_like_today.get(user_id) == today:
        return
    try:
        await bot.call_api("send_like", user_id=int(user_id), times=int(AUTO_LIKE_TIMES))
        _auto_like_today[user_id] = today
    except Exception:
        logger.debug("send_like failed for user=%s", user_id, exc_info=True)


async def _build_inbound_text(event: GroupMessageEvent, bot: Bot, user_text: str, conversation_key: str, attachments: List[Dict[str, Any]]) -> str:
    """组装提交给 tangqiu_main 的群聊 inbound 文本，并匿名化 QQ 群号/用户号。"""
    group_id = int(event.group_id)
    history_lines = list(_group_history[group_id])[-_HISTORY_MAX_LEN:]
    current_name = _anonymize_text_for_ai(_sender_display_name(event.sender, event.user_id))
    current_user = _anonymize_user_id(event.user_id)
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")

    parts: List[str] = ["【群聊上下文记录】"]
    parts.extend(history_lines or ["（暂无）"])

    reply_context, quoted_attachments = await _build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])
    custom_face_prompt = _custom_face_prompt_block()
    if custom_face_prompt:
        parts.extend(["", custom_face_prompt])

    parts.extend([
        "",
        f"当前时间: {now}",
        "【当前用户身份】",
        *_user_identity_lines(event.user_id, current_name),
        "",
        "【当前用户指令】",
        f"{current_name}（{current_user}）: {_anonymize_text_for_ai(user_text)}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


async def _build_private_inbound_text(event: PrivateMessageEvent, bot: Bot, user_text: str, conversation_key: str, attachments: List[Dict[str, Any]]) -> str:
    """组装提交给 tangqiu_main 的私聊 inbound 文本，并匿名化 QQ 用户号。"""
    fallback_text = await _message_to_text_with_forward(bot, event.get_message(), getattr(bot, "self_id", None))
    text = _anonymize_text_for_ai((user_text or fallback_text).strip() or "你好")
    name = _anonymize_text_for_ai(_sender_display_name(event.sender, event.user_id))
    user = _anonymize_user_id(event.user_id)
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")
    parts: List[str] = [f"当前时间: {now}"]
    reply_context, quoted_attachments = await _build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])
    custom_face_prompt = _custom_face_prompt_block()
    if custom_face_prompt:
        parts.extend(["", custom_face_prompt])
    parts.extend([
        "",
        "【当前用户身份】",
        *_user_identity_lines(event.user_id, name),
        "",
        "【当前用户指令】",
        f"{name}（{user}）: {text}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


async def _try_preempt_running_task(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    inbound_text: str,
    attachments: Optional[List[Dict[str, Any]]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
) -> bool:
    """尝试把 QQ 新消息注入当前会话正在运行的入口任务。"""
    # 2026-05-03 修改原因：TangQiu 以前每次消息都 submit_inbound，会在同一
    # 会话已有入口任务运行时并发启动新任务。这里移植 Discord 的 Preempt V2：
    # 先查询当前 session 的入口任务，再用 preempt_task 注入新消息；目的
    # 是让同一会话的新指令打断并接续旧任务，而不是重复开任务。
    if _client is None or _session_state is None:
        return False

    existing_sid = _session_state.get_session_id(conversation_key)
    if not existing_sid:
        return False

    try:
        running_tasks = await _client.get_running_tasks(existing_sid)
    except Exception:
        logger.exception("preempt_v2: get running tasks failed")
        return False

    for rt in running_tasks:
        if not rt.task_id or not rt.is_user_entry:
            continue

        rt_src_seq = rt.source_inbound_seq or 0
        rt_trigger = _session_state.get_trigger(rt_src_seq) if rt_src_seq else None
        if not rt_trigger:
            result = _session_state.find_trigger_by_session(existing_sid)
            rt_trigger = result[1] if result else None

        if not is_dm:
            # 2026-05-03 修改原因：群聊里多个用户共享同一个 conversation_key。
            # 这里必须用旧 trigger 保存的 event.user_id 校验来源；无法确认同一用户
            # 时不执行 preempt，目的是避免一个群成员打断另一个群成员的任务。
            if not rt_trigger:
                continue
            rt_event = rt_trigger.platform_data.get("event")
            if getattr(rt_event, "user_id", None) != getattr(event, "user_id", None):
                continue

        try:
            ok = await _client.preempt_task(
                rt.task_id,
                message=inbound_text,
                attachments=attachments,
            )
        except Exception:
            logger.exception("preempt_v2: preempt task failed")
            continue

        if not ok:
            continue

        # 2026-05-03 修改原因：Preempt 成功后不会生成新的 inbound_seq。
        # 因此必须把旧 trigger 的平台对象更新为本次 QQ event 和 bot；目的
        # 是让最终回复引用新的触发消息，并继续发到正确的 QQ 会话。
        fresh_platform = dict(platform_updates)
        fresh_platform["last_typing_time"] = time.time()
        if rt_trigger:
            rt_trigger.platform_data.update(fresh_platform)
        elif rt_src_seq:
            _session_state.register_trigger(
                TriggerInfo(
                    inbound_seq=rt_src_seq,
                    conversation_key=conversation_key,
                    session_id=existing_sid,
                    is_dm=is_dm,
                    platform_data=fresh_platform,
                )
            )

        _session_targets[existing_sid] = dict(fresh_platform)
        _conversation_bots[conversation_key] = bot
        await _remember_route_state(
            stable_conversation_key=conversation_key,
            real_conversation_key=_real_conversation_key(conversation_key),
            session_id=existing_sid,
            target=fresh_platform,
        )
        logger.info("preempt_v2: injected into QQ task %s", rt.task_id[:8])
        return True

    return False


async def _submit_or_preempt_inbound(
    *,
    bot: Bot,
    event: Event,
    channel: str,
    real_conversation_key: str,
    stable_conversation_key: str,
    inbound_text: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
) -> bool:
    """提交或打断 QQ 入站消息；对外使用稳定哈希 conversation_key，对内保留真实路由。"""
    if _client is None or _session_state is None:
        return False

    if ENABLE_PREEMPT:
        preempt_ok = await _try_preempt_running_task(
            bot=bot,
            event=event,
            conversation_key=stable_conversation_key,
            inbound_text=inbound_text,
            attachments=attachments or None,
            is_dm=is_dm,
            platform_updates=platform_updates,
        )
        if preempt_ok:
            return True

    result = await _client.submit_inbound(
        channel=channel,
        conversation_key=stable_conversation_key,
        text=inbound_text,
        message_id=str(getattr(event, "message_id", "")),
        attachments=attachments or None,
        use_context=True,
        entry_node_id=ENTRY_NODE_ID,
        platform_auth={
            "platform": "qq",
            "user_id": str(getattr(event, "user_id", "")),
            "is_admin": _is_admin_user(getattr(event, "user_id", "")),
        },
        route_hints={
            "platform": "qq",
            "channel": channel,
            "has_image": any(str(a.get("type") or "") == "image" for a in (attachments or [])),
            "target_type": "private" if is_dm else "group",
        },
    )
    if not result.session_id or not result.accepted:
        return False

    _real_conversation_keys[stable_conversation_key] = real_conversation_key
    _session_state.register_session(stable_conversation_key, result.session_id)
    target = dict(platform_updates)
    target["conversation_key"] = stable_conversation_key
    _session_targets[result.session_id] = target
    _conversation_bots[stable_conversation_key] = bot
    await _remember_route_state(
        stable_conversation_key=stable_conversation_key,
        real_conversation_key=real_conversation_key,
        session_id=result.session_id,
        target=target,
    )

    if result.inbound_seq:
        react_meta: Dict[str, Any] = {}
        if platform_updates.get("type") == "group" and ENABLE_REACTIONS:
            react_meta = {
                "_react_stage": "submitted",
                "_react_stage_emoji": _REACT_STAGE_EMOJIS["submitted"],
            }
        trigger = TriggerInfo(
            inbound_seq=result.inbound_seq,
            conversation_key=stable_conversation_key,
            session_id=result.session_id,
            is_dm=is_dm,
            platform_data={
                **platform_updates,
                "last_typing_time": time.time(),
                **react_meta,
            },
        )
        _session_state.register_trigger(trigger)
    return True


async def _enqueue_or_submit_inbound(item: QueuedInbound) -> bool:
    if not ENABLE_QQ_QUEUE:
        return await _submit_or_preempt_inbound(
            bot=item.bot,
            event=item.event,
            channel=item.channel,
            real_conversation_key=item.real_conversation_key,
            stable_conversation_key=item.stable_conversation_key,
            inbound_text=item.text,
            user_text=item.user_text,
            attachments=item.attachments,
            is_dm=item.is_dm,
            platform_updates=item.platform_updates,
        )

    async with _qq_queue_condition:
        existing = _qq_queue_by_key.get(item.stable_conversation_key)
        if existing is not None:
            existing.text = f"{existing.text}\n\n【排队期间追加消息】\n{item.text}"
            existing.attachments.extend(item.attachments)
            existing.event = item.event
            existing.platform_updates = item.platform_updates
            existing.user_text = item.user_text
        else:
            _qq_queue.append(item)
            _qq_queue_by_key[item.stable_conversation_key] = item
        _qq_queue_condition.notify()
    return True


async def _qq_queue_worker_forever() -> None:
    while True:
        async with _qq_queue_condition:
            while not _qq_queue:
                await _qq_queue_condition.wait()
            item = _qq_queue.popleft()
            _qq_queue_by_key.pop(item.stable_conversation_key, None)

        reply_event: Optional[asyncio.Event] = None
        if QQ_QUEUE_WAIT_FOR_REPLY:
            reply_event = asyncio.Event()
            _qq_waiting_replies[item.stable_conversation_key] = reply_event
        try:
            await _submit_or_preempt_inbound(
                bot=item.bot,
                event=item.event,
                channel=item.channel,
                real_conversation_key=item.real_conversation_key,
                stable_conversation_key=item.stable_conversation_key,
                inbound_text=item.text,
                user_text=item.user_text,
                attachments=item.attachments,
                is_dm=item.is_dm,
                platform_updates=item.platform_updates,
            )
            if reply_event is not None:
                try:
                    await asyncio.wait_for(reply_event.wait(), timeout=QQ_QUEUE_REPLY_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("QQ queue reply wait timed out conversation=%s timeout=%ss", item.real_conversation_key, QQ_QUEUE_REPLY_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to process QQ queue item conversation=%s", item.real_conversation_key)
        finally:
            if reply_event is not None:
                _qq_waiting_replies.pop(item.stable_conversation_key, None)
        if QQ_QUEUE_INTERVAL > 0:
            await asyncio.sleep(QQ_QUEUE_INTERVAL)



def _group_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_group:{group_id} 会话键解析群号。"""
    prefix = "qq_group:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _private_user_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_private:{user_id} 会话键解析私聊用户号。"""
    prefix = "qq_private:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _target_from_conversation_key(conversation_key: str) -> Optional[Dict[str, Any]]:
    """根据 conversation_key 还原 QQ 回复目标，供无 trigger 的回调兜底使用。"""
    # 2026-05-01 修改原因：EventRouter 的 fallback 回调只有 conversation_key；
    # 这里集中解析 group/private 两种 key，避免发送逻辑继续硬编码群聊。
    conversation_key = _real_conversation_key(conversation_key)
    group_id = _group_id_from_conversation_key(conversation_key)
    if group_id is not None:
        return {"type": "group", "group_id": group_id}
    user_id = _private_user_id_from_conversation_key(conversation_key)
    if user_id is not None:
        return {"type": "private", "user_id": user_id}
    return None


def _target_from_platform_data(platform_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 trigger 平台数据中提取 QQ 回复目标。"""
    # 2026-05-01 修改原因：主回调会携带 matcher 保存的平台数据；优先使用
    # 显式 type 字段区分私聊和群聊，避免只有 user_id 时误判群聊成员为私聊。
    # 2026-05-03 新增：提取触发消息 ID 和发送者 ID，发第一条回复时自动引用 + @
    event = platform_data.get("event")
    msg_id = getattr(event, "message_id", None) if event else None
    sender_id = getattr(event, "user_id", None) if event else None
    target_type = platform_data.get("type")
    conversation_key = str(platform_data.get("conversation_key") or "")
    if target_type == "private" and platform_data.get("user_id") is not None:
        t: Dict[str, Any] = {"type": "private", "user_id": int(platform_data["user_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        return t
    if target_type == "group" and platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    if platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    return None


def _get_fallback_bot() -> Optional[Bot]:
    """获取 fallback 发送用 Bot；EventRouter 可能在 matcher 外触发回调。"""
    if _last_bot is not None:
        return _last_bot
    try:
        return get_bot()
    except Exception:
        return None


def _truncate_qq_text(text: str) -> str:
    """限制单条 QQ 消息长度，避免超过 OneBot 实现的消息上限。"""
    if len(text) <= _QQ_MESSAGE_LIMIT:
        return text
    suffix = "\n（内容过长，已截断）"
    return text[: _QQ_MESSAGE_LIMIT - len(suffix)] + suffix


def _message_from_processed_segments(segments: List[Dict[str, Any]]) -> Message:
    """把 emoji_handler 的轻量段描述转换为 OneBot Message。"""
    message_segments: List[MessageSegment] = []
    for segment in segments:
        if segment.get("type") == "text":
            content = str(segment.get("content", ""))
            if content:
                message_segments.append(MessageSegment.text(content))
        elif segment.get("type") == "image":
            url = segment.get("url")
            if url:
                message_segments.append(MessageSegment.image(url))
        elif segment.get("type") == "at":
            qq_id = segment.get("qq")
            if qq_id:
                message_segments.append(MessageSegment.at(qq_id))
    return Message(message_segments)


def _mark_qq_reply_finished(conversation_key: str) -> None:
    for key in {conversation_key, _real_conversation_key(conversation_key)}:
        event = _qq_waiting_replies.get(key)
        if event is not None:
            event.set()


def _message_dedup_text(message: Any) -> str:
    """生成用于 QQ 发送幂等判断的稳定文本。"""
    try:
        if isinstance(message, Message):
            return str(message)
    except Exception:
        pass
    return str(message or "")


def _should_skip_duplicate_send(target: Dict[str, Any], message: Any, *, ttl: float = 30.0) -> bool:
    """短时间内跳过同一会话的完全相同消息，防止事件重放/双路径重复发送。"""
    target_type = str(target.get("type") or "")
    target_id = str(target.get("group_id") if target_type == "group" else target.get("user_id") or "")
    body = _message_dedup_text(message).strip()
    if not target_type or not target_id or not body:
        return False
    digest = hashlib.sha256(f"{target_type}:{target_id}:{body}".encode("utf-8", "ignore")).hexdigest()
    now = time.time()
    while _sent_reply_cache_order:
        old = _sent_reply_cache_order[0]
        ts = _sent_reply_cache.get(old, 0.0)
        if now - ts <= ttl and len(_sent_reply_cache_order) <= 512:
            break
        _sent_reply_cache_order.popleft()
        if ts and now - ts > ttl:
            _sent_reply_cache.pop(old, None)
    ts = _sent_reply_cache.get(digest)
    if ts and now - ts <= ttl:
        logger.warning("skip duplicate QQ send target=%s:%s digest=%s", target_type, target_id, digest[:10])
        return True
    _sent_reply_cache[digest] = now
    _sent_reply_cache_order.append(digest)
    return False


async def _send_qq_message(bot: Bot, target: Dict[str, Any], message: Any) -> None:
    """按 QQ 会话类型选择 OneBot 发送接口。"""
    # 2026-05-01 修改原因：私聊回复必须调用 send_private_msg，群聊回复继续调用
    # send_group_msg。通过 target 分发，回调发送文本和附件时不再硬编码群聊接口。
    if _should_skip_duplicate_send(target, message):
        return
    if target.get("type") == "private":
        user_id = target.get("user_id")
        if user_id is None:
            raise ValueError("private target missing user_id")
        await bot.send_private_msg(user_id=int(user_id), message=message)
        return
    if target.get("type") == "group":
        group_id = target.get("group_id")
        if group_id is None:
            raise ValueError("group target missing group_id")
        await bot.send_group_msg(group_id=int(group_id), message=message)
        return
    raise ValueError(f"unknown QQ target type: {target!r}")


async def _send_split_text(bot: Bot, target: Dict[str, Any], text: str) -> bool:
    """按 [SPLIT] 拆分最终文本，逐段发送并清理 QQ 不支持的标记。"""
    # 2026-05-01 修改原因：文本拆分逻辑对群聊和私聊相同，实际发送交给
    # _send_qq_message 处理，确保私聊也能复用表情替换和分段发送能力。
    sent_any = False
    parts = text.split(_SPLIT_SIGNAL) if text else []
    for index, raw_part in enumerate(parts):
        part = _truncate_qq_text(raw_part.strip())
        if not part:
            continue
        segments = await process_emojis(
            part,
            bot,
            _bqbs,
            _current_custom_face_names(),
            _current_custom_face_metadata(),
        )
        if not segments:
            continue
        msg = _message_from_processed_segments(segments)
        # 第一条消息带引用回复 + @发送者（与 ZhenXia 逻辑一致）
        if not sent_any and target.get("reply_message_id"):
            prefix = MessageSegment.reply(target["reply_message_id"])
            if target.get("reply_sender_id"):
                prefix = prefix + MessageSegment.at(target["reply_sender_id"]) + MessageSegment.text(" ")
            msg = prefix + msg
        await _send_qq_message(bot, target, msg)
        sent_any = True
        if index < len(parts) - 1:
            await asyncio.sleep(0.5)
    return sent_any


def _resolve_attachment_path(attachment: Dict[str, Any]) -> Optional[Path]:
    """把 Clonoth 附件 dict 解析为本地路径。"""
    raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else Path(CLONOTH_WORKSPACE) / path


async def _send_attachment_path(bot: Bot, target: Dict[str, Any], path: Path, filename: str = "") -> None:
    """图片附件发送为图片消息；非图片附件通过 NapCat 文件上传 API 发送。"""
    display_name = filename or path.name
    if path.exists() and path.suffix.lower() in _IMAGE_SUFFIXES:
        try:
            await _send_qq_message(bot, target, MessageSegment.image(file=path))
            return
        except Exception:
            # 部分 OneBot 实现不接受 Path 对象；失败后改用字符串路径重试以增强兼容性。
            await _send_qq_message(bot, target, MessageSegment.image(file=str(path)))
            return
    # [2026-05-08] 非图片附件通过 NapCat upload_group_file / upload_private_file 发送
    if not path.exists():
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}（文件不存在）")
        return
    try:
        file_str = str(path.resolve())
        if target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None:
                await bot.call_api("upload_group_file", group_id=group_id, file=file_str, name=display_name)
                return
        elif target.get("type") == "private":
            user_id = target.get("user_id")
            if user_id is not None:
                await bot.call_api("upload_private_file", user_id=user_id, file=file_str, name=display_name)
                return
        # fallback: 未知 target type
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}")
    except Exception as e:
        logger.warning("文件上传失败 (%s): %s", display_name, e)
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}（上传失败）")


async def _send_attachments(bot: Bot, target: Dict[str, Any], attachments: List[Dict[str, Any]]) -> None:
    """发送 Clonoth 返回的附件列表。"""
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        filename = str(attachment.get("name") or attachment.get("filename") or "")
        if path is None:
            if filename:
                await _send_qq_message(bot, target, f"Clonoth 生成了文件：{filename}")
            continue
        await _send_attachment_path(bot, target, path, filename=filename)


async def _send_text_and_attachments(bot: Bot, target: Dict[str, Any], text: str, attachments: List[Dict[str, Any]]) -> None:
    """统一发送最终文本与附件，并仅在群聊中把最终文本写回群历史。"""
    conv_key = target.get("conversation_key")
    # 2026-05-01 修改原因：私聊没有群历史缓存，不应写入 _group_history；群聊
    # 仍保留原来的 Bot 回复入库逻辑，维持后续 @Bot 请求的上下文连续性。
    if text:
        if await _send_split_text(bot, target, text) and target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None:
                _record_bot_reply(int(group_id), text)
    if attachments:
        await _send_attachments(bot, target, attachments)
    if conv_key:
        _mark_qq_reply_finished(str(conv_key))


async def _set_message_react(bot: Bot, event: Any, emoji_id: str, enabled: bool) -> bool:
    """设置或移除触发消息上的 QQ React，失败时静默返回 False。"""
    # 2026-05-03 修改原因：React 阶段切换需要多处复用 OneBot 扩展 API。
    # 做法是把单次 set_msg_emoji_like 调用包进独立函数，并强制 emoji_id 为 str；
    # 目的在于满足每次 API 调用都单独容错，避免 React 失败影响消息回复。
    if not ENABLE_REACTIONS or not bot or not event or not hasattr(event, "message_id"):
        return False
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=int(event.message_id),
            emoji_id=str(emoji_id),
            set=enabled,
        )
        return True
    except Exception:
        return False


async def _switch_react_stage(
    bot: Bot,
    event: Any,
    platform_data: Dict[str, Any],
    stage: str,
) -> None:
    """按单调阶段切换触发消息 React，防止流式事件造成重复抖动。"""
    # 2026-05-03 修改原因：stream_delta 会持续到达，tool 进度也可能被 sweep 多次看到。
    # 做法是在 trigger.platform_data 中记录当前阶段和 emoji_id，并只允许阶段前进；
    # 目的在于首个推理片段和首次工具调用各切换一次，不对每条事件重复调 API。
    next_order = _REACT_STAGE_ORDER.get(stage, 0)
    current_stage = str(platform_data.get("_react_stage") or "")
    current_order = _REACT_STAGE_ORDER.get(current_stage, 0)
    if not next_order or current_order >= next_order:
        return

    next_emoji = _REACT_STAGE_EMOJIS[stage]
    current_emoji = str(platform_data.get("_react_stage_emoji") or _REACT_STAGE_EMOJIS.get(current_stage, ""))
    if current_emoji and current_emoji != next_emoji:
        await _set_message_react(bot, event, current_emoji, False)
    elif not current_emoji:
        # 2026-05-03 修改原因：旧 trigger 或私聊路径可能没有阶段元数据。
        # 做法是在缺少当前 emoji 时清理所有已知阶段 emoji；目的在于补齐状态链时
        # 不让 76、281、178 或 97 残留在同一条触发消息上。
        for emoji_id in _REACT_CLEANUP_EMOJIS:
            if emoji_id != next_emoji:
                await _set_message_react(bot, event, emoji_id, False)

    if await _set_message_react(bot, event, next_emoji, True):
        platform_data["_react_stage"] = stage
        platform_data["_react_stage_emoji"] = next_emoji


async def _clear_message_reacts(bot: Bot, event: Any) -> None:
    """移除任务生命周期使用过的全部 QQ React。"""
    # 2026-05-03 修改原因：最终回复代表任务结束，触发消息不应继续显示中间状态。
    # 做法是按统一清理列表逐个移除 76、281、178、97、326；目的在于包含本轮
    # 新增的推理和工具阶段，并让任一移除失败都不影响最终回复发送。
    for emoji_id in _REACT_CLEANUP_EMOJIS:
        await _set_message_react(bot, event, emoji_id, False)




def _progress_mentions_search(record: str) -> bool:
    """判断进度记录是否与联网搜索工具相关。"""
    text = str(record or "").lower()
    return any(keyword in text for keyword in _SEARCH_PROGRESS_KEYWORDS)


async def _maybe_send_search_progress_notice(
    bot: Bot,
    target: Dict[str, Any],
    platform_data: Dict[str, Any],
) -> None:
    """在 QQ 侧为长搜索任务发送低频可见进度提示。"""
    now = time.time()
    if not platform_data.get("_qq_search_notice_sent"):
        platform_data["_qq_search_notice_sent"] = True
        platform_data["_qq_search_notice_last_at"] = now
        try:
            await _send_qq_message(bot, target, _SEARCH_PROGRESS_FIRST_NOTICE)
        except Exception:
            logger.debug("send QQ search progress notice failed", exc_info=True)
        return

    last_at = float(platform_data.get("_qq_search_notice_last_at") or 0.0)
    if now - last_at < _SEARCH_PROGRESS_STILL_RUNNING_INTERVAL_SEC:
        return
    platform_data["_qq_search_notice_last_at"] = now
    try:
        await _send_qq_message(bot, target, _SEARCH_PROGRESS_STILL_RUNNING_NOTICE)
    except Exception:
        logger.debug("send QQ search still-running notice failed", exc_info=True)

class TangQiuCallbacks:
    """Clonoth SDK 的 QQ 平台回调实现。

    发送最终回复、附件和主节点中间回复；其余进度、审批、typing、子任务日志均静默。

    2026-05-01 修改原因：QQ 端已开启 send_intermediate_reply，因此类说明需要
    同步说明中间回复会发送；审批流程仍由 Discord 端处理，避免 QQ 侧误展示审批控件。
    """

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        main_state: Optional[MainTaskState] = None,
    ) -> None:
        """发送主节点最终回复。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        if not bot or not target:
            logger.warning("send_reply skipped: missing bot or target for session=%s", trigger.session_id)
            return
        # 提取 [REACT:ID] 标记
        final_text = text or ""
        if final_text:
            from .emoji_handler import _extract_reactions
            final_text, reactions = _extract_reactions(final_text)
            if reactions:
                await self.add_reactions(trigger, reactions)
        await _send_text_and_attachments(bot, target, final_text, attachments or [])
        # 2026-05-03 修改原因：最终回复到达时，触发消息上可能仍残留生命周期 React。
        # 做法是调用统一清理函数移除 76、281、178、97、326，目的在于把本轮
        # 新增的推理和工具阶段也纳入最终收尾。
        event = platform_data.get("event")
        await _clear_message_reacts(bot, event)

    async def send_reply_attachment(self, session_id: str, path: str, *args: Any, **kwargs: Any) -> None:
        """兼容旧式 session_id 附件回调；当前 SDK 通常把附件放在 send_reply 中。"""
        target = _session_targets.get(session_id) or _persisted_session_targets.get(session_id)
        if not target:
            return
        bot = target.get("bot") or _get_fallback_bot()
        if bot:
            await _send_attachment_path(bot, target, Path(path))

    async def send_intermediate_reply(self, trigger: TriggerInfo, text: str) -> None:
        """发送主节点中间回复，但不写入群历史。"""
        if not text:
            return
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        if not bot or not target:
            return
        # 2026-05-03 修改原因：中间回复回调表示 Clonoth 已经开始产出用户可见内容。
        # 做法是通过统一阶段切换进入 writing，目的在于从 281、178 或 97 中
        # 任一状态平滑切到 326，并继续让 React API 失败不影响中间文本发送。
        event = platform_data.get("event")
        await _switch_react_stage(bot, event, platform_data, "writing")
        # 提取 [REACT:ID] 标记
        from .emoji_handler import _extract_reactions
        text, reactions = _extract_reactions(text)
        if reactions:
            await self.add_reactions(trigger, reactions)
        if text:
            if await _send_split_text(bot, target, text):
                conv_key = target.get("conversation_key")
                if conv_key:
                    _mark_qq_reply_finished(str(conv_key))

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        node_id: str = "",
    ) -> None:
        """处理没有 trigger 的 fallback 最终输出。"""
        target = _target_from_conversation_key(conversation_key)
        if target is None:
            return
        bot = _conversation_bots.get(conversation_key) or _get_fallback_bot()
        if bot:
            await _send_text_and_attachments(bot, target, text or "", attachments or [])

    async def delete_status_message(self, trigger: TriggerInfo) -> None:
        return None

    async def edit_status_message(self, trigger: TriggerInfo, content: str) -> None:
        return None

    async def update_progress(self, trigger: TriggerInfo, state: MainTaskState) -> None:
        """根据主任务进度切换触发消息上的 React 表情。"""
        # 2026-05-03 修改原因：EventRouter 不展示 QQ 进度文本，但 QQ 侧需要补齐
        # React 链中的 LLM 推理和工具阶段。做法是读取 MainTaskState.stream_parts
        # 与 progress_records：首次流式文本进入 thinking，真实工具执行进度进入 tool；
        # 目的在于复用 SDK sweep 回调并用阶段元数据避免高频 stream_delta 抖动。
        platform_data = trigger.platform_data
        event = platform_data.get("event")
        if not event or not hasattr(event, "message_id"):
            return
        bot = platform_data.get("bot") or _get_fallback_bot()
        if not bot:
            return

        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        has_tool_progress = any("执行" in record and "个工具" in record for record in state.progress_records)
        has_search_progress = any(_progress_mentions_search(record) for record in state.progress_records)
        if has_tool_progress or has_search_progress:
            await _switch_react_stage(bot, event, platform_data, "tool")
            if has_search_progress and target:
                await _maybe_send_search_progress_notice(bot, target, platform_data)
            return
        if state.stream_parts:
            await _switch_react_stage(bot, event, platform_data, "thinking")

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: Optional[TriggerInfo] = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        return None

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        return None

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        return None

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: Dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """QQ 端审批必须由管理员私聊确认；不再自动放行。"""
        if _client is None:
            logger.warning(
                "approval review skipped on QQ: client unavailable id=%s operation=%s",
                approval_id,
                operation,
            )
            return

        _pending_approvals[approval_id] = {
            "operation": operation,
            "details": dict(details or {}),
            "conversation_key": conversation_key,
            "session_id": session_id,
            "created_at": time.time(),
        }

        if not ADMIN_QQ_USERS:
            try:
                await _client.approve(
                    approval_id,
                    decision="deny",
                    comment="QQ adapter denied because CLONOTH_ADMIN_QQ_USERS is not configured.",
                )
            except Exception:
                logger.exception("approval deny failed on QQ: id=%s operation=%s", approval_id, operation)
            _pending_approvals.pop(approval_id, None)
            logger.warning("approval denied on QQ: no admin users configured id=%s operation=%s", approval_id, operation)
            return

        bot = _get_fallback_bot()
        if not bot:
            logger.warning("approval review pending but no QQ bot available: id=%s operation=%s", approval_id, operation)
            return

        summary = _approval_summary(approval_id, operation, details or {})
        delivered = 0
        for admin_id in ADMIN_QQ_USERS:
            try:
                await bot.send_private_msg(user_id=int(admin_id), message=summary)
                delivered += 1
            except Exception:
                logger.exception("send approval request to QQ admin failed: admin=%s id=%s", admin_id, approval_id)

        target = _target_from_conversation_key(conversation_key)
        if target:
            try:
                await _send_qq_message(bot, target, "当前操作需要 Clonoth 管理员审批，已提交等待处理。")
            except Exception:
                logger.debug("send approval pending notice failed: id=%s", approval_id, exc_info=True)

        logger.info(
            "approval pending on QQ: id=%s operation=%s admins_notified=%s",
            approval_id,
            operation,
            delivered,
        )

    async def refresh_typing(self, trigger: TriggerInfo) -> None:
        return None

    async def add_reactions(self, trigger: TriggerInfo, reactions: List[str]) -> None:
        """在 QQ 消息上添加表情反应。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        event = platform_data.get("event")
        if not bot or not event or not hasattr(event, "message_id"):
            return
        for emoji_id in reactions:
            eid = emoji_id.strip()
            if not eid:
                continue
            try:
                await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id=eid, set=True)
            except Exception:
                pass

    async def on_task_created(self, trigger: TriggerInfo, task_id: str) -> None:
        return None

    async def on_restart_signal(self, conversation_key: str) -> None:
        return None

    async def on_context_reset(self, conversation_key: str, reason: str, cleaned_triggers: List[TriggerInfo]) -> None:
        """上下文重置时同步清理 QQ 侧历史缓存。"""
        target = _target_from_conversation_key(conversation_key)
        if target and target.get("type") == "group" and reason != "compact":
            group_id = target.get("group_id")
            if group_id is not None:
                _group_history.pop(int(group_id), None)

    async def on_engine_restarted(self, payload: Dict[str, Any]) -> None:
        return None

    async def on_task_complete(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_fail(self, *args: Any, **kwargs: Any) -> None:
        return None


@driver.on_startup
async def _startup() -> None:
    """NoneBot 启动时初始化 Clonoth SDK 与事件路由。"""
    global _client, _session_state, _event_router, _router_task, _qq_queue_tasks, _callbacks, _bqbs, _custom_face_names, _custom_face_metadata
    if _router_task is not None and not _router_task.done():
        return

    global _QQ_USER_PROFILES
    _bqbs = load_bqbs(BQBS_PATH) if BQBS_PATH else []
    _custom_face_names = _load_custom_face_names_file()
    _custom_face_metadata = _load_custom_face_metadata_file()
    _QQ_USER_PROFILES = _load_qq_user_profiles(USER_PROFILES_PATH)
    if _QQ_USER_PROFILES:
        logger.info("loaded %d QQ user profiles", len(_QQ_USER_PROFILES))
    _load_route_state()
    _client = ClonothClient(CLONOTH_BASE_URL)
    _session_state = SessionState()
    for sid, target in list(_persisted_session_targets.items()):
        conv_key = str(target.get("conversation_key") or "")
        if conv_key:
            _session_state.register_session(conv_key, sid)
    _callbacks = TangQiuCallbacks()

    bot_config = BotConfig(
        base_url=CLONOTH_BASE_URL,
        entry_node_id=ENTRY_NODE_ID,
        conversation_key_prefix="qq_group",
        workspace_root=Path(CLONOTH_WORKSPACE),
        # QQ 侧不再自动审批内部操作；所有 approval_requested 都必须由
        # CLONOTH_ADMIN_QQ_USERS 中的管理员私聊明确同意后才会放行。
        auto_approve_internal=False,
    )
    _event_router = EventRouter(
        _client,
        _session_state,
        _callbacks,
        bot_config,
        entry_node_id=ENTRY_NODE_ID,
        poll_interval=1.0,
    )
    _router_task = asyncio.create_task(_event_router.run())
    if ENABLE_QQ_QUEUE and not any(not task.done() for task in _qq_queue_tasks):
        _qq_queue_tasks = [asyncio.create_task(_qq_queue_worker_forever()) for _ in range(QQ_QUEUE_WORKERS)]
        logger.info(
            "QQ queue enabled: workers=%s interval=%ss wait_for_reply=%s reply_timeout=%ss preempt=%s",
            QQ_QUEUE_WORKERS,
            QQ_QUEUE_INTERVAL,
            QQ_QUEUE_WAIT_FOR_REPLY,
            QQ_QUEUE_REPLY_TIMEOUT,
            ENABLE_PREEMPT,
        )
    logger.info("Clonoth Agent QQ adapter started: %s", CLONOTH_BASE_URL)


@driver.on_shutdown
async def _shutdown() -> None:
    """NoneBot 关闭时停止事件路由并释放 HTTP 连接。"""
    global _client, _event_router, _router_task, _qq_queue_tasks, _callbacks
    if _event_router is not None:
        _event_router.stop()
    if _router_task is not None:
        _router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _router_task
        _router_task = None
    if _qq_queue_tasks:
        for task in _qq_queue_tasks:
            task.cancel()
        for task in _qq_queue_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        _qq_queue_tasks = []
    if _client is not None:
        await _client.close()
        _client = None
    _event_router = None
    _callbacks = None
    logger.info("Clonoth Agent QQ adapter stopped")


# 普通群消息记录器。priority 较低且 block=False，只负责维护上下文缓存。
_history_matcher = on_message(rule=Rule(_allowed_group_rule), priority=99, block=False)


@_history_matcher.handle()
async def _handle_history(bot: Bot, event: GroupMessageEvent) -> None:
    """记录非触发消息，供后续 @Bot 请求作为群聊上下文。"""
    _remember_message_for_reply_context(event)
    if str(event.user_id) != str(getattr(bot, "self_id", "")):
        expanded_text = await _message_to_text_with_forward(bot, event.get_message(), getattr(bot, "self_id", None))
        _record_group_message(event, bot, override_text=expanded_text)
        # QQ 客户端经常把“问图文本 + 图片”拆成两条事件；第二条纯图片
        # 通常不会 @bot，因此不会进入 agent matcher。这里在低优先级历史
        # matcher 中把非触发图片也下载并写入近期缓存，让前一条文本等待后
        # 能合并到正确图片。
        try:
            image_urls = _iter_qq_image_urls(event.get_message())
        except Exception:
            image_urls = []
        if image_urls:
            real_conversation_key = f"qq_group:{int(event.group_id)}"
            stable_conversation_key = _stable_conversation_key(real_conversation_key)
            attachments, _errors = await _image_sources_to_attachments(image_urls, stable_conversation_key)
            _remember_recent_images(stable_conversation_key, event, attachments)


# Agent 入口 matcher。默认只处理 @Bot；也可通过 ONEBOT_GROUP_TRIGGER=prefix/all 切换触发策略。
_agent_matcher = on_message(rule=Rule(_agent_group_rule), priority=10, block=True)


@_agent_matcher.handle()
async def _handle_agent(bot: Bot, event: GroupMessageEvent) -> None:
    """把当前 QQ 群请求提交给 ClonothZX。"""
    global _last_bot
    _last_bot = bot

    if _client is None or _session_state is None:
        await _agent_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    _remember_message_for_reply_context(event)
    group_id = int(event.group_id)
    real_conversation_key = f"qq_group:{group_id}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    user_text = (await _message_to_text_with_forward(bot, event.get_message(), getattr(bot, "self_id", None))).strip() or "你好"
    user_text = _strip_trigger_prefix(user_text)
    _record_group_message(event, bot, override_text=user_text)
    asyncio.create_task(_auto_like_user(bot, int(event.user_id)))

    attachments, attachment_errors = await _collect_qq_attachments(event, stable_conversation_key)
    _remember_recent_images(stable_conversation_key, event, attachments)
    custom_face_reply = await _maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _agent_matcher.finish(custom_face_reply)
    await _merge_recent_images_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    inbound_text = await _build_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
    if attachment_errors:
        inbound_text += "\n\n【图片处理提示】\n" + "\n".join(dict.fromkeys(attachment_errors))
    if attachments:
        for att in attachments:
            inbound_text = inbound_text.replace("[图片]", f"[图片: {att['path']}]", 1)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "group",
        "group_id": group_id,
        "conversation_key": stable_conversation_key,
    }
    try:
        ok = await _enqueue_or_submit_inbound(QueuedInbound(
            matcher=_agent_matcher,
            bot=bot,
            event=event,
            channel="qq_group",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=False,
            platform_updates=platform_updates,
            user_text=user_text,
        ))
        if ENABLE_REACTIONS:
            await _set_message_react(bot, event, "281", True)
    except Exception as exc:
        logger.exception("submit inbound failed")
        await _agent_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _agent_matcher.finish("Clonoth Agent 未接受本次请求。")
    await _agent_matcher.finish()


# 私聊入口 matcher。私聊不需要 @Bot，直接阻断后续 matcher，避免重复响应。
_private_matcher = on_message(rule=Rule(_private_message_rule), priority=10, block=True)


@_private_matcher.handle()
async def _handle_private_agent(bot: Bot, event: PrivateMessageEvent) -> None:
    """把当前 QQ 私聊请求提交给 ClonothZX。"""
    global _last_bot
    _last_bot = bot

    if _client is None or _session_state is None:
        await _private_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    _remember_message_for_reply_context(event)
    user_id = int(event.user_id)
    user_text = (await _message_to_text_with_forward(bot, event.get_message(), getattr(bot, "self_id", None))).strip() or "你好"

    approval_command = _parse_approval_command(user_text)
    if approval_command is not None:
        if not _is_admin_user(user_id):
            await _private_matcher.finish("你不是 Clonoth 审批管理员，不能处理审批请求。")
        decision, approval_token = approval_command
        approval_id, error = _resolve_pending_approval_id(approval_token)
        if not approval_id:
            await _private_matcher.finish(error)
        try:
            ok = await _client.approve(
                approval_id,
                decision=decision,
                comment=f"QQ admin {user_id} {decision}ed via private message.",
            )
        except Exception as exc:
            logger.exception("submit QQ approval decision failed")
            await _private_matcher.finish(f"提交审批失败：{exc}")
        info = _pending_approvals.pop(approval_id, {}) if ok else _pending_approvals.get(approval_id, {})
        operation = str(info.get("operation") or "unknown")
        if ok:
            await _private_matcher.finish(f"已{('同意' if decision == 'allow' else '拒绝')}审批：{approval_id}\n操作：{operation}")
        await _private_matcher.finish("审批提交被 Supervisor 拒绝，请检查日志。")

    if not _is_private_allowed(event):
        await _private_matcher.finish("当前 QQ 私聊未被允许接入 Clonoth。")

    real_conversation_key = f"qq_private:{user_id}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    attachments, attachment_errors = await _collect_qq_attachments(event, stable_conversation_key)
    _remember_recent_images(stable_conversation_key, event, attachments)
    custom_face_reply = await _maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _private_matcher.finish(custom_face_reply)
    await _merge_recent_images_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    inbound_text = await _build_private_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
    if attachment_errors:
        inbound_text += "\n\n【图片处理提示】\n" + "\n".join(dict.fromkeys(attachment_errors))
    if attachments:
        for att in attachments:
            inbound_text = inbound_text.replace("[图片]", f"[图片: {att['path']}]", 1)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "private",
        "user_id": user_id,
        "conversation_key": stable_conversation_key,
    }
    try:
        ok = await _enqueue_or_submit_inbound(QueuedInbound(
            matcher=_private_matcher,
            bot=bot,
            event=event,
            channel="qq_private",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=True,
            platform_updates=platform_updates,
            user_text=user_text,
        ))
        try:
            await bot.call_api("set_input_status", user_id=int(event.user_id), event_type=1)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("submit private inbound failed")
        await _private_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _private_matcher.finish("Clonoth Agent 未接受本次请求。")
    await _private_matcher.finish()
