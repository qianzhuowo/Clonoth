"""Shared mutable state and service singletons of the QQ adapter."""
from __future__ import annotations

from .config import FORWARD_MSG_MAX_DEPTH
from .config import FORWARD_MSG_MAX_MESSAGES
from .config import FORWARD_MSG_TEXT_LIMIT
from .config import GROUP_HISTORY_MAX
from .config import ONEBOT_IDEMPOTENCY_MAX_ITEMS
from .config import ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS
from .config import ONEBOT_IDEMPOTENCY_STORE_FILE
from .config import QQ_MESSAGE_LIMIT
from .config import RECENT_IMAGE_MAX_ITEMS
from .send_contract import TwoPhaseIdempotencyStore
from clonoth_sdk import ClonothClient
from clonoth_sdk import EventRouter
from clonoth_sdk import SessionState
from collections import OrderedDict
from collections import defaultdict
from collections import deque
from dataclasses import dataclass
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11 import Event
from typing import Any
from typing import DefaultDict
from typing import Deque
from typing import Dict
from typing import List
from typing import Optional
import asyncio
import datetime as dt
import os
import re

from .anon import AnonMap
from .approval import ApprovalBook
from .custom_faces import CustomFaces
from .inbound_attachments import RecentImages, ReplyAttachmentCache
from .message_parse import ForwardExpander, ReplyMessageCache, get_forward_messages
from .profiles import UserProfiles

# ---- service singletons of the split modules --------------------------------
ANON = AnonMap()
PROFILES = UserProfiles(anon_map=ANON)
BOOK = ApprovalBook()
REPLY_CACHE = ReplyAttachmentCache()
RECENT = RecentImages(REPLY_CACHE)
REPLY_MSGS = ReplyMessageCache()
FACES = CustomFaces()


def at_name(qq: Any) -> str:
    """Model-visible name for an @-mentioned QQ id; never the raw id."""
    return PROFILES.display_name(qq) or ANON.anonymize_user_id(qq)


def forward_expander(bot: Any) -> ForwardExpander:
    """Forward-message expander bound to one bot connection."""
    return ForwardExpander(lambda forward_id: get_forward_messages(bot, forward_id), at_name, PROFILES.display_name)


_CST = dt.timezone(dt.timedelta(hours=8))


_SPLIT_SIGNAL = "[SPLIT]"


_HISTORY_MAX_LEN = GROUP_HISTORY_MAX


_QQ_MESSAGE_LIMIT = QQ_MESSAGE_LIMIT


_FORWARD_MSG_MAX_DEPTH = FORWARD_MSG_MAX_DEPTH


_FORWARD_MSG_MAX_MESSAGES = FORWARD_MSG_MAX_MESSAGES


_FORWARD_MSG_TEXT_LIMIT = FORWARD_MSG_TEXT_LIMIT


_QQ_EMOJI_MARK_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")


_AT_QQ_RE = re.compile(r"\[(?:CQ:)?at[,:][^\]]*?qq=([^,\]\s]+)", re.IGNORECASE)


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


# [2026-09-17] “还在联网搜索中”最多发几条。日志证实投递失败后 trigger 挂到 10 分钟
# 超时才被清理，期间每 20 秒刷一条共 27 条；这里硬限上限，避免刷屏。
_SEARCH_PROGRESS_STILL_RUNNING_MAX_COUNT = int(
    os.environ.get("ONEBOT_SEARCH_PROGRESS_MAX_STILL_NOTICES", "3") or "3"
)


# [2026-09-17] 最终回复进死信（QQ 侧未确认送达且不可重试）时给用户的一句提示。
# 措辞兼顾“其实已送达”的歧义超时场景，不断言一定没发出去。
_DELIVERY_DEAD_LETTER_NOTICE = "刚才的回复 QQ 侧没有确认送达，如果你没收到，请重新问我一次。"


# [2026-07-22] 图片 sendMsg 超时后，延迟多久再用 base64:// 补发一次。
# 给 NapCat/NTQQ 一点时间把“已提交但未 ack”的首发真正落地，降低补发双发概率。
_IMAGE_TIMEOUT_RESEND_DELAY_SEC = float(os.environ.get("ONEBOT_IMAGE_TIMEOUT_RESEND_DELAY_SEC", "3") or "3")


_SEARCH_PROGRESS_KEYWORDS = ("web_search", "exa_search", "x_search")


# 群聊上下文只保留最近 N 条。这样可以给入口节点提供社交语境，
# 同时避免每次 inbound 发送过长历史。
_group_history: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=_HISTORY_MAX_LEN))


# EventRouter 回调只拿到 session/trigger，因此这里保存发送最终回复所需的平台对象。
_session_targets: Dict[str, Dict[str, Any]] = {}


_conversation_bots: Dict[str, Bot] = {}


_persisted_session_targets: Dict[str, Dict[str, Any]] = {}


