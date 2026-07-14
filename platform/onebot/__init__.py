"""TangQiu 的 Clonoth Agent QQ 接入插件。

本插件采用纯对话模式：QQ群成员 @Bot 或用户私聊 Bot 后，插件把当前请求提交到
ClonothZX；ClonothZX 返回最终结果后，插件再把最终回复发回对应 QQ 会话。
Clonoth 主动发出的中间回复会展示给 QQ；工具调用、进度日志、审批请求和子任务状态
仍不展示给 QQ。
"""
from __future__ import annotations

import asyncio
import base64
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
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Deque, Dict, List, Optional

import httpx
import yaml
from nonebot import get_bot, get_driver, on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, GroupUploadNoticeEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed
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
    ENABLE_FILE_INPUT,
    ENABLE_FORWARD_BRIDGE,
    ENABLE_FORWARD_MSG_INPUT,
    FORWARD_BRIDGE_HOST,
    FORWARD_BRIDGE_MAX_MESSAGES,
    FORWARD_BRIDGE_PORT,
    FORWARD_BRIDGE_TOKEN,
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
    FILE_MAX_BYTES,
    MAX_FILES_PER_TURN,
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
    REPLY_ATTACHMENT_CACHE_FILE,
    ANON_MAP_FILE,
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
    set_at_alias_resolver,
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
_DRAW_DIRECT_RE = re.compile(r"^(?:[/／!！]?\s*(?:生图|画图|绘图|nai生图|novelai生图)|/(?:draw|nai|novelai))\s*(.*)$", re.IGNORECASE)
_DRAW_HELP_RE = re.compile(r"^(?:[/／!！]?\s*(?:生图帮助|画图帮助|绘图帮助|画师串帮助)|/(?:drawhelp|naihelp))\s*$", re.IGNORECASE)
_DRAW_PRESET_LIST_RE = re.compile(r"^(?:[/／!！]?\s*(?:画师串列表|绘图预设列表|生图预设列表|画风列表)|/(?:drawpresets|presets))\s*$", re.IGNORECASE)
# [AutoC] 管理员快捷切换全局主模型：/切换模型 <模型名>。
# 兼容 /切模型、/setmodel、切换主模型 等别名；"模型帮助"/"当前模型" 用于查看。
_MODEL_SWITCH_RE = re.compile(
    r"^[/／!！]?\s*(?:切换模型|切换主模型|切模型|设置模型|/?(?:setmodel|switchmodel|model\s+set))\s+(.+?)\s*$",
    re.IGNORECASE,
)
_MODEL_SHOW_RE = re.compile(
    r"^[/／!！]?\s*(?:当前模型|查看模型|模型帮助|/?(?:model|showmodel|model\s+show))\s*$",
    re.IGNORECASE,
)
_DRAW_PRESET_SWITCH_RE = re.compile(r"^(?:[/／!！]?\s*(?:切换画师串|切换绘图预设|切换生图预设|切换画风)|/(?:setdrawpreset|preset))\s+(.+?)\s*$", re.IGNORECASE)
DRAW_NODE_ID = "draw.novelai_planner"
# 2026-07-09: 旧的"5~12 位数字一律当作 QQ 号"兜底匿名正则已废弃（会误伤金额/验证码/
# 日期等普通数字）。现改为只对采集入口登记的已知真实 ID 做精确匿名，不再保留该正则。
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


@dataclass
class ProactiveTarget:
    target_type: str
    target_id: int
    label: str


@dataclass
class GroupContentRecord:
    """保留最近群消息的结构化副本，供管理员自然语言转发筛选。"""

    formatted_line: str
    text: str
    sender_name: str
    sender_id: str
    timestamp: float
    message_id: str = ""
    attachments: List[Dict[str, Any]] | None = None


_recent_images: DefaultDict[str, Deque[RecentImageEntry]] = defaultdict(lambda: deque(maxlen=RECENT_IMAGE_MAX_ITEMS))
_group_content_records: DefaultDict[int, Deque[GroupContentRecord]] = defaultdict(lambda: deque(maxlen=max(_HISTORY_MAX_LEN, 20)))
# [2026-07-07] 按会话记录 Bot 最近发出/生成的附件（如生图插件产出的图片），
# 供 qq_forward “把刚才生成的那张图发给 xx”等自然语言请求检索。
# 只保存工作区内本地路径与显示名，不涉及真实 QQ 号。
_recent_sent_attachments: DefaultDict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max(RECENT_IMAGE_MAX_ITEMS, 20)))
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
    entry_node_id: str = ""


_qq_queue: Deque[QueuedInbound] = deque()
_qq_queue_by_key: Dict[str, QueuedInbound] = {}
_qq_queue_condition = asyncio.Condition()
_qq_waiting_replies: Dict[str, asyncio.Event] = {}
_auto_like_today: Dict[int, str] = {}
_reply_message_cache: Dict[str, Dict[str, Any]] = {}
_reply_message_cache_order: Deque[str] = deque()
# 持久化的“message_id -> 本地附件”索引。用于 NapCat get_msg 取不到引用消息时，
# 仍能转发此前已经下载到 Clonoth 的图片/表情包。只保存路径和少量路由元数据。
_reply_attachment_cache: Dict[str, Dict[str, Any]] = {}
_sent_reply_cache: Dict[str, float] = {}
_sent_reply_cache_order: Deque[str] = deque()
_route_state_lock = asyncio.Lock()
_anon_users: Dict[str, str] = {}
_anon_groups: Dict[str, str] = {}
_anon_user_reverse: Dict[str, str] = {}
_anon_group_reverse: Dict[str, str] = {}
# 显式维护“下一个编号”计数器。不再用 len(_anon_users) 推断，避免持久化恢复/
# TTL 回收后长度与实际已用编号不一致导致别名冲突。加载时以文件为准恢复。
_anon_user_next: int = 0
_anon_group_next: int = 0
_anon_map_dirty: bool = False
# 匿名映射写盘节流：新登记不再每次都立即写盘，而是合并到至少间隔 5s 后写一次。
# _anon_map_save_task 保存待触发的延迟 flush task；_anon_map_last_saved_at 记录上次落盘时间。
_ANON_MAP_SAVE_MIN_INTERVAL = max(0.0, float(os.environ.get("ONEBOT_ANON_MAP_SAVE_MIN_INTERVAL", "5.0")))
_anon_map_save_task: Optional["asyncio.Task[Any]"] = None
_anon_map_last_saved_at: float = 0.0

# 待管理员审批的 Clonoth 操作。key 为 approval_id，value 保存操作、详情和来源会话。
_pending_approvals: Dict[str, Dict[str, Any]] = {}

# 发给管理员的审批消息 message_id -> approval_id 映射。
# 用于支持管理员“引用审批消息回复 审批同意/拒绝”即可放行，无需再手输 approval_id。
# 采用有界字典，超过上限时清理最旧条目，避免无限增长。
_approval_message_ids: "OrderedDict[str, str]" = OrderedDict()
_APPROVAL_MSG_MAP_MAX = 500


def _remember_approval_message(message_id: Any, approval_id: str) -> None:
    """记录审批消息 id 到 approval_id 的映射，供引用回复审批时反查。"""
    if message_id is None or not str(message_id).strip() or not approval_id:
        return
    key = str(message_id).strip()
    _approval_message_ids[key] = approval_id
    _approval_message_ids.move_to_end(key)
    while len(_approval_message_ids) > _APPROVAL_MSG_MAP_MAX:
        _approval_message_ids.popitem(last=False)


def _resolve_approval_id_by_reply(reply_message_id: Any) -> Optional[str]:
    """根据被引用的审批消息 id 反查 approval_id，只返回仍在待审批列表中的。"""
    if reply_message_id is None:
        return None
    approval_id = _approval_message_ids.get(str(reply_message_id).strip())
    if approval_id and approval_id in _pending_approvals:
        return approval_id
    return None


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
        "【快捷审批】直接引用(回复)本消息，发送“同意”或“拒绝”即可。",
        f"或手动发送：审批 同意 {approval_id}",
        f"　　　　　审批 拒绝 {approval_id}",
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


def _parse_approval_reply_verb(text: str) -> Optional[str]:
    """从引用回复的文本中解析审批意图，返回 'allow' / 'deny' / None。

    用于“引用审批消息 + 回复同意/拒绝”的快捷审批：无需携带 approval_id，
    只要文本中出现同意/拒绝类关键词即可。容忍“审批同意”这种连写。
    """
    normalized = re.sub(r"\s+", "", (text or "").strip()).lower()
    if not normalized:
        return None
    # 去掉可能的“审批”/“approval”前缀，便于“审批同意”也能识别。
    for prefix in ("审批", "approval"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    allow_words = ("同意", "批准", "通过", "允许", "allow", "approve", "approved", "yes", "ok")
    deny_words = ("拒绝", "驳回", "不同意", "deny", "reject", "rejected", "no")
    # 先判断拒绝，避免“不同意”因包含“同意”而误判为 allow。
    if any(w in normalized for w in deny_words):
        return "deny"
    if any(w in normalized for w in allow_words):
        return "allow"
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
    """优先使用 QQ Profile 显示名，其次使用群名片/昵称，最后回退到匿名别名。

    2026-07-09 修改原因：取消了 `_anonymize_text_for_ai` 的泛数字兜底后，本函数不能再
    直接回退到裸 QQ 号，否则无名片/无昵称的发送者真实 QQ 号会泄露给模型。
    无可用名称时回退到稳定匿名别名（并登记映射）。
    """
    profile_name = _qq_profile_display_name(fallback_user_id)
    if profile_name:
        return profile_name
    card = getattr(sender, "card", "") or ""
    nickname = getattr(sender, "nickname", "") or ""
    if card or nickname:
        return _sanitize_name(card or nickname)
    return _anonymize_user_id(fallback_user_id) if str(fallback_user_id or "").strip() else "未知成员"


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
            # 没有 profile 显示名时回退到匿名别名（并登记映射），避免裸 QQ 号进入上下文。
            parts.append("@全体成员" if str(qq).lower() == "all" else f"@{profile_name or _anonymize_user_id(qq)}")
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
        elif seg_type == "file":
            name = str(data.get("name") or data.get("file_name") or data.get("file") or "").strip()
            parts.append(f"[文件:{_sanitize_name(Path(name).name if name else '附件', max_len=80)}]")
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
                parts.append("@全体成员" if qq.lower() == "all" else f"@{_qq_profile_display_name(qq) or _anonymize_user_id(qq)}")
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "face":
                parts.append(f"[QQ表情:{data.get('id', '')}]" if data.get("id") else "[QQ表情]")
            elif seg_type == "record":
                parts.append("[语音]")
            elif seg_type == "video":
                parts.append("[视频]")
            elif seg_type == "file":
                name = str(data.get("name") or data.get("file_name") or data.get("file") or "").strip()
                parts.append(f"[文件:{_sanitize_name(Path(name).name if name else '附件', max_len=80)}]")
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


# 合并转发子消息发送者无名片/昵称时的固定占位。
FORWARD_SENDER_PLACEHOLDER = "转发用户"


def _format_forward_sender(sender: Any) -> str:
    """返回合并转发子消息发送者的显示名。

    2026-07-09 修改原因：合并转发卡片里的发送者往往是与 Bot 无直接交互的路人
    （一张卡片可能带几十个陆陌号）。不应把这些无关 QQ 号写入匿名映射表（更不应持久化）。
    因此这里不再调 _anonymize_user_id（既不登记内存匿名表、也不落盘），直接用
    profile 显示名 / 群名片 / 昵称；都没有时回退到固定占位，绝不暴露裸 QQ 号。
    """
    if isinstance(sender, dict):
        user_id = str(sender.get("user_id") or "").strip()
        raw_name = str(sender.get("card") or sender.get("nickname") or "").strip()
    else:
        user_id = str(getattr(sender, "user_id", "") or "").strip()
        raw_name = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()
    display = _qq_profile_display_name(user_id) or raw_name
    return _sanitize_name(display) if display else FORWARD_SENDER_PLACEHOLDER


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
        sender_name = _format_forward_sender(item.get("sender"))
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
        # 合并转发子消息只展示发送者显示名，不附带匿名 ID（避免缓存无关号）。
        lines.append(f"{index}. [{_format_hhmm(item.get('time'))}] {sender_name}: {text}")
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
            return "@全体成员" if qq.lower() == "all" else f"@{_qq_profile_display_name(qq) or _anonymize_user_id(qq)}"
        if cq_type == "reply":
            return f"[回复:{params.get('id', '')}]"
        if cq_type == "image":
            return "[图片]"
        if cq_type == "face":
            return f"[QQ表情:{params.get('id', '')}]"
        if cq_type == "record":
            return "[语音]"
        if cq_type == "file":
            name = params.get("name") or params.get("file") or params.get("file_name") or "附件"
            return f"[文件:{_sanitize_name(Path(str(name)).name, max_len=80)}]"
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
    image_atts: list[dict[str, Any]] = []
    for att in attachments:
        if str(att.get("type") or "") == "image" and att.get("path"):
            att_copy = dict(att)
            image_atts.append(att_copy)
            q.append(RecentImageEntry(att_copy, now, sender_id, message_id))
    if image_atts and message_id:
        _remember_reply_attachments(message_id, conversation_key, sender_id, image_atts, created_at=now)


def _remember_reply_attachments(
    message_id: Any,
    conversation_key: str,
    sender_id: str,
    attachments: List[Dict[str, Any]],
    *,
    created_at: float | None = None,
) -> None:
    """持久化引用消息附件索引，供后续主动转发兜底使用。"""
    mid = str(message_id or "").strip()
    if not mid or not attachments:
        return
    now = time.time() if created_at is None else float(created_at)
    image_atts = [dict(att) for att in attachments if isinstance(att, dict) and att.get("path")]
    if not image_atts:
        return
    _reply_attachment_cache[mid] = {
        "conversation_key": str(conversation_key or ""),
        "sender_id": str(sender_id or ""),
        "created_at": now,
        "attachments": image_atts[:MAX_IMAGES_PER_TURN],
    }
    _trim_reply_attachment_cache()
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_save_reply_attachment_cache())
    except Exception:
        pass


def _trim_reply_attachment_cache() -> None:
    if not _reply_attachment_cache:
        return
    now = time.time()
    cutoff = now - max(IMAGE_CACHE_TTL_SECONDS, 60)
    for mid, item in list(_reply_attachment_cache.items()):
        try:
            created_at = float(item.get("created_at") or 0.0)
        except Exception:
            created_at = 0.0
        if created_at and created_at < cutoff:
            _reply_attachment_cache.pop(mid, None)
    while len(_reply_attachment_cache) > 1000:
        oldest = min(
            _reply_attachment_cache,
            key=lambda key: float(_reply_attachment_cache.get(key, {}).get("created_at") or 0.0),
        )
        _reply_attachment_cache.pop(oldest, None)


def _forward_nodes_from_cached_reply(bot: Bot, reply_message_id: Any) -> list[dict[str, Any]]:
    mid = str(reply_message_id or "").strip()
    if not mid:
        return []
    item = _reply_attachment_cache.get(mid)
    if not isinstance(item, dict):
        return []
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    if not attachments:
        return []
    node = _make_forward_node(
        bot,
        "",
        [dict(att) for att in attachments if isinstance(att, dict)],
        nickname="引用图片",
        user_id=item.get("sender_id") or getattr(bot, "self_id", "") or "10000",
    )
    return [node] if node else []


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
    """从 OneBot Message、segment list 或 CQ 字符串中提取 image/表情图片下载地址。"""
    urls: List[str] = []
    if message is None:
        return urls
    image_segment_types = {"image", "mface", "marketface"}
    if isinstance(message, str):
        for match in _CQ_RE.finditer(message):
            if match.group(1) not in image_segment_types:
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
        if seg_type not in image_segment_types:
            continue
        url = str(data.get("url") or data.get("path") or data.get("file") or "").strip()
        if url:
            urls.append(url)
    return urls


def _safe_attachment_name(name: str, default: str = "attachment") -> str:
    """清理 QQ 文件名，避免路径穿越和控制字符进入附件目录。"""
    raw = Path(str(name or "").replace("\\", "/")).name.strip().strip(". ")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    if not raw:
        raw = default
    return raw[:120]


def _iter_qq_file_sources(message: Any) -> List[Dict[str, Any]]:
    """从 OneBot file 段中提取可下载/可复制的普通文件来源。"""
    sources: List[Dict[str, Any]] = []
    if message is None:
        return sources

    def add_from_data(data: Dict[str, Any]) -> None:
        src = str(data.get("url") or data.get("path") or data.get("file") or "").strip()
        name = str(data.get("name") or data.get("file_name") or data.get("filename") or "").strip()
        if not name and src:
            name = Path(src.split("?", 1)[0].split("#", 1)[0]).name
        if not src and not name:
            return
        size_raw = data.get("size") or data.get("file_size") or data.get("filesize")
        try:
            size = int(size_raw) if size_raw is not None and str(size_raw).strip() else 0
        except Exception:
            size = 0
        sources.append({"source": src, "name": _safe_attachment_name(name, "file"), "size": size})

    if isinstance(message, str):
        for match in _CQ_RE.finditer(message):
            if match.group(1) != "file":
                continue
            add_from_data(_parse_cq_params(match.group(2) or ""))
        return sources

    try:
        segments = list(message)
    except Exception:
        return sources
    for segment in segments:
        if isinstance(segment, dict):
            seg_type = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        else:
            seg_type = getattr(segment, "type", "")
            data = getattr(segment, "data", {}) or {}
        if seg_type == "file":
            add_from_data(data)
    return sources


def _file_attachment_error_text(error: str) -> str:
    if error == "too_large":
        return f"文件太大，已超过当前限制 {FILE_MAX_BYTES // 1024 // 1024}MB。"
    if error == "no_source":
        return "我收到了文件消息，但当前 OneBot 事件没有提供可下载链接。"
    if error == "download_failed":
        return "我收到了文件，但下载失败了，可能是 QQ 临时链接已过期。"
    return "我收到了文件，但处理文件时失败了。"


async def _read_file_source_bytes(client: httpx.AsyncClient, source: str) -> tuple[bytes, str]:
    """读取 QQ 文件来源；支持 URL、file:// 和本地路径，并限制最大字节数。"""
    if source.startswith("file://"):
        local = Path(source[7:])
        if local.stat().st_size > FILE_MAX_BYTES:
            raise ValueError("too_large")
        return local.read_bytes(), "application/octet-stream"
    if re.match(r"^[a-zA-Z]:[\\/]", source) or source.startswith("/"):
        local = Path(source)
        if local.stat().st_size > FILE_MAX_BYTES:
            raise ValueError("too_large")
        return local.read_bytes(), "application/octet-stream"

    content = bytearray()
    content_type = "application/octet-stream"
    async with client.stream("GET", source) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", content_type)
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > FILE_MAX_BYTES:
                raise ValueError("too_large")
    return bytes(content), content_type


async def _file_sources_to_attachments(file_sources: List[Dict[str, Any]], conversation_key: str) -> tuple[list[dict], list[str]]:
    """下载/复制 OneBot 普通文件，返回 Clonoth 附件描述。"""
    result: list[dict] = []
    errors: list[str] = []
    if not ENABLE_FILE_INPUT or not file_sources:
        return result, errors

    _cleanup_old_qq_attachments()
    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("collect QQ file attachment cannot create directory %s: %s", att_dir, exc)
        return result, [_file_attachment_error_text("download_failed")]

    async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for item in file_sources[:MAX_FILES_PER_TURN]:
            source = str(item.get("source") or "").strip()
            name = _safe_attachment_name(str(item.get("name") or "file"), "file")
            try:
                size = int(item.get("size") or 0)
            except Exception:
                size = 0
            if size and size > FILE_MAX_BYTES:
                errors.append(_file_attachment_error_text("too_large"))
                continue
            if not source:
                errors.append(_file_attachment_error_text("no_source"))
                continue
            try:
                content, content_type = await _read_file_source_bytes(client, source)
                if not content:
                    errors.append(_file_attachment_error_text("download_failed"))
                    continue
                if len(content) > FILE_MAX_BYTES:
                    errors.append(_file_attachment_error_text("too_large"))
                    continue
                target_name = f"{os.urandom(8).hex()}_{name}"
                file_path = att_dir / target_name
                file_path.write_bytes(content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "file",
                    "path": rel_path,
                    "mime_type": content_type or "application/octet-stream",
                    "name": name,
                    "source": "onebot",
                })
            except ValueError as exc:
                errors.append(_file_attachment_error_text(str(exc) or "download_failed"))
            except Exception as exc:
                logger.warning("collect QQ file attachment failed: source=%s error=%s", source, exc)
                errors.append(_file_attachment_error_text("download_failed"))
    return result, errors


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
    """下载 QQ 当前消息中的图片/普通文件，并返回 Clonoth 附件列表。引用消息由增强 reply 逻辑单独处理。"""
    message = event.get_message() if hasattr(event, "get_message") else None
    try:
        image_urls = _iter_qq_image_urls(message)
    except Exception as exc:
        logger.warning("collect QQ image attachments skipped current message: %s", exc)
        image_urls = []
    try:
        file_sources = _iter_qq_file_sources(message)
    except Exception as exc:
        logger.warning("collect QQ file attachments skipped current message: %s", exc)
        file_sources = []
    image_attachments, image_errors = await _image_sources_to_attachments(image_urls, conversation_key)
    file_attachments, file_errors = await _file_sources_to_attachments(file_sources, conversation_key)
    return image_attachments + file_attachments, image_errors + file_errors


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


def _drawtools_help_text() -> str:
    return (
        "【NovelAI 生图命令】\n"
        "1) /生图 画一个穿着西装的帅哥\n"
        "   直接进入绘图节点并生成图片。\n"
        "2) /生图 初音未来，画风用可爱风\n"
        "   可在需求里指定画风/预设。\n"
        "3) /生图 给我初音未来的NAI提示词，不要画\n"
        "   只输出绘图 tag / prompt，不调用生图。\n"
        "4) /画师串列表\n"
        "   查看可用绘图预设。\n"
        "5) /切换画师串 可爱风\n"
        "   切换默认绘图预设。\n"
        "支持别名：/画图、/绘图、/nai、/draw。"
    )


def _load_drawtools_preset_manager():
    drawtools_dir = Path(CLONOTH_WORKSPACE) / "tools" / "drawtools"
    if str(drawtools_dir) not in sys.path:
        sys.path.insert(0, str(drawtools_dir))
    from preset_manager import list_presets, switch_preset  # type: ignore
    return list_presets, switch_preset