_last_bot: Optional[Bot] = None


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


_group_content_records: DefaultDict[int, Deque[GroupContentRecord]] = defaultdict(lambda: deque(maxlen=max(_HISTORY_MAX_LEN, 20)))


# [2026-07-07] 按会话记录 Bot 最近发出/生成的附件（如生图插件产出的图片），
# 供 qq_forward “把刚才生成的那张图发给 xx”等自然语言请求检索。
# 只保存工作区内本地路径与显示名，不涉及真实 QQ 号。
_recent_sent_attachments: DefaultDict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max(RECENT_IMAGE_MAX_ITEMS, 20)))


# [2026-07-17] Bot 已发出附件消息的 message_id -> 本地附件路径列表 映射。
# Why: Bot 发出的图片（如绘图产物）在上下文里被替换成占位符/表情包，用户引用
# 这条消息要求“基于这张图继续改”时，AI 看不到原图。How: 发图时接住 send_*_msg
# 返回的 message_id，登记它对应的本地附件路径；用户引用命中时把该图当附件送给 AI。
# Purpose: 让“引用 Bot 生成的图 + 追加要求”能真正带上原图，而普通表情包（无此
# 映射）仍保持屏蔽，不会重新触发识图。
_MESSAGE_ATTACHMENT_MAX_ITEMS = int(os.environ.get("ONEBOT_MSG_ATTACHMENT_CACHE_ITEMS", "512") or "512")


_sent_message_attachments: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()


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


# Outbound replay protection uses pending/sent phases: failed sends release pending,
# while only acknowledged sends become sent.
_outbound_idempotency = TwoPhaseIdempotencyStore(
    ONEBOT_IDEMPOTENCY_STORE_FILE,
    sent_ttl=ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS,
    max_items=ONEBOT_IDEMPOTENCY_MAX_ITEMS,
)


_route_state_lock = asyncio.Lock()


_client: Optional[ClonothClient] = None


_session_state: Optional[SessionState] = None


_event_router: Optional[EventRouter] = None


_router_task: Optional[asyncio.Task] = None


_qq_queue_tasks: List[asyncio.Task] = []


_callbacks: Any = None  # callbacks.TangQiuCallbacks, set on startup


_PROACTIVE_PRIVATE_KINDS = {"私聊", "好友", "private", "pm", "user"}


_PROACTIVE_GROUP_KINDS = {"群聊", "群", "group"}


# 命令别名：群聊里直接清当前群；私聊里列可清理群名或按群名清指定群。
_CLEAR_GROUP_MEMORY_RE = re.compile(
    r"^[/／!！]?\s*(?:清除群记忆|清空群记忆|清理群记忆|清除群聊记忆|清空群聊记忆)\s*(.*)$"
)


# 群成员缓存：group_id -> (过期时间, {user_id 集合}, {名片/昵称小写 -> user_id},
#                         {user_id -> (card, nickname)})。
# 用于（1）发送前校验 at 目标是否本群成员，避免 NapCat 对无法解析的 uid
# 抛 Get Uid Error 使整条消息失败；（2）按当前群名片/昵称实时反查真实 QQ 号，
# 让 [at:昵称] 能命中只存在于本群、未录入 profile/匿名表的成员（包括昵称叫 all 的人）；
# （3）2026-07-17 新增 uid -> (card, nickname) 正向映射，让 @ 群友时的显示名
# 优先取“本群群名片(card)”，其次“QQ 昵称(nickname)”，避免直接用 QQ 昵称。
_group_member_cache: Dict[int, tuple[float, set[str], Dict[str, str], Dict[str, tuple[str, str]]]] = {}


_GROUP_MEMBER_CACHE_TTL = float(os.environ.get("ONEBOT_GROUP_MEMBER_CACHE_TTL", "300") or "300")


# 表情后紧跟的“收尾标点”：属于前一段正文的句末/右括号，不应单独成为下一条气泡的开头。
_EMOJI_TRAILING_PUNCT_RE = re.compile(
    r"^\s*([，。！？、；：,.!?;:）)\]】」』》〉}”’\"'~～…—]+)"
)


_NAPCAT_TEMP_ENOENT_RE = re.compile(r"ENOENT[^'\"]*['\"]([^'\"]*/NapCat/temp/[^'\"]*)['\"]", re.IGNORECASE)


_NAPCAT_AT_UID_ERROR_RE = re.compile(
    r"(get\s*uid\s*error|\buid\b|\buin\b|\bat\b|@|艾特|not\s+(?:a\s+)?(?:group\s+)?member|群成员)",
    re.IGNORECASE,
)


# Attachment delivery remains ordered per conversation, but the callback awaits
# this lock and the final platform acknowledgement before returning to the SDK.
_attachment_send_locks: Dict[str, asyncio.Lock] = {}


_forward_bridge_runner: Any = None


_forward_bridge_site: Any = None


_forward_bridge_started: bool = False