async def _maybe_handle_drawtools_command(user_text: str) -> str | None:
    """处理 QQ 侧绘图帮助/预设命令；返回回复文本，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None
    if _DRAW_HELP_RE.match(text):
        return _drawtools_help_text()
    if _DRAW_PRESET_LIST_RE.match(text):
        try:
            list_presets, _switch_preset = _load_drawtools_preset_manager()
            presets = list_presets()
        except Exception as exc:
            logger.warning("list draw presets failed: %s", exc, exc_info=True)
            return f"读取绘图预设失败：{exc}"
        if not presets:
            return "当前没有可用绘图预设。"
        lines = []
        for item in presets:
            mark = "✅ " if item.get("selected") else "   "
            lines.append(f"{mark}{item.get('name') or item.get('id')}（id: {item.get('id')}，model: {item.get('model')}，CFG: {item.get('scale')}，steps: {item.get('steps')}）")
        return "【可用画师串/绘图预设】\n" + "\n".join(lines) + "\n切换：/切换画师串 <名称或id>"
    switch_match = _DRAW_PRESET_SWITCH_RE.match(text)
    if switch_match:
        preset_ref = switch_match.group(1).strip()
        try:
            _list_presets, switch_preset = _load_drawtools_preset_manager()
            preset = switch_preset(preset_ref)
        except Exception as exc:
            logger.warning("switch draw preset failed: %s", exc, exc_info=True)
            return f"切换绘图预设失败：没找到“{preset_ref}”，可发送 /画师串列表 查看可用项。"
        return f"已切换默认画师串：{preset.get('name') or preset.get('id')}（id: {preset.get('id')}）"
    return None


async def _maybe_handle_model_command(
    *,
    event: Event,
    user_text: str,
) -> str | None:
    """处理 QQ 管理员 /切换模型 <模型名> 命令，切换全局主模型。

    返回回复文本；None 表示不是本命令。切换通过 Supervisor 的
    POST /v1/config/openai 实时写入 data/config.yaml；Engine 每次处理任务
    前都会重新拉取该配置（fetch_openai_secret），因此无需重启服务。
    """
    text = (user_text or "").strip()
    if not text:
        return None

    if _client is None:
        # 不是本命令时返回 None；确实是本命令但客户端未就绪时才提示。
        if _MODEL_SWITCH_RE.match(text) or _MODEL_SHOW_RE.match(text):
            return "Clonoth Agent 尚未初始化，请稍后重试。"
        return None

    is_admin = _is_admin_user(getattr(event, "user_id", ""))

    # 查看当前模型 / 帮助（仅管理员，避免向普通用户暴露底层模型名）。
    if _MODEL_SHOW_RE.match(text):
        if not is_admin:
            return "模型命令仅限 Clonoth 管理员使用。"
        try:
            cfg = await _client.get_openai_config()
        except Exception as exc:
            logger.warning("get openai config failed: %s", exc, exc_info=True)
            return f"❌ 获取当前模型失败：{exc}"
        return (
            f"ℹ️ 当前 model → {cfg.model or '(未设置)'}\n"
            "切换：/切换模型 <模型名>"
        )

    switch_match = _MODEL_SWITCH_RE.match(text)
    if switch_match:
        if not is_admin:
            # 不向非管理员暴露切换能力。
            return "模型命令仅限 Clonoth 管理员使用。"
        model_name = switch_match.group(1).strip()
        # 去掉可能的包裹引号/反引号。
        model_name = model_name.strip("`\"'").strip()
        if not model_name:
            return "用法：/切换模型 <模型名>"
        try:
            out = await _client.update_openai_config(model=model_name)
        except Exception as exc:
            logger.warning("switch global model failed: %s", exc, exc_info=True)
            return f"❌ 切换失败：{exc}"
        new_model = ""
        try:
            new_model = str((out or {}).get("openai", {}).get("model") or "").strip()
        except Exception:
            new_model = ""
        logger.info("QQ admin %s switched global model -> %s", getattr(event, "user_id", ""), new_model or model_name)
        return f"✅ model → {new_model or model_name}"

    return None


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


# ---------------------------------------------------------------------------
# 管理员主动发送 / 合并转发命令
# ---------------------------------------------------------------------------

_PROACTIVE_PRIVATE_KINDS = {"私聊", "好友", "private", "pm", "user"}
_PROACTIVE_GROUP_KINDS = {"群聊", "群", "group"}


def _normalize_target_ref(text: str) -> str:
    """把管理员输入的目标名规整为可匹配 token，不写入模型上下文。"""
    return re.sub(r"[\s_\-—]+", "", str(text or "").strip().lower())


def _proactive_help_text() -> str:
    return (
        "【管理员主动发送 / 转发命令】\n"
        "权限：仅 CLONOTH_ADMIN_QQ_USERS 中的管理员可用；命令在 QQ 适配器本地处理，不进入普通模型上下文。\n"
        "目标：使用联系人显示名/好友备注/群名/配置别名；目标列表不会展示真实 QQ 号。\n\n"
        "1) 查看可用目标\n"
        "   主动目标 / 主动目标 私聊 / 主动目标 群\n"
        "2) 主动发文本（可在同条消息附图，图片会一起发送）\n"
        "   私信 <联系人名> <内容>\n"
        "   群发 <群名> <内容>\n"
        "   发送 私聊 <联系人名> <内容>\n"
        "   发送 群 <群名> <内容>\n"
        "3) 主动发本地文件（路径限制在 Clonoth 工作区内，推荐 data/attachments/）\n"
        "   发文件 私聊 <联系人名> data/attachments/xxx.png\n"
        "   发文件 群 <群名> data/attachments/xxx.zip 展示文件名.zip\n"
        "4) 合并转发\n"
        "   合并转发 私聊 <联系人名> <内容>\n"
        "   合并转发 群 <群名> <内容>\n"
        "   也可以引用一条消息/合并转发卡片后发送：合并转发 群 <群名>\n"
        "   文本中用单独一行 --- 可拆成多条转发 node。\n\n"
        "5) 清除群记忆（清空指定 QQ 群的长期记忆）\n"
        "   /清除群记忆            —— 群聊内直接清空当前群；私聊内列出可清理的群\n"
        "   /清除群记忆 <群名>     —— 清空指定群（群名/别名/群号均可）"
    )


def _parse_proactive_command(text: str) -> dict[str, str] | None:
    """解析管理员主动发送/转发命令。

    返回字段：action(send/file/forward/list/help), target_type(private/group), target_ref, body。
    目标名按单个 token 解析；如群名含空格，请在配置里设置无空格别名/显示名。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if raw in {"主动发送帮助", "主动转发帮助", "通知帮助", "转发帮助"}:
        return {"action": "help"}
    parts = raw.split()
    if not parts:
        return None
    head = parts[0]
    if head in {"主动目标", "通知目标", "转发目标", "目标列表"}:
        kind = parts[1] if len(parts) >= 2 else ""
        return {"action": "list", "target_type": _canonical_proactive_target_type(kind)}
    if head in {"私信", "私聊通知"} and len(parts) >= 3:
        return {"action": "send", "target_type": "private", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head in {"群发", "群通知"} and len(parts) >= 3:
        return {"action": "send", "target_type": "group", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head in {"发送", "通知", "主动发送"} and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            return {"action": "send", "target_type": target_type, "target_ref": parts[2], "body": raw.split(None, 3)[3]}
    if head in {"发文件", "发送文件"} and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3]
            return {"action": "file", "target_type": target_type, "target_ref": parts[2], "body": body}
    if head in {"合并转发", "转发"} and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    if head in {"转发到", "转发给", "合并转发到", "合并转发给"} and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    # English aliases for admins that copy/paste commands.
    if lowered.startswith("send ") and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            return {"action": "send", "target_type": target_type, "target_ref": parts[2], "body": raw.split(None, 3)[3]}
    if lowered.startswith("forward ") and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    return None


def _canonical_proactive_target_type(kind: str) -> str:
    token = str(kind or "").strip().lower()
    if not token:
        return ""
    if token in _PROACTIVE_PRIVATE_KINDS:
        return "private"
    if token in _PROACTIVE_GROUP_KINDS:
        return "group"
    return ""


def _target_display_label(target_type: str, name: str) -> str:
    prefix = "私聊" if target_type == "private" else "群聊"
    return f"{prefix}「{_sanitize_name(name, max_len=40)}」"


def _profile_aliases_for_user(user_id: Any) -> list[str]:
    profile = _qq_user_profile(user_id)
    aliases: list[str] = []
    for key in ("display_name", "address_as", "title"):
        value = str(profile.get(key) or "").strip()
        if value:
            aliases.append(value)
    return aliases


async def _safe_call_onebot_list(bot: Bot, api_name: str) -> list[dict[str, Any]]:
    try:
        data = await bot.call_api(api_name)
    except Exception:
        logger.debug("OneBot list API failed: %s", api_name, exc_info=True)
        return []
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else data
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _private_target_candidates(bot: Bot) -> list[ProactiveTarget]:
    """获取管理员可主动私聊的目标；返回值不包含真实 QQ 字符串展示。"""
    friend_rows = await _safe_call_onebot_list(bot, "get_friend_list") if ALLOW_PRIVATE_FRIENDS else []
    friend_ids: set[int] = set()
    names_by_id: dict[int, list[str]] = defaultdict(list)
    for row in friend_rows:
        try:
            uid = int(row.get("user_id"))
        except Exception:
            continue
        friend_ids.add(uid)
        for key in ("remark", "nickname", "card"):
            name = str(row.get(key) or "").strip()
            if name:
                names_by_id[uid].append(name)
    allowed_ids = set(ADMIN_QQ_USERS) | set(ALLOWED_PRIVATE_USERS) | friend_ids
    # 只把 profile 作为别名来源；是否允许发送仍由 allowed_ids/friend list 决定。
    for raw_uid in _QQ_USER_PROFILES:
        try:
            uid = int(raw_uid)
        except Exception:
            continue
        if uid in allowed_ids:
            names_by_id[uid].extend(_profile_aliases_for_user(uid))
    candidates: list[ProactiveTarget] = []
    for uid in sorted(allowed_ids):
        aliases = [a for a in dict.fromkeys(names_by_id.get(uid, []) + _profile_aliases_for_user(uid)) if a]
        label = aliases[0] if aliases else f"User{len(candidates) + 1}"
        candidates.append(ProactiveTarget("private", uid, label))
    return candidates


async def _group_target_candidates(bot: Bot) -> list[ProactiveTarget]:
    rows = await _safe_call_onebot_list(bot, "get_group_list")
    allowed = set(ALLOWED_GROUPS)
    candidates: list[ProactiveTarget] = []
    seen: set[int] = set()
    for row in rows:
        try:
            gid = int(row.get("group_id"))
        except Exception:
            continue
        if allowed and gid not in allowed:
            continue
        seen.add(gid)
        name = str(row.get("group_name") or row.get("group_remark") or "").strip()
        candidates.append(ProactiveTarget("group", gid, _sanitize_name(name or f"Group{len(candidates) + 1}", max_len=40)))
    # 若 get_group_list 不可用，也允许配置白名单群通过“当前群/群名缓存”之外的显式候选参与解析。
    for gid in allowed:
        if gid not in seen:
            candidates.append(ProactiveTarget("group", int(gid), _anonymize_group_id(gid)))
    return candidates


def _candidate_alias_tokens(target: ProactiveTarget) -> set[str]:
    tokens = {_normalize_target_ref(target.label)}
    if target.target_type == "private":
        for alias in _profile_aliases_for_user(target.target_id):
            tokens.add(_normalize_target_ref(alias))
    elif target.target_type == "group":
        tokens.add(_normalize_target_ref(_anonymize_group_id(target.target_id)))
    return {t for t in tokens if t}


async def _resolve_proactive_target(bot: Bot, event: Event, target_type: str, ref: str) -> tuple[ProactiveTarget | None, str]:
    ref_text = str(ref or "").strip()
    if not ref_text:
        return None, "缺少目标名称。"
    ref_norm = _normalize_target_ref(ref_text)
    # 管理员显式输入 id 时允许解析，但目标列表/模型上下文不会展示真实 QQ 号。
    explicit = ref_text
    for prefix in ("qq:", "user:", "u:", "群:", "group:", "g:"):
        if explicit.lower().startswith(prefix):
            explicit = explicit[len(prefix):]
            break
    if explicit.isdigit():
        target_id = int(explicit)
        if target_type == "group":
            if ALLOWED_GROUPS and target_id not in ALLOWED_GROUPS:
                return None, "该群不在允许的主动群聊目标中。请先加入 CLONOTH_ALLOWED_GROUPS。"
            return ProactiveTarget("group", target_id, _anonymize_group_id(target_id)), ""
        allowed_private_ids = {t.target_id for t in await _private_target_candidates(bot)}
        if target_id not in allowed_private_ids:
            return None, "该私聊目标不在好友/管理员/允许私聊白名单中。"
        return ProactiveTarget("private", target_id, _qq_profile_display_name(target_id) or _anonymize_user_id(target_id)), ""

    if target_type == "group" and ref_norm in {_normalize_target_ref("当前群"), _normalize_target_ref("本群")} and isinstance(event, GroupMessageEvent):
        gid = int(event.group_id)
        if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS:
            return None, "当前群不在允许的主动群聊目标中。"
        return ProactiveTarget("group", gid, "当前群"), ""

    candidates = await (_private_target_candidates(bot) if target_type == "private" else _group_target_candidates(bot))
    matches = [target for target in candidates if ref_norm in _candidate_alias_tokens(target)]
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        kind = "私聊" if target_type == "private" else "群聊"
        return None, f"没有找到{kind}目标：{ref_text}\n可发送“主动目标 {kind}”查看可用名称。"
    labels = "、".join(_sanitize_name(m.label, max_len=24) for m in matches[:10])
    return None, f"目标名称不唯一：{ref_text}\n匹配到：{labels}\n请在配置中设置唯一显示名，或使用管理员显式 id 前缀。"


async def _proactive_target_list_text(bot: Bot, target_type: str = "") -> str:
    sections: list[str] = []
    if target_type in ("", "private"):
        privates = await _private_target_candidates(bot)
        names = [_sanitize_name(t.label, max_len=30) for t in privates if t.label]
        sections.append("【可主动私聊目标】\n" + ("、".join(names[:80]) if names else "（无；需好友列表、管理员或允许私聊白名单）"))
    if target_type in ("", "group"):
        groups = await _group_target_candidates(bot)
        names = [_sanitize_name(t.label, max_len=30) for t in groups if t.label]
        sections.append("【可主动群聊目标】\n" + ("、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"))
    return "\n\n".join(sections) + "\n\n提示：目标列表不展示真实 QQ 号；普通模型上下文也不会接触这些真实 id。"


def _target_to_send_dict(target: ProactiveTarget) -> Dict[str, Any]:
    if target.target_type == "private":
        return {"type": "private", "user_id": target.target_id}
    return {"type": "group", "group_id": target.target_id}


def _attachment_path_under_workspace(raw_path: str) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / path
    try:
        resolved = path.resolve()
        workspace = Path(CLONOTH_WORKSPACE).resolve()
        if workspace not in resolved.parents and resolved != workspace:
            return None
    except Exception:
        return None
    return resolved


def _parse_file_send_body(body: str) -> tuple[Dict[str, Any] | None, str]:
    parts = str(body or "").strip().split(maxsplit=1)
    if not parts:
        return None, "缺少文件路径。"
    path = _attachment_path_under_workspace(parts[0])
    if path is None:
        return None, "文件路径必须位于 Clonoth 工作区内。"
    if not path.exists() or not path.is_file():
        return None, f"文件不存在：{parts[0]}"
    display_name = parts[1].strip() if len(parts) > 1 else path.name
    rel_path = str(path)
    try:
        rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
    except Exception:
        pass
    return {"type": "file", "path": rel_path, "name": display_name}, ""


def _forward_media_text(message: Any, bot_self_id: Any = None) -> str:
    """为合并转发提取文本，去掉会重复出现的图片/表情占位符。"""
    text = _message_to_text_generic(message, bot_self_id)
    if _iter_qq_image_urls(message):
        # 合并转发 node 会单独携带 image segment；这里去掉模型可读占位，避免
        # 转发卡片里出现“[图片]”文字后面又跟真实图片。
        text = re.sub(r"\[(?:图片|mface|marketface|表情包|动画表情)\]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _forward_content_segments(text: str = "", attachments: list[dict[str, Any]] | None = None, image_urls: list[str] | None = None) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "data": {"text": str(text)}})
    for url in image_urls or []:
        if url:
            segments.append({"type": "image", "data": {"file": str(url)}})
    for att in attachments or []:
        path = _resolve_attachment_path(att)
        if path and path.exists() and path.suffix.lower() in _IMAGE_SUFFIXES:
            segments.append({"type": "image", "data": {"file": f"file://{str(path.resolve())}"}})
        elif att:
            name = _attachment_filename(att) or "附件"
            segments.append({"type": "text", "data": {"text": f"[附件: {name}]"}})
    return segments


def _make_forward_node(bot: Bot, text: str = "", attachments: list[dict[str, Any]] | None = None, *, nickname: str = "Clonoth 通知", user_id: Any = None, image_urls: list[str] | None = None) -> dict[str, Any] | None:
    content = _forward_content_segments(text, attachments, image_urls)
    if not content:
        return None
    return {
        "type": "node",
        "data": {
            "user_id": str(user_id or getattr(bot, "self_id", "") or "10000"),
            "nickname": _sanitize_name(nickname, max_len=32),
            "content": content,
        },
    }


async def _forward_messages_to_nodes(bot: Bot, messages: list[dict[str, Any]], conversation_key: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in messages[:_FORWARD_MSG_MAX_MESSAGES]:
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        sender_id = sender.get("user_id") or item.get("user_id") or getattr(bot, "self_id", "")
        nickname = sender.get("card") or sender.get("nickname") or "转发消息"
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = _forward_media_text(content, getattr(bot, "self_id", None))
        image_urls = _iter_qq_image_urls(content)
        attachments: list[dict[str, Any]] = []
        if image_urls:
            attachments, _errors = await _image_sources_to_attachments(image_urls, conversation_key)
        node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
        if node:
            nodes.append(node)
    return nodes


async def _forward_nodes_from_reply(bot: Bot, event: Event, conversation_key: str) -> list[dict[str, Any]]:
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply_obj = getattr(event, "reply", None)
    reply: Dict[str, Any] | None = None
    if reply_obj is not None:
        # NapCat/NoneBot 的私聊引用有时无法再通过 get_msg 取回，但事件本身
        # 已携带 reply 对象；主动转发优先使用事件内 reply，避免“引用了但无内容”。
        reply = {
            "message": getattr(reply_obj, "message", None),
            "raw_message": getattr(reply_obj, "raw_message", None),
            "sender": getattr(reply_obj, "sender", None),
            "user_id": getattr(reply_obj, "user_id", None),
            "time": getattr(reply_obj, "time", None),
        }
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        cached = _reply_message_cache.get(str(reply_message_id))
        if cached:
            reply = cached
    if (not reply or reply.get("message") is None) and reply_message_id is not None:
        reply = await _get_reply_message(bot, reply_message_id)
    if not reply:
        cached_nodes = _forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
        logger.info("forward reply skipped: no reply object/id=%s", reply_message_id)
        return []
    message = reply.get("message") if reply.get("message") is not None else reply.get("raw_message")
    forward_ids = _extract_forward_ids(message)
    if forward_ids:
        nodes: list[dict[str, Any]] = []
        for forward_id in forward_ids[:3]:
            messages = await _get_forward_messages(bot, forward_id)
            if messages:
                nodes.extend(await _forward_messages_to_nodes(bot, messages, conversation_key))
        if nodes:
            return nodes
    sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else {}
    sender_id = sender.get("user_id") or reply.get("user_id") or getattr(bot, "self_id", "")
    nickname = sender.get("card") or sender.get("nickname") or "引用消息"
    text = _forward_media_text(message, getattr(bot, "self_id", None))
    image_urls = _iter_qq_image_urls(message)
    attachments: list[dict[str, Any]] = []
    if image_urls:
        attachments, _errors = await _image_sources_to_attachments(image_urls, conversation_key)
        if attachments and reply_message_id is not None:
            _remember_reply_attachments(reply_message_id, conversation_key, str(sender_id or ""), attachments)
    if not attachments and reply_message_id is not None:
        cached_nodes = _forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
    node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
    return [node] if node else []


async def _send_forward_nodes(bot: Bot, target: ProactiveTarget, nodes: list[dict[str, Any]]) -> None:
    if target.target_type == "group":
        await bot.call_api("send_group_forward_msg", group_id=int(target.target_id), messages=nodes)
    else:
        await bot.call_api("send_private_forward_msg", user_id=int(target.target_id), messages=nodes)


# 注意：自然语言转发（“把 xx 转发给 xx”等）不再在 Bot 入口做本地正则拦截，
# 避免把本应交给 AI（qq.orchestrator）分析“哪些条目需要转发”的请求误譍为
# 本地转发指令。这类需求原样进入 Agent，由其调用 qq_forward 工具完成。


def _filter_attachments_by_kind(
    attachments: List[Dict[str, Any]],
    *,
    include_images: bool,
    include_files: bool,
) -> List[Dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("type") or "")
        if kind == "image" and include_images:
            selected.append(dict(att))
        elif kind == "file" and include_files:
            selected.append(dict(att))
        elif include_images and include_files:
            selected.append(dict(att))
    return selected


async def _maybe_handle_proactive_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理管理员主动私聊/群聊发送、文件发送、合并转发等显式命令请求。

    注意：这里只处理带明确命令前缀（如“合并转发/私信/群发/转发到”）的结构化命令。
    自然语言转发需求（例如“把上面聊到的 xxx 转发给我”）不在此拦截，而是原样交给
    AI（qq.orchestrator 节点）分析，由其调用 qq_forward 工具挑选并转发对应条目，
    避免与“让 QQ Bot 分析哪些条目需要转发”的初衷冲突。
    """
    command = _parse_proactive_command(user_text)
    is_admin = _is_admin_user(getattr(event, "user_id", ""))
    if command is None:
        return None
    if not is_admin:
        # 不向非管理员暴露“主动发送/转发”能力、目标列表或命令名称。
        return "该请求不可用。"
    action = command.get("action") or ""
    if action == "help":
        return _proactive_help_text()
    if action == "list":
        return await _proactive_target_list_text(bot, command.get("target_type") or "")

    target_type = command.get("target_type") or ""
    target, error = await _resolve_proactive_target(bot, event, target_type, command.get("target_ref") or "")
    if target is None:
        return error or "目标解析失败。"
    send_target = _target_to_send_dict(target)
    label = _target_display_label(target.target_type, target.label)

    if action == "send":
        body = str(command.get("body") or "").strip()
        attachments = current_attachments or []
        if not body and not attachments:
            return "发送内容为空。"
        await _send_text_and_attachments(bot, send_target, body, attachments)
        return f"已发送到{label}。"

    if action == "file":
        attachment, file_error = _parse_file_send_body(command.get("body") or "")
        if attachment is None:
            return file_error
        await _send_attachments(bot, send_target, [attachment])
        return f"已向{label}发送文件：{attachment.get('name') or attachment.get('path')}"

    if action == "forward":
        body = str(command.get("body") or "").strip()
        payload_attachments = current_attachments or []
        nodes: list[dict[str, Any]] = []
        if body:
            chunks = [chunk.strip() for chunk in re.split(r"\n\s*---\s*\n", body) if chunk.strip()]
            for chunk in chunks or [body]:
                node = _make_forward_node(bot, chunk, payload_attachments if not nodes else None)
                if node:
                    nodes.append(node)
        if not nodes:
            nodes = await _forward_nodes_from_reply(bot, event, conversation_key)
        if not nodes and payload_attachments:
            node = _make_forward_node(bot, "", payload_attachments)
            if node:
                nodes.append(node)
        if not nodes:
            return "没有可转发内容。请提供文本，或引用一条消息/合并转发卡片。"
        try:
            await _send_forward_nodes(bot, target, nodes)
        except Exception as exc:
            logger.warning("send forward message failed: %s", exc, exc_info=True)
            return "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"
        return f"已向{label}发送合并转发（{len(nodes)} 条 node）。"

    return None



# ---------------------------------------------------------------------------
#  管理员命令：/清除群记忆 —— 清空指定 QQ 群的长期 memory namespace
# ---------------------------------------------------------------------------

# 命令别名：群聊里直接清当前群；私聊里列可清理群名或按群名清指定群。
_CLEAR_GROUP_MEMORY_RE = re.compile(
    r"^[/／!！]?\s*(?:清除群记忆|清空群记忆|清理群记忆|清除群聊记忆|清空群聊记忆)\s*(.*)$"
)


def _parse_clear_group_memory_command(text: str) -> Optional[str]:
    """识别 /清除群记忆 命令。返回目标群引用（可为空字符串表示当前群/需列表）。

    返回 None 表示不是本命令。返回 "" 表示命令无参数；返回非空字符串表示
    管理员显式指定了群名/群号。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _CLEAR_GROUP_MEMORY_RE.match(raw)
    if match is None:
        return None
    return match.group(1).strip()


def _conversation_memory_namespace_for_group(group_id: int) -> str:
    """计算 QQ 群对应的 memory namespace 目录名。

    必须与 engine.builtin.knowledge_inject._conversation_memory_namespace 完全一致：
    对"真实 conversation_key"（qq_group:<群号>）做纯 SHA256 后取前 24 位，前缀 conv_。
    注意这里用真实 key 而非稳定哈希 key，因为 memory 落盘用的就是真实 key 的摘要。
    """
    real_key = f"qq_group:{int(group_id)}"
    digest = hashlib.sha256(real_key.encode("utf-8")).hexdigest()[:24]
    return f"conv_{digest}"


def _clear_group_memory_namespace(group_id: int) -> tuple[bool, int]:
    """删除指定群的 memory namespace 目录及其命中缓存条目。

    返回 (是否存在过目录, 删除的 memory book 文件数)。删除后清除 knowledge
    injector 的进程内缓存与磁盘 .hit_cache.json 中对应条目，避免残留。
    """
    namespace = _conversation_memory_namespace_for_group(group_id)
    mem_root = Path(CLONOTH_WORKSPACE) / "data" / "memory"
    ns_dir = mem_root / namespace
    deleted_books = 0
    existed = ns_dir.exists() and ns_dir.is_dir()
    if existed:
        try:
            deleted_books = sum(1 for _ in ns_dir.glob("*.yaml"))
        except Exception:
            deleted_books = 0
        import shutil as _shutil
        try:
            _shutil.rmtree(ns_dir)
        except Exception:
            logger.exception("remove memory namespace failed: %s", ns_dir)
            existed = False
    # 尽力使 engine 侧 memory catalog 缓存与磁盘命中缓存失效，避免被删条目仍被注入。
    try:
        from engine.builtin.knowledge_inject import _invalidate_cache as _ki_invalidate  # type: ignore
        _ki_invalidate(Path(CLONOTH_WORKSPACE), memory_book=namespace)
    except Exception:
        logger.debug("invalidate knowledge cache failed for %s", namespace, exc_info=True)
    _drop_hit_cache_entries()
    return existed, deleted_books


def _drop_hit_cache_entries() -> None:
    """删除 data/memory/.hit_cache.json，让被清群的命中记录一并作废。

    命中缓存以 entry id 为 key、不区分 namespace，被清群的 entry id 已随目录
    删除；直接移除整个命中缓存文件最简单且安全（下次访问会重建）。
    """
    hit_cache = Path(CLONOTH_WORKSPACE) / "data" / "memory" / ".hit_cache.json"
    try:
        if hit_cache.exists():
            hit_cache.unlink()
    except Exception:
        logger.debug("drop hit cache failed", exc_info=True)


async def _clearable_group_targets(bot: Bot) -> list[ProactiveTarget]:
    """列出管理员可清理记忆的群（复用主动群目标候选逻辑）。"""
    return await _group_target_candidates(bot)


async def _clear_group_memory_list_text(bot: Bot) -> str:
    """生成私聊场景下"可清理群记忆"的群名列表提示。"""
    groups = await _clearable_group_targets(bot)
    names = [_sanitize_name(t.label, max_len=30) for t in groups if t.label]
    body = "、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"
    return (
        "【可清理群记忆的群】\n"
        + body
        + "\n\n用法：/清除群记忆 <群名>\n"
        "例如：/清除群记忆 " + (names[0] if names else "某群")
        + "\n提示：群名可用 get_group_list 中的群名或配置别名；也可用 /清除群记忆 群号"
    )


async def _maybe_handle_clear_group_memory_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
) -> str | None:
    """处理管理员 /清除群记忆 命令。

    - 群聊中无参数：清空当前群记忆。
    - 私聊中无参数：返回可清理群列表 + 用法。
    - 任意场景带参数（群名/群号）：解析目标群并清空其记忆。
    非管理员一律拒绝，且不暴露命令能力细节。
    """
    target_ref = _parse_clear_group_memory_command(user_text)
    if target_ref is None:
        return None
    if not _is_admin_user(getattr(event, "user_id", "")):
        return "该请求不可用。"

    # 无参数：群聊直接清当前群；私聊给出可清理列表。
    if not target_ref:
        if isinstance(event, GroupMessageEvent):
            group_id = int(event.group_id)
            existed, count = _clear_group_memory_namespace(group_id)
            if existed:
                return f"已清空当前群的记忆（删除 {count} 个记忆本）。"
            return "当前群没有可清理的记忆（记忆目录为空或从未生成）。"
        return await _clear_group_memory_list_text(bot)

    # 带参数：解析目标群（复用主动发送的目标解析，支持群名/别名/显式群号）。
    target, error = await _resolve_proactive_target(bot, event, "group", target_ref)
    if target is None:
        # 附带可清理群列表，方便管理员纠正群名。
        list_hint = await _clear_group_memory_list_text(bot)
        return (error or "目标群解析失败。") + "\n\n" + list_hint
    existed, count = _clear_group_memory_namespace(int(target.target_id))
    label = _sanitize_name(target.label, max_len=40)
    if existed:
        return f"已清空群「{label}」的记忆（删除 {count} 个记忆本）。"
    return f"群「{label}」没有可清理的记忆（记忆目录为空或从未生成）。"


def _format_history_line(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> str:
    """把群消息格式化为 tangqiu_main 提示词要求的历史行，并匿名化 QQ ID。"""
    text = _anonymize_text_for_ai(_compact_text(override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))))
    name = _anonymize_text_for_ai(_sender_display_name(event.sender, event.user_id))
    user = _anonymize_user_id(event.user_id)
    return f"[{_format_hhmm(getattr(event, 'time', None))}] {name}({user}): {text}"


def _record_group_message(
    event: GroupMessageEvent,
    bot: Bot,
    override_text: str = "",
    attachments: List[Dict[str, Any]] | None = None,
) -> None:
    """记录群最近消息；@Bot 触发消息由 Agent matcher 手动记录，避免被 block 跳过。"""
    text = override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))
    if text.strip():
        line = _format_history_line(event, bot, override_text=text)
        group_id = int(event.group_id)
        _group_history[group_id].append(line)
        sender_id = str(getattr(event, "user_id", "") or "")
        _group_content_records[group_id].append(GroupContentRecord(
            formatted_line=line,
            text=_compact_text(text, limit=max(_HISTORY_TEXT_LIMIT, 1200)),
            sender_name=_sender_display_name(getattr(event, "sender", None), sender_id),
            sender_id=sender_id,
            timestamp=float(getattr(event, "time", None) or time.time()),
            message_id=str(getattr(event, "message_id", "") or ""),
            attachments=[dict(att) for att in (attachments or []) if isinstance(att, dict)],
        ))


def _record_bot_reply(group_id: int, text: str) -> None:
    """把 Bot 最终回复写回群历史，保持后续对话连续性。"""
    if not text:
        return
    text = strip_output_markers(text).replace(_SPLIT_SIGNAL, " ")
    text = _QQ_EMOJI_MARK_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
    text = _anonymize_text_for_ai(_compact_text(text))
    if text:
        line = f"[{dt.datetime.now(_CST).strftime('%H:%M')}] Bot: {text}"
        _group_history[int(group_id)].append(line)
        _group_content_records[int(group_id)].append(GroupContentRecord(
            formatted_line=line,
            text=text,
            sender_name="Bot",
            sender_id="",
            timestamp=time.time(),
            message_id="",
            attachments=[],
        ))


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

    # 判断引用的消息是否为 Bot 自己发出的。QQ Bot 的回复里可能带表情包图片，
    # 用户引用这类回复时不应该把 Bot 自己的图片下载成附件再送去识图——那既浪费
    # 算力，也会让模型误以为用户在要求识别一张图片。这里仅当被引用消息不是 Bot
    # 自身发出时才采集图片附件，否则保留 [图片] 文本占位符。
    reply_sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else None
    reply_sender_qq = ""
    if reply_sender is not None:
        reply_sender_qq = str(reply_sender.get("user_id") or reply.get("user_id") or "")
    else:
        reply_sender_qq = str(reply.get("user_id") or "")
    bot_ids = _bot_self_id_candidates(event, bot)
    reply_from_bot = _qq_matches_bot(reply_sender_qq, bot_ids)

    if reply_from_bot:
        # Bot 自己的回复：不下载图片附件，将所有图片占位符标注为表情包。
        quoted_attachments: list[dict[str, Any]] = []
        text = text.replace("[图片]", "[表情包]")
    else:
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


def _mark_anon_map_dirty() -> None:
    """标记匿名映射需要回写；带最小写盘间隔（默认 5s）的节流合并落盘。

    2026-07-09 修改原因：原实现每次新登记都 create_task 一次全量写盘，新用户
    集中涌入时会短时间多次写盘。改为节流：距上次落盘不足 _ANON_MAP_SAVE_MIN_INTERVAL
    时，只调度一个延迟 flush task（已存在则复用），把这段时间内的多次登记合并成一次写盘。
    关闭时的显式 flush 仍会强制写入最新别名。
    """
    global _anon_map_dirty, _anon_map_save_task
    _anon_map_dirty = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环（如同步测试上下文），保持 dirty，由下次显式保存处理。
        return
    # 已有等待中的 flush task 时直接复用，避免重复调度。
    if _anon_map_save_task is not None and not _anon_map_save_task.done():
        return
    _anon_map_save_task = loop.create_task(_anon_map_save_throttled())


async def _anon_map_save_throttled() -> None:
    """trailing 节流：触发后固定等待一个写盘窗口再写，窗口内的多次 dirty 合并为一次。

    为何固定等待而不是“距上次写盘”计算：后者在首次/间隔已满足时会立即写，
    导致高频场景下每隔一个间隔就写一次；固定等待一个窗口能保证“窗口内只写一次”。
    """
    global _anon_map_save_task
    try:
        if _ANON_MAP_SAVE_MIN_INTERVAL > 0:
            await asyncio.sleep(_ANON_MAP_SAVE_MIN_INTERVAL)
        await _save_anon_map()
    finally:
        _anon_map_save_task = None


def _load_anon_map() -> None:
    """以文件为准恢复 _anon_users/_anon_groups、反向表及“下一个编号”计数器。

    2026-07-09 新增：匿名别名映射持久化，保障跨重启同一人/群别名一致与可反解。
    加载优先从持久化的 next 计数器恢复；若旧文件无该字段，则根据已有别名推断。
    """
    global _anon_user_next, _anon_group_next
    path = Path(ANON_MAP_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load OneBot anon map: %s", path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    max_user_idx = -1
    users = data.get("users")
    if isinstance(users, dict):
        for real, item in users.items():
            real = str(real or "").strip()
            if not real or not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or "").strip()
            if not alias:
                continue
            _anon_users[real] = alias
            _anon_user_reverse[alias] = real
            max_user_idx = max(max_user_idx, _alias_to_index("User", alias))

    max_group_idx = -1
    groups = data.get("groups")
    if isinstance(groups, dict):
        for real, item in groups.items():
            real = str(real or "").strip()
            if not real or not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or "").strip()
            if not alias:
                continue
            _anon_groups[real] = alias
            _anon_group_reverse[alias] = real
            max_group_idx = max(max_group_idx, _alias_to_index("Group", alias))

    # 下一个编号：优先用文件里显式保存的 next；否则用“最大已用 index + 1”兼容旧文件。
    try:
        _anon_user_next = max(int(data.get("user_next", 0) or 0), max_user_idx + 1)
    except Exception:
        _anon_user_next = max_user_idx + 1
    try:
        _anon_group_next = max(int(data.get("group_next", 0) or 0), max_group_idx + 1)
    except Exception:
        _anon_group_next = max_group_idx + 1
    _anon_user_next = max(0, _anon_user_next)
    _anon_group_next = max(0, _anon_group_next)
    logger.info(
        "loaded OneBot anon map from %s (users=%d groups=%d)",
        path, len(_anon_users), len(_anon_groups),
    )


async def _save_anon_map() -> None:
    """把匿名别名映射原子写入单独文件；复用 _route_state_lock + tmp→replace。"""
    global _anon_map_dirty, _anon_map_last_saved_at
    async with _route_state_lock:
        if not _anon_map_dirty:
            return
        path = Path(ANON_MAP_FILE)
        now = time.time()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "user_next": _anon_user_next,
                "group_next": _anon_group_next,
                "users": {
                    real: {"alias": alias, "last_seen": now}
                    for real, alias in _anon_users.items()
                },
                "groups": {
                    real: {"alias": alias, "last_seen": now}
                    for real, alias in _anon_groups.items()
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            _anon_map_dirty = False
            _anon_map_last_saved_at = time.time()
        except Exception:
            logger.warning("failed to save OneBot anon map: %s", path, exc_info=True)


def _alias_to_index(prefix: str, alias: str) -> int:
    """把 UserA/GroupAB 这类别名反解为 0-based 索引；非法别名返回 -1。"""
    if not alias.startswith(prefix):
        return -1
    letters = alias[len(prefix):]
    if not letters or not letters.isalpha() or not letters.isupper():
        return -1
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _anonymize_user_id(user_id: Any) -> str:
    global _anon_user_next
    real = str(user_id or "").strip()
    if not real:
        return "UserUnknown"
    alias = _anon_users.get(real)
    if alias is None:
        alias = _alias_from_index("User", _anon_user_next)
        _anon_user_next += 1
        _anon_users[real] = alias
        _anon_user_reverse[alias] = real
        _mark_anon_map_dirty()
    return alias


def _anonymize_group_id(group_id: Any) -> str:
    global _anon_group_next
    real = str(group_id or "").strip()
    if not real:
        return "GroupUnknown"
    alias = _anon_groups.get(real)
    if alias is None:
        alias = _alias_from_index("Group", _anon_group_next)
        _anon_group_next += 1
        _anon_groups[real] = alias
        _anon_group_reverse[alias] = real
        _mark_anon_map_dirty()
    return alias


def _resolve_at_alias_to_real(token: str) -> str:
    """把模型写的 [at:xxx] 里的 xxx 反查为真实 QQ 号。

    2026-07-14 新增：模型看到的是匿名别名（UserA/UserAF）或群昵称/显示名，
    会输出 [at:UserAF] 而非真实 QQ 号。emoji_handler 在遇到非数字 token 时
    回调本函数：
      1. 先按匿名反向表 _anon_user_reverse 精确匹配别名 -> 真实 QQ 号；
      2. 再按已知用户 profile 的显示名/群名片/别名尝试匹配。
    解析不到时返回空串，由 emoji_handler 回退为可读文本。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    # 去掉可能带上的 @ 前缀。
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    # 1) 匿名别名精确反查（UserA/UserAF 等）。
    real = _anon_user_reverse.get(raw)
    if real:
        return str(real)
    # 2) 按已知用户 profile 的显示名/别名匹配（忽略大小写与首尾空白）。
    norm = raw.casefold()
    try:
        for uid in list(_QQ_USER_PROFILES.keys()):
            names = [_qq_profile_display_name(uid) or ""]
            names.extend(_profile_aliases_for_user(uid))
            for name in names:
                if name and str(name).strip().casefold() == norm:
                    return str(uid)
    except Exception:
        logger.debug("resolve at alias by profile failed for %r", raw, exc_info=True)
    return ""


def _anonymize_text_for_ai(text: str) -> str:
    """把文本中"系统已知的真实 QQ 号/群号"替换为稳定匿名别名后交给模型。

    2026-07-09 修改原因：原实现用 `_SENSITIVE_ID_RE`（5~12 位数字）兜底匿名化，
    会把金额、验证码、订单号、日期等普通数字误伤成 UserX，导致模型收到失真内容。
    做法改为"已知 ID 精确替换"：只替换 `_anon_groups` / `_anon_users` 映射表里
    真实出现过、并已在采集入口登记的群号/QQ 号；不再对任意数字串做泛匹配。
    目的是在保证"上下文里真实存在的隐私标识仍被匿名"的同时，避免误伤日常数字。
    注意：真正需要匿名的 QQ 号/群号必须在采集入口（发送者、@某人、引用消息、
    历史行等）主动调用 `_anonymize_user_id` / `_anonymize_group_id` 登记，
    这样它们才会进入映射表并在此处被替换。
    """
    if not text:
        return ""
    safe = str(text)
    for real, alias in sorted(_anon_groups.items(), key=lambda item: len(item[0]), reverse=True):
        safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
    for real, alias in sorted(_anon_users.items(), key=lambda item: len(item[0]), reverse=True):
        safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
    return safe


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


def _load_reply_attachment_cache() -> None:
    """加载独立的引用消息附件索引缓存。"""
    path = Path(REPLY_ATTACHMENT_CACHE_FILE)
    source_path = path
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load OneBot reply attachment cache: %s", path, exc_info=True)
            return
    else:
        # 兼容拆分前短暂写入 onebot_plugin_state.json 的旧字段；加载后下一次保存会
        # 写入独立 cache 文件，route state 后续保存也不会再包含 reply_attachments。
        legacy_path = Path(ONEBOT_STATE_FILE)
        if not legacy_path.exists():
            return
        try:
            legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(legacy_data, dict) or not isinstance(legacy_data.get("reply_attachments"), dict):
            return
        data = {"reply_attachments": legacy_data.get("reply_attachments")}
        source_path = legacy_path
    if not isinstance(data, dict):
        return
    payload = data.get("reply_attachments") if isinstance(data.get("reply_attachments"), dict) else data
    if not isinstance(payload, dict):
        return
    now = time.time()
    cutoff = now - max(IMAGE_CACHE_TTL_SECONDS, 60)
    loaded = 0
    for mid, item in payload.items():
        if not isinstance(item, dict):
            continue
        try:
            created_at = float(item.get("created_at") or 0.0)
        except Exception:
            created_at = 0.0
        attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
        if not attachments or (created_at and created_at < cutoff):
            continue
        _reply_attachment_cache[str(mid)] = {
            "conversation_key": str(item.get("conversation_key") or ""),
            "sender_id": str(item.get("sender_id") or ""),
            "created_at": created_at or now,
            "attachments": [dict(att) for att in attachments if isinstance(att, dict)],
        }
        loaded += 1
    _trim_reply_attachment_cache()
    if loaded:
        logger.info("loaded %d OneBot reply attachment cache entries from %s", loaded, source_path)


async def _save_reply_attachment_cache() -> None:
    """保存引用消息附件索引到独立缓存文件。"""
    async with _route_state_lock:
        _trim_reply_attachment_cache()
        path = Path(REPLY_ATTACHMENT_CACHE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ttl_seconds": IMAGE_CACHE_TTL_SECONDS,
                "reply_attachments": dict(_reply_attachment_cache),
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("failed to save OneBot reply attachment cache: %s", path, exc_info=True)


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


def _parse_direct_draw_command(user_text: str) -> Optional[str]:
    """解析 QQ 侧 /生图 直达命令；返回清理后的绘图需求，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None
    match = _DRAW_DIRECT_RE.match(text)
    if not match:
        return None
    prompt = (match.group(1) or "").strip()
    if not prompt:
        prompt = "请根据上下文生成一张合适的图片"
    return prompt


async def _build_draw_direct_inbound_text(event: Event, user_text: str, is_dm: bool) -> str:
    """构造直达绘图节点的入站文本，避免普通入口 AI 再判断一次意图。"""
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")
    name = _anonymize_text_for_ai(_sender_display_name(getattr(event, "sender", None), getattr(event, "user_id", "")))
    user = _anonymize_user_id(getattr(event, "user_id", ""))
    target = "私聊" if is_dm else "群聊"
    return "\n".join([
        f"当前时间: {now}",
        f"来源: QQ{target} / /生图 直达命令",
        "【当前用户身份】",
        *_user_identity_lines(getattr(event, "user_id", ""), name),
        "",
        "【绘图请求】",
        f"{name}（{user}）: {user_text}",
        "",
        "请直接按绘图节点规则处理：如果用户要求画图/生图则生成图片；如果用户明确只要提示词/tag则只给提示词。",
    ])


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
    entry_node_id: str = "",
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
        entry_node_id=entry_node_id or ENTRY_NODE_ID,
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
            entry_node_id=item.entry_node_id,
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
                entry_node_id=item.entry_node_id,
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


# 群成员缓存：group_id -> (过期时间, {user_id 集合}, {名片/昵称小写 -> user_id})。
# 用于（1）发送前校验 at 目标是否本群成员，避免 NapCat 对无法解析的 uid
# 抛 Get Uid Error 使整条消息失败；（2）按当前群名片/昵称实时反查真实 QQ 号，
# 让 [at:昵称] 能命中只存在于本群、未录入 profile/匿名表的成员（包括昵称叫 all 的人）。
_group_member_cache: Dict[int, tuple[float, set[str], Dict[str, str]]] = {}
_GROUP_MEMBER_CACHE_TTL = float(os.environ.get("ONEBOT_GROUP_MEMBER_CACHE_TTL", "300") or "300")


async def _load_group_members(bot: Bot, group_id: int) -> tuple[set[str], Dict[str, str]] | None:
    """拉取并缓存群成员：返回 ({user_id 集合}, {名片/昵称小写 -> user_id})。

    取不到时返回 None（表示无法校验/反查，由调用方决定不做拦截）。
    """
    try:
        gid = int(group_id)
    except Exception:
        return None
    now = time.time()
    cached = _group_member_cache.get(gid)
    if cached and cached[0] > now:
        return cached[1], cached[2]
    try:
        data = await bot.call_api("get_group_member_list", group_id=gid)
    except Exception:
        logger.debug("get_group_member_list failed for group=%s", gid, exc_info=True)
        return None
    members: set[str] = set()
    name_map: Dict[str, str] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or item.get("user_id") is None:
                continue
            uid = str(item.get("user_id"))
            members.add(uid)
            # 名片(card)优先于昵称(nickname)；同名时不覆盖已有映射（保留先遇到的）。
            for key in (item.get("card"), item.get("nickname")):
                name = str(key or "").strip()
                if name:
                    name_map.setdefault(name.casefold(), uid)
    if not members:
        return None
    _group_member_cache[gid] = (now + _GROUP_MEMBER_CACHE_TTL, members, name_map)
    return members, name_map


async def _group_member_ids(bot: Bot, group_id: int) -> set[str] | None:
    """获取群成员 QQ 号集合（带 TTL 缓存）。取不到时返回 None。"""
    loaded = await _load_group_members(bot, group_id)
    return loaded[0] if loaded else None


def _resolve_at_alias_in_group(group_id: Any, token: str) -> str:
    """在已缓存的群成员名片/昵称里反查 token 对应的真实 QQ 号（小写匹配）。

    仅使用缓存（不发网络请求）；缓存未就绪或未命中时返回空串。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = _group_member_cache.get(gid)
    if not cached:
        return ""
    return cached[2].get(raw.casefold(), "")


async def _sanitize_group_at_segments(bot: Bot, group_id: Any, message: Any) -> Any:
    """把群消息里"非本群成员/无法解析"的 at 段降级为文本，避免整条消息发送失败。

    2026-07-14 修复：一条消息 @ 多人时，只要其中一个 at 的 uid 被 NapCat 判为
    Get Uid Error，send_group_msg 就会整条失败。这里在发送前用群成员列表校验，
    命中不了的 at 用 @昵称/@别名 文本代替，@全体成员(all) 放行。
    """
    if not isinstance(message, Message):
        return message
    loaded = await _load_group_members(bot, group_id)
    if loaded is None:
        # 拿不到成员列表时不拦截，保持原样（避免误伤把所有 at 变文本）。
        return message
    members, name_map = loaded
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
            # 特例：[at:all] 但群里真有成员名片/昵称叫 "all"，优先当成 @ 那个人，
            # 避免把他误伤为 @全体成员。
            if qq.lower() == "all":
                mapped = name_map.get("all")
                if mapped:
                    logger.info("resolve [at:all] to group member %s in group=%s", mapped, group_id)
                    new_segments.append(MessageSegment.at(mapped))
                    continue
                # 确实是 @全体成员，放行。
                new_segments.append(seg)
                continue
            if qq and qq not in members:
                # 先尝试用群名片/昵称反查（适用于昵称未录入 profile/匿名表的本群成员）。
                name = _qq_profile_display_name(qq) or _anonymize_user_id(qq)
                logger.warning("downgrade at to text: qq=%s not in group=%s", qq, group_id)
                new_segments.append(MessageSegment.text(f"@{name}"))
                continue
        new_segments.append(seg)
    return Message(new_segments)


def _strip_at_to_text(message: Any) -> Any:
    """把消息里所有 at 段降级为 @文本，用于发送失败后的兜底重发。"""
    if not isinstance(message, Message):
        return message
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            data = getattr(seg, "data", {}) or {}
            qq = str(data.get("qq", "")).strip()
            if qq.lower() == "all":
                new_segments.append(MessageSegment.text("@全体成员"))
            elif qq:
                name = _qq_profile_display_name(qq) or _anonymize_user_id(qq)
                new_segments.append(MessageSegment.text(f"@{name}"))
            continue
        new_segments.append(seg)
    return Message(new_segments)


async def _send_qq_message(bot: Bot, target: Dict[str, Any], message: Any, *, dedupe: bool = True) -> None:
    """按 QQ 会话类型选择 OneBot 发送接口。"""
    # 2026-05-01 修改原因：私聊回复必须调用 send_private_msg，群聊回复继续调用
    # send_group_msg。通过 target 分发，回调发送文本和附件时不再硬编码群聊接口。
    if dedupe and _should_skip_duplicate_send(target, message):
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
        # 发送前先把非本群/无法解析的 at 降级为文本，避免 Get Uid Error 整条失败。
        safe_message = await _sanitize_group_at_segments(bot, group_id, message)
        try:
            await bot.send_group_msg(group_id=int(group_id), message=safe_message)
        except ActionFailed as exc:
            # 兜底：若仍因 at/uid 问题失败（如成员列表不准确），把所有 at 降级为文本重发一次。
            fallback = _strip_at_to_text(safe_message)
            if str(fallback) != str(safe_message):
                logger.warning("send_group_msg failed (%s); retry with at-as-text", exc)
                await bot.send_group_msg(group_id=int(group_id), message=fallback)
            else:
                raise
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


def _resolve_attachment_path(attachment: Any) -> Optional[Path]:
    """把 Clonoth 附件解析为本地路径，兼容 dict 与字符串路径。"""
    # OneBot 适配层同时接受 dict 和 str，并统一解析 file://、绝对路径和工作区相对路径
    if isinstance(attachment, dict):
        raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    elif isinstance(attachment, str):
        raw_path = attachment
    else:
        raw_path = None
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else Path(CLONOTH_WORKSPACE) / path


def _attachment_filename(attachment: Any) -> str:
    """从附件对象中提取展示文件名。"""
    if isinstance(attachment, dict):
        return str(attachment.get("name") or attachment.get("filename") or "")
    if isinstance(attachment, str):
        text = attachment[7:] if attachment.startswith("file://") else attachment
        return Path(text).name
    return ""


async def _send_attachment_path(bot: Bot, target: Dict[str, Any], path: Path, filename: str = "") -> None:
    """图片附件发送为图片消息；非图片附件通过 NapCat 文件上传 API 发送。"""
    display_name = filename or path.name
    if path.exists() and path.suffix.lower() in _IMAGE_SUFFIXES:
        file_path = str(path.resolve())
        try:
            await _send_qq_message(bot, target, MessageSegment.image(file=file_path))
            return
        except Exception as exc:
            # 部分 OneBot 实现对本地路径格式较挑剔；失败后改用 file:// URI 重试。
            # 注意首发失败时去重缓存已经记录过该图片，重试必须绕过去重，否则会被
            # 当成重复消息直接跳过，导致「生成完成但图片没发出去」。
            logger.warning("图片发送失败，改用 file:// 重试 (%s): %s", display_name, exc, exc_info=True)
            await _send_qq_message(
                bot,
                target,
                MessageSegment.image(file=f"file://{file_path}"),
                dedupe=False,
            )
            return
    # [2026-05-08] 非图片附件通过 NapCat upload_group_file / upload_private_file 发送
    # [2026-07-08] 改用 base64:// 传输文件内容，避免把宿主机绝对路径交给 NapCat：
    # NapCat 运行在 Docker 容器内，只挂载了部分目录，直接传本地路径会因容器内
    # 找不到文件而报 retcode=1200「识别URL失败」。base64 内联可彻底绕开路径/挂载问题。
    if not path.exists():
        await _send_qq_message(bot, target, f"Clonoth 生成了文件：{display_name}（文件不存在）")
        return
    try:
        try:
            raw_bytes = path.read_bytes()
            file_str = "base64://" + base64.b64encode(raw_bytes).decode("ascii")
        except Exception as read_exc:
            # 读取失败时退回本地绝对路径（例如文件已被移动/权限问题）
            logger.warning("文件读取失败，退回本地路径 (%s): %s", display_name, read_exc)
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


async def _send_attachments(bot: Bot, target: Dict[str, Any], attachments: List[Any]) -> None:
    """发送 Clonoth 返回的附件列表。"""
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        filename = _attachment_filename(attachment)
        if path is None:
            if filename:
                await _send_qq_message(bot, target, f"Clonoth 生成了文件：{filename}")
            else:
                logger.warning("skip unsupported QQ attachment payload: %r", attachment)
            continue
        await _send_attachment_path(bot, target, path, filename=filename)


def _sent_attachment_bucket_key(target: Dict[str, Any]) -> str:
    """为最近发送附件索引生成稳定的会话桶 key。

    优先用 group_id/user_id（Bot 进程内部值），避免 real/stable conversation_key
    不一致导致记录与查询对不上。
    """
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        return f"group:{int(target['group_id'])}"
    if ttype == "private" and target.get("user_id") is not None:
        return f"private:{int(target['user_id'])}"
    conv_key = str(target.get("conversation_key") or "")
    return f"conv:{conv_key}" if conv_key else ""


def _record_sent_attachments(target: Dict[str, Any], attachments: List[Any]) -> None:
    """把 Bot 发出/生成的附件记入会话级最近附件索引，供 qq_forward 后续检索。

    Why: 生图插件产出的图片文件名随机，用户说“把刚才那张图发给 xx”时
    AI 无从得知具体路径。How: 发送时把已解析的本地路径 + 显示名 + 时间戳
    按会话桶缓存。Purpose: 让后续转发能按“最近生成/发送”定位图片。
    """
    bucket = _sent_attachment_bucket_key(target)
    if not bucket or not attachments:
        return
    now = time.time()
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in _IMAGE_SUFFIXES
        _recent_sent_attachments[bucket].append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
            "timestamp": now,
        })


def _recent_sent_attachment_records(bucket_key: str, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回会话最近发出/生成的附件（旧→新），可选只要图片，并过滤不存在的文件。"""
    bucket = str(bucket_key or "")
    if not bucket:
        return []
    records: list[dict[str, Any]] = []
    for item in _recent_sent_attachments.get(bucket, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    return records


async def _send_text_and_attachments(bot: Bot, target: Dict[str, Any], text: str, attachments: List[Any]) -> None:
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
        _record_sent_attachments(target, attachments)
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
                sent = await bot.send_private_msg(user_id=int(admin_id), message=summary)
                delivered += 1
                # 记录审批消息 id -> approval_id，使管理员可直接引用该消息回复“审批同意/拒绝”。
                sent_mid = sent.get("message_id") if isinstance(sent, dict) else None
                _remember_approval_message(sent_mid, approval_id)
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


# ---------------------------------------------------------------------------
#  QQ 自然语言转发 Bridge Server
#
#  Why: 让 AI（qq.orchestrator 节点）能用自然语言“帮我把上面聊到的 xxx 私发给我 /
#  合并转发到群 xxx / 转发这张图给 xx / 提醒 xx 明天带笔记本”，并支持多选多条聊天
#  消息一次性合并转发。
#  How: qq_forward 外部工具在 Engine 子进程中运行，经本地 HTTP Bridge 调用运行在
#  QQ Bot 进程内的以下处理器；真正的 QQ 群号/QQ 号只留在 Bot 进程，模型上下文只看到
#  匿名下标/关键词，避免把副作用能力和真实目标暴露给普通模型。
# ---------------------------------------------------------------------------

_forward_bridge_runner: Any = None
_forward_bridge_site: Any = None
_forward_bridge_started: bool = False


def _forward_bridge_lookup_target(session_id: str) -> Dict[str, Any] | None:
    """在内存/持久化两张表中按单个 session_id 查找目标。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    for source in (_session_targets, _persisted_session_targets):
        target = source.get(sid)
        if isinstance(target, dict) and target:
            return dict(target)
    return None


def _forward_bridge_session_target(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> Dict[str, Any] | None:
    """根据 Engine 传来的 session_id 找回该会话对应的真实 QQ 目标。

    2026-07-14 修复原因：入口任务实际运行在 entry branch session（branch_xxx），
    ctx.session_id 为 branch，而 _session_targets / _persisted_session_targets 只按
    用户可见的 parent session 登记。若直接拿 branch 去查会 miss，导致
    op=remind + target_type=current 在群聊中误报“当前会话不是群聊”。
    做法：依次尝试 parent_session_id -> 传入 session_id -> runtime_session_id，
    任一命中即返回，对旧调用（仅传 session_id）完全兼容。
    """
    for candidate in (parent_session_id, session_id, runtime_session_id):
        target = _forward_bridge_lookup_target(candidate)
        if target:
            return target
    return None


def _forward_bridge_origin_group_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if str(target.get("type") or "") == "group" and target.get("group_id") is not None:
        try:
            return int(target.get("group_id"))
        except Exception:
            return None
    return None


def _forward_bridge_origin_user_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    """当前会话触发者 QQ 号，用于把内容“私发给我”。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if target.get("user_id") is not None:
        try:
            return int(target.get("user_id"))
        except Exception:
            return None
    return None


def _forward_bridge_list_messages(
    session_id: str,
    query: str = "",
    limit: int = 30,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出当前会话（群）最近消息，供 AI 按下标/关键词多选转发。

    返回的 index 从 1 开始；只包含匿名后可展示给模型的字段。
    """
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return []
    records = list(_group_content_records.get(int(group_id), ()))
    if not records:
        return []
    query_norm = str(query or "").strip().lower()
    limit = max(1, min(int(limit or 30), FORWARD_BRIDGE_MAX_MESSAGES))
    items: list[dict[str, Any]] = []
    for offset, record in enumerate(records):
        line = record.formatted_line or ""
        text = record.text or ""
        if query_norm and query_norm not in line.lower() and query_norm not in text.lower():
            continue
        att_kinds = sorted({str(att.get("type") or "") for att in (record.attachments or []) if isinstance(att, dict)})
        items.append({
            "index": offset + 1,
            "preview": _compact_text(line or text, limit=200),
            "sender": _anonymize_text_for_ai(record.sender_name or ""),
            "has_image": "image" in att_kinds,
            "has_file": "file" in att_kinds,
        })
    # 只回传最近 limit 条，保留时间顺序（旧→新）。
    return items[-limit:]


def _forward_bridge_origin_bucket_key(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> str:
    """把来源会话映射为“最近发送/生成附件”索引的桶 key。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return ""
    return _sent_attachment_bucket_key(target)


def _forward_bridge_list_recent(
    session_id: str,
    *,
    only_images: bool = True,
    limit: int = 20,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出本会话最近由 Bot 发出/生成的附件（如生图产出的图片），带 index 下标。"""
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = _recent_sent_attachment_records(bucket, only_images=only_images)
    limit = max(1, min(int(limit or 20), FORWARD_BRIDGE_MAX_MESSAGES))
    records = records[-limit:]
    items: list[dict[str, Any]] = []
    for offset, rec in enumerate(records):
        items.append({
            "index": offset + 1,
            "name": _sanitize_name(rec.get("name") or Path(str(rec.get("path") or "")).name, max_len=60),
            "type": str(rec.get("type") or "file"),
        })
    return items


def _forward_bridge_pick_recent_attachments(
    session_id: str,
    *,
    indices: list[int] | None,
    only_images: bool = False,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """按下标从最近发送/生成附件里挑选；无下标时默认取最新一个。

    返回可直接发送的附件 dict（带绝对 path + name + type）。
    """
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = _recent_sent_attachment_records(bucket, only_images=only_images)
    if not records:
        return []
    selected: list[dict[str, Any]]
    if indices:
        selected = []
        for raw in indices:
            try:
                idx = int(raw)
            except Exception:
                continue
            if 1 <= idx <= len(records):
                selected.append(records[idx - 1])
        if not selected:
            selected = [records[-1]]
    else:
        selected = [records[-1]]
    attachments: list[dict[str, Any]] = []
    for rec in selected:
        attachments.append({
            "type": str(rec.get("type") or "file"),
            "path": str(rec.get("path") or ""),
            "name": str(rec.get("name") or ""),
        })
    return attachments


def _forward_bridge_pick_records(
    session_id: str,
    *,
    indices: list[int] | None,
    query: str,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[GroupContentRecord]:
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return []
    records = list(_group_content_records.get(int(group_id), ()))
    if not records:
        return []
    if indices:
        picked: list[GroupContentRecord] = []
        for raw in indices:
            try:
                idx = int(raw)
            except Exception:
                continue
            if 1 <= idx <= len(records):
                picked.append(records[idx - 1])
        if picked:
            return picked
    query_norm = str(query or "").strip().lower()
    if query_norm:
        matched = [r for r in records if query_norm in (r.formatted_line or "").lower() or query_norm in (r.text or "").lower()]
        if matched:
            return matched[-FORWARD_BRIDGE_MAX_MESSAGES:]
    return records[-min(10, len(records)):]


async def _forward_bridge_resolve_target(
    bot: Bot,
    session_id: str,
    target_type: str,
    target_ref: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> tuple[ProactiveTarget | None, str]:
    """把工具给出的目标（含 self/current 语义）解析为真实 ProactiveTarget。"""
    target_type = str(target_type or "").strip().lower()
    ref = str(target_ref or "").strip()
    if target_type == "self" or ref in {"我", "自己", "me", "self"}:
        uid = _forward_bridge_origin_user_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if uid is None:
            return None, "无法确定当前用户，无法私发给你。"
        return ProactiveTarget("private", uid, _qq_profile_display_name(uid) or _anonymize_user_id(uid)), ""
    if target_type == "current" or ref in {"当前群", "本群", "这个群", "此群"}:
        gid = _forward_bridge_origin_group_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if gid is None:
            return None, "当前会话不是群聊，无法发送到本群。"
        if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS:
            return None, "当前群不在允许的主动群聊目标中。"
        return ProactiveTarget("group", gid, "当前群"), ""
    # 其余交给既有的目标解析（支持显式 id 前缀与配置的显示名/别名）。
    resolve_type = "group" if target_type == "group" else "private"
    return await _resolve_proactive_target(bot, None, resolve_type, ref)


async def _forward_bridge_records_to_nodes(
    bot: Bot,
    records: list[GroupContentRecord],
    conversation_key: str,
    *,
    include_images: bool,
    include_files: bool,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for record in records[:FORWARD_BRIDGE_MAX_MESSAGES]:
        text = record.text or record.formatted_line or ""
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            attachments = _filter_attachments_by_kind(
                record.attachments or [],
                include_images=include_images,
                include_files=include_files,
            )
        node = _make_forward_node(
            bot,
            text,
            attachments,
            nickname=record.sender_name or "转发消息",
            user_id=record.sender_id or getattr(bot, "self_id", None),
        )
        if node:
            nodes.append(node)
    return nodes


def _forward_bridge_resolve_files(raw_paths: Any, display_names: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """把工具传来的仓库相对/绝对路径解析为可发送的 file 附件。

    Why: 管理员“把仓库里的 xx 文件发出来”需要把工作区内文件发到 QQ。
    How: 复用 _attachment_path_under_workspace 限定在工作区内，防止目录穿越。
    Purpose: 只允许发送工作区内存在的普通文件，逐个回报错误。
    """
    paths: list[str] = []
    if isinstance(raw_paths, str):
        paths = [raw_paths]
    elif isinstance(raw_paths, list):
        paths = [str(item) for item in raw_paths if str(item).strip()]
    names: list[str] = []
    if isinstance(display_names, str):
        names = [display_names]
    elif isinstance(display_names, list):
        names = [str(item) for item in display_names]

    attachments: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, raw in enumerate(paths):
        raw = str(raw or "").strip()
        if not raw:
            continue
        path = _attachment_path_under_workspace(raw)
        if path is None:
            errors.append(f"{raw}：路径必须位于 Clonoth 工作区内。")
            continue
        if not path.exists() or not path.is_file():
            errors.append(f"{raw}：文件不存在。")
            continue
        rel_path = str(path)
        try:
            rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
        except Exception:
            pass
        display_name = names[idx].strip() if idx < len(names) and str(names[idx]).strip() else path.name
        attachments.append({"type": "file", "path": rel_path, "name": display_name})
    return attachments, errors


async def _forward_bridge_execute(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次自然语言转发/发送/提醒请求。仅在 Bot 进程内运行。"""
    try:
        bot = get_bot()
    except Exception:
        return {"ok": False, "error": "QQ Bot 尚未连接，无法执行转发。"}
    session_id = str(payload.get("session_id") or "").strip()
    # [2026-07-14] 入口任务运行在 branch session 上，因此工具会额外透传 parent/runtime
    # session id；这里全部收集下来供 target 解析做多级回退。
    parent_session_id = str(payload.get("parent_session_id") or "").strip()
    runtime_session_id = str(payload.get("runtime_session_id") or "").strip()
    action = str(payload.get("action") or "forward").strip().lower()
    target_type = str(payload.get("target_type") or "").strip().lower()
    target_ref = str(payload.get("target_ref") or "").strip()
    extra_text = _truncate_qq_text(str(payload.get("text") or "").strip())
    query = str(payload.get("query") or "").strip()
    include_images = bool(payload.get("include_images", True))
    include_files = bool(payload.get("include_files", True))
    raw_indices = payload.get("message_indices")
    indices: list[int] = []
    if isinstance(raw_indices, list):
        for item in raw_indices:
            try:
                indices.append(int(item))
            except Exception:
                continue
    # “最近生成/发出的图片”系列参数：use_recent 启用；recent_indices 按下标挑选。
    use_recent = bool(payload.get("use_recent", False))
    raw_recent = payload.get("recent_indices")
    recent_indices: list[int] = []
    if isinstance(raw_recent, list):
        for item in raw_recent:
            try:
                recent_indices.append(int(item))
            except Exception:
                continue
    if recent_indices:
        use_recent = True

    target, error = await _forward_bridge_resolve_target(
        bot,
        session_id,
        target_type,
        target_ref,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if target is None:
        return {"ok": False, "error": error or "目标解析失败。"}
    send_target = _target_to_send_dict(target)
    label = _target_display_label(target.target_type, target.label)
    conversation_key = f"qq_forward:{target.target_type}:{target.target_id}"

    # 纯提醒/通知：不涉及历史挑选，直接发一段文本。
    if action == "remind":
        body = extra_text
        if not body:
            return {"ok": False, "error": "提醒内容为空。"}
        await _send_text_and_attachments(bot, send_target, body, [])
        return {"ok": True, "result": f"已向{label}发送提醒。"}

    # 发送工作区文件：“把仓库里的 xx 文件发出来”。支持一次多个文件。
    if action == "file":
        attachments, file_errors = _forward_bridge_resolve_files(
            payload.get("file_paths"),
            payload.get("file_names"),
        )
        # 允许 op=file 时不给 file_paths、而是从最近生成/发出的附件里挑（use_recent）。
        if not attachments and use_recent:
            recent_atts = _forward_bridge_pick_recent_attachments(
                session_id,
                indices=recent_indices,
                only_images=False,
                parent_session_id=parent_session_id,
                runtime_session_id=runtime_session_id,
            )
            attachments = _forward_bridge_resolve_files(
                [att.get("path") for att in recent_atts],
                [att.get("name") for att in recent_atts],
            )[0]
        if not attachments:
            hint = "；".join(file_errors) if file_errors else "请在 file_paths 里给出工作区内的文件路径，或用 use_recent 发送最近生成的图片。"
            return {"ok": False, "error": f"没有可发送的文件：{hint}"}
        if extra_text:
            await _send_text_and_attachments(bot, send_target, extra_text, [])
        await _send_attachments(bot, send_target, attachments)
        names = "、".join(_sanitize_name(att.get("name") or att.get("path"), max_len=40) for att in attachments)
        result = f"已向{label}发送 {len(attachments)} 个文件：{names}"
        if file_errors:
            result += "\n（部分文件未发送：" + "；".join(file_errors) + "）"
        return {"ok": True, "result": result}

    # use_recent：把“最近生成/发出的图片”作为附件直接发送（不依赖群历史）。
    if use_recent and action in {"send", "forward"}:
        recent_atts = _forward_bridge_pick_recent_attachments(
            session_id,
            indices=recent_indices,
            only_images=False,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        recent_atts = _forward_bridge_resolve_files(
            [att.get("path") for att in recent_atts],
            [att.get("name") for att in recent_atts],
        )[0]
        if not recent_atts:
            return {"ok": False, "error": "没有找到最近生成/发送的图片。可先用 op=recent 查看可选图片。"}
        await _send_text_and_attachments(bot, send_target, extra_text, recent_atts)
        return {"ok": True, "result": f"已向{label}发送 {len(recent_atts)} 张最近生成/发送的图片。"}

    records = _forward_bridge_pick_records(
        session_id,
        indices=indices,
        query=query,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )

    if action == "send":
        # 直接把挑选到的消息拼成一段文本 + 附件发送（非合并转发卡片）。
        lines = [r.formatted_line or r.text for r in records if (r.formatted_line or r.text)]
        body = "\n".join(part for part in ([extra_text] if extra_text else []) + lines).strip()
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            for r in records:
                attachments.extend(_filter_attachments_by_kind(
                    r.attachments or [],
                    include_images=include_images,
                    include_files=include_files,
                ))
        if not body and not attachments:
            return {"ok": False, "error": "没有可发送的内容。"}
        await _send_text_and_attachments(bot, send_target, _truncate_qq_text(body), attachments)
        return {"ok": True, "result": f"已发送到{label}（{len(records)} 条消息）。"}

    # 默认 action == "forward"：合并转发卡片，支持多选多条消息。
    nodes = await _forward_bridge_records_to_nodes(
        bot,
        records,
        conversation_key,
        include_images=include_images,
        include_files=include_files,
    )
    if extra_text:
        head = _make_forward_node(bot, extra_text, None, nickname="Clonoth 通知")
        if head:
            nodes.insert(0, head)
    if not nodes:
        return {"ok": False, "error": "没有可转发的消息。请先用 list 查看上文消息并给出 message_indices 或 query。"}
    try:
        await _send_forward_nodes(bot, target, nodes)
    except Exception as exc:
        logger.warning("qq_forward bridge send forward failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"}
    return {"ok": True, "result": f"已向{label}发送合并转发（{len(nodes)} 条）。"}


def _forward_bridge_check_token(request: "Any") -> bool:
    if not FORWARD_BRIDGE_TOKEN:
        return True
    return request.headers.get("X-Forward-Token", "") == FORWARD_BRIDGE_TOKEN


async def _forward_bridge_http_handler(request: "Any") -> "Any":
    from aiohttp import web  # 局部导入，未启用 Bridge 时不强依赖 aiohttp。

    if not _forward_bridge_check_token(request):
        return web.json_response({"ok": False, "error": "invalid token"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)
    op = str(payload.get("op") or "").strip().lower()
    try:
        _parent_sid = str(payload.get("parent_session_id") or "")
        _runtime_sid = str(payload.get("runtime_session_id") or "")
        if op == "list":
            messages = _forward_bridge_list_messages(
                str(payload.get("session_id") or ""),
                query=str(payload.get("query") or ""),
                limit=int(payload.get("limit") or FORWARD_BRIDGE_MAX_MESSAGES),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "messages": messages})
        if op == "recent":
            recent = _forward_bridge_list_recent(
                str(payload.get("session_id") or ""),
                only_images=bool(payload.get("only_images", True)),
                limit=int(payload.get("limit") or 20),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "recent": recent})
        if op in {"forward", "send", "remind", "file", ""}:
            if op:
                payload.setdefault("action", op)
            result = await _forward_bridge_execute(payload)
            status = 200 if result.get("ok") else 400
            return web.json_response(result, status=status)
        return web.json_response({"ok": False, "error": f"unknown op: {op}"}, status=400)
    except Exception as exc:
        logger.warning("qq_forward bridge handler error: %s", exc, exc_info=True)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _start_forward_bridge() -> None:
    """在 QQ Bot 进程内启动 qq_forward Bridge Server。"""
    global _forward_bridge_runner, _forward_bridge_site, _forward_bridge_started
    if _forward_bridge_started or not ENABLE_FORWARD_BRIDGE:
        return
    try:
        from aiohttp import web
    except Exception:
        logger.warning("qq_forward bridge disabled: aiohttp 未安装。")
        return
    app = web.Application()
    app.router.add_post("/qq_forward", _forward_bridge_http_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, FORWARD_BRIDGE_HOST, FORWARD_BRIDGE_PORT)
    await site.start()
    _forward_bridge_runner = runner
    _forward_bridge_site = site
    _forward_bridge_started = True
    logger.info(
        "qq_forward bridge server started: http://%s:%s/qq_forward (token=%s)",
        FORWARD_BRIDGE_HOST,
        FORWARD_BRIDGE_PORT,
        "set" if FORWARD_BRIDGE_TOKEN else "none",
    )


async def _stop_forward_bridge() -> None:
    global _forward_bridge_runner, _forward_bridge_site, _forward_bridge_started
    if _forward_bridge_runner is not None:
        with contextlib.suppress(Exception):
            await _forward_bridge_runner.cleanup()
    _forward_bridge_runner = None
    _forward_bridge_site = None
    _forward_bridge_started = False


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
    _load_reply_attachment_cache()
    _load_anon_map()
    # [2026-07-14] 注入 at 别名反查，让 emoji_handler 在处理 [at:UserAF]/[at:显示名]
    # 时能把匿名别名/群昵称回解为真实 QQ 号，避免直接把代号当纯文本 @ 出去。
    set_at_alias_resolver(_resolve_at_alias_to_real)
    # [AutoC] QQ 管理员 /切换模型 命令需要调用受 admin_token 保护的
    # POST /v1/config/openai，因此这里把 Supervisor 写出的 data/.admin_token
    # 传给 ClonothClient（每次请求实时读取，token 轮换也能跟上）。
    _client = ClonothClient(
        CLONOTH_BASE_URL,
        admin_token=os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip(),
        admin_token_path=str(Path(CLONOTH_WORKSPACE) / "data" / ".admin_token"),
    )
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
        # [AutoC] QQ 私聊会话键前缀为 qq_private，需一并声明归属，
        # 否则私聊触发的 approval_requested 会因前缀不匹配被 SDK 丢弃，
        # 导致管理员收不到审批、任务空等到超时。
        extra_conversation_key_prefixes=["qq_private"],
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
    with contextlib.suppress(Exception):
        await _start_forward_bridge()
    logger.info("Clonoth Agent QQ adapter started: %s", CLONOTH_BASE_URL)


@driver.on_shutdown
async def _shutdown() -> None:
    """NoneBot 关闭时停止事件路由并释放 HTTP 连接。"""
    global _client, _event_router, _router_task, _qq_queue_tasks, _callbacks, _anon_map_save_task
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
    await _stop_forward_bridge()
    # 关闭前先取消可能在睡等间隔的节流 flush task，再同步 flush 一次，
    # 避免延迟写盘 task 未到点就退出导致最新别名丢失。
    if _anon_map_save_task is not None and not _anon_map_save_task.done():
        _anon_map_save_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _anon_map_save_task
    _anon_map_save_task = None
    with contextlib.suppress(Exception):
        await _save_anon_map()
    logger.info("Clonoth Agent QQ adapter stopped")


# 普通群消息记录器。priority 较低且 block=False，只负责维护上下文缓存。
_history_matcher = on_message(rule=Rule(_allowed_group_rule), priority=99, block=False)


@_history_matcher.handle()
async def _handle_history(bot: Bot, event: GroupMessageEvent) -> None:
    """记录非触发消息，供后续 @Bot 请求作为群聊上下文。"""
    _remember_message_for_reply_context(event)
    if str(event.user_id) != str(getattr(bot, "self_id", "")):
        expanded_text = await _message_to_text_with_forward(bot, event.get_message(), getattr(bot, "self_id", None))
        # QQ 客户端经常把“问图文本 + 图片”拆成两条事件；第二条纯图片
        # 通常不会 @bot，因此不会进入 agent matcher。这里在低优先级历史
        # matcher 中把非触发附件也下载并写入近期缓存，让后续自然语言转发
        # 和图片提问能选到对应内容。
        real_conversation_key = f"qq_group:{int(event.group_id)}"
        stable_conversation_key = _stable_conversation_key(real_conversation_key)
        attachments, _errors = await _collect_qq_attachments(event, stable_conversation_key)
        _remember_recent_images(stable_conversation_key, event, attachments)
        _record_group_message(event, bot, override_text=expanded_text, attachments=attachments)


# 群文件上传通知记录器。部分 OneBot 实现把普通文件作为 notice 上报，而不是 message file 段。
_group_upload_matcher = on_notice(priority=99, block=False)


@_group_upload_matcher.handle()
async def _handle_group_upload_notice(bot: Bot, event: Event) -> None:
    if not isinstance(event, GroupUploadNoticeEvent):
        return
    if not _is_group_allowed(int(event.group_id)):
        return
    _remember_message_for_reply_context(event)
    real_conversation_key = f"qq_group:{int(event.group_id)}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    file_info = getattr(event, "file", None)
    if not isinstance(file_info, dict):
        file_info = {}
    source = str(file_info.get("url") or file_info.get("path") or file_info.get("file") or "").strip()
    name = _safe_attachment_name(str(file_info.get("name") or file_info.get("file_name") or file_info.get("filename") or "文件"), "file")
    size = file_info.get("size") or file_info.get("file_size") or file_info.get("filesize") or 0
    attachments, _errors = await _file_sources_to_attachments([
        {"source": source, "name": name, "size": size},
    ], stable_conversation_key)
    display_text = f"[文件:{name}]"
    sender_id = str(getattr(event, "user_id", "") or "")
    name_raw = _sender_display_name(getattr(event, "sender", None), sender_id)
    line = f"[{_format_hhmm(getattr(event, 'time', None))}] {_anonymize_text_for_ai(name_raw)}({_anonymize_user_id(sender_id)}): {display_text}"
    _group_history[int(event.group_id)].append(line)
    _group_content_records[int(event.group_id)].append(GroupContentRecord(
        formatted_line=line,
        text=display_text,
        sender_name=name_raw,
        sender_id=sender_id,
        timestamp=float(getattr(event, "time", None) or time.time()),
        message_id=str(getattr(event, "message_id", "") or ""),
        attachments=attachments,
    ))


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
    asyncio.create_task(_auto_like_user(bot, int(event.user_id)))

    attachments, attachment_errors = await _collect_qq_attachments(event, stable_conversation_key)
    _remember_recent_images(stable_conversation_key, event, attachments)
    clear_mem_reply = await _maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _agent_matcher.finish(clear_mem_reply)
    model_reply = await _maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _agent_matcher.finish(model_reply)
    drawtools_reply = await _maybe_handle_drawtools_command(user_text)
    if drawtools_reply is not None:
        await _agent_matcher.finish(drawtools_reply)
    custom_face_reply = await _maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _agent_matcher.finish(custom_face_reply)
    proactive_reply = await _maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _agent_matcher.finish(proactive_reply)
    await _merge_recent_images_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    _record_group_message(event, bot, override_text=user_text, attachments=attachments)
    draw_direct_prompt = _parse_direct_draw_command(user_text)
    entry_node_id = DRAW_NODE_ID if draw_direct_prompt is not None else ""
    inbound_text = await _build_draw_direct_inbound_text(event, draw_direct_prompt, False) if draw_direct_prompt is not None else await _build_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
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
            entry_node_id=entry_node_id,
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


async def _finish_approval_decision(user_id: int, approval_id: str, decision: str) -> None:
    """提交审批决策并结束当前私聊处理（末尾总会抛出 FinishedException）。

    同时服务于“引用回复审批”和“手输审批命令”两个入口，避免重复代码。
    """
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
        await _private_matcher.finish(
            f"已{('同意' if decision == 'allow' else '拒绝')}审批：{approval_id}\n操作：{operation}"
        )
    await _private_matcher.finish("审批提交被 Supervisor 拒绝，请检查日志。")


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

    # 快捷审批：管理员引用(回复)审批消息并发“同意/拒绝”即可，无需携带 approval_id。
    reply_mid = _extract_reply_message_id(event.get_message(), getattr(event, "raw_message", None))
    reply_approval_id = _resolve_approval_id_by_reply(reply_mid) if reply_mid is not None else None
    if reply_approval_id is not None:
        reply_verb = _parse_approval_reply_verb(user_text)
        if reply_verb is not None:
            if not _is_admin_user(user_id):
                await _private_matcher.finish("你不是 Clonoth 审批管理员，不能处理审批请求。")
            await _finish_approval_decision(user_id, reply_approval_id, reply_verb)

    approval_command = _parse_approval_command(user_text)
    if approval_command is not None:
        if not _is_admin_user(user_id):
            await _private_matcher.finish("你不是 Clonoth 审批管理员，不能处理审批请求。")
        decision, approval_token = approval_command
        approval_id, error = _resolve_pending_approval_id(approval_token)
        if not approval_id:
            await _private_matcher.finish(error)
        await _finish_approval_decision(user_id, approval_id, decision)

    # 管理员私聊清除群记忆：无参数列出可清理群，带参数按群名/群号清除。
    # 放在 _is_private_allowed 之前，使管理员即便私聊未整体放通也能使用该命令。
    clear_mem_reply = await _maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _private_matcher.finish(clear_mem_reply)
    model_reply = await _maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _private_matcher.finish(model_reply)

    if not _is_private_allowed(event):
        await _private_matcher.finish("当前 QQ 私聊未被允许接入 Clonoth。")

    real_conversation_key = f"qq_private:{user_id}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    attachments, attachment_errors = await _collect_qq_attachments(event, stable_conversation_key)
    _remember_recent_images(stable_conversation_key, event, attachments)
    drawtools_reply = await _maybe_handle_drawtools_command(user_text)
    if drawtools_reply is not None:
        await _private_matcher.finish(drawtools_reply)
    custom_face_reply = await _maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _private_matcher.finish(custom_face_reply)
    proactive_reply = await _maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _private_matcher.finish(proactive_reply)
    await _merge_recent_images_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    draw_direct_prompt = _parse_direct_draw_command(user_text)
    entry_node_id = DRAW_NODE_ID if draw_direct_prompt is not None else ""
    inbound_text = await _build_draw_direct_inbound_text(event, draw_direct_prompt, True) if draw_direct_prompt is not None else await _build_private_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
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
            entry_node_id=entry_node_id,
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
