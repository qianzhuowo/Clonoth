"""Clonoth OneBot 11 适配器配置。

所有敏感/实例特定的参数通过环境变量注入，避免硬编码。
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        value = float(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() else default


def _parse_qq_id_list(raw: str) -> list[int]:
    """Parse a comma separated QQ/group id list; non-numeric items are ignored."""
    return [int(token) for token in (item.strip() for item in (raw or "").split(",")) if token.isdigit()]


# Clonoth Supervisor API 地址
CLONOTH_BASE_URL = _env_str("CLONOTH_BASE_URL", "http://127.0.0.1:8765")

# Clonoth 工作区根目录（用于 clonoth_sdk 导入和附件路径解析）
CLONOTH_WORKSPACE = _env_str("CLONOTH_WORKSPACE", str(Path(__file__).resolve().parents[2]))

# 入口节点 ID：QQ 综合入口。
ENTRY_NODE_ID = _env_str("CLONOTH_ENTRY_NODE", "qq.orchestrator")

# ---- 权限 -------------------------------------------------------------------
# 允许接入的群号列表（逗号分隔）。空值表示不允许任何群（白名单制）。
ALLOWED_GROUPS: list[int] = _parse_qq_id_list(os.environ.get("CLONOTH_ALLOWED_GROUPS", ""))

# QQ 审批管理员：只有这些用户能批准/拒绝审批、使用管理命令；为空时所有审批直接拒绝。
ADMIN_QQ_USERS: list[int] = _parse_qq_id_list(os.environ.get("CLONOTH_ADMIN_QQ_USERS", ""))

# 私聊白名单。为空、或写了“好友/friend”时，已通过好友请求的用户也可私聊。
_raw_private_users = os.environ.get("CLONOTH_ALLOWED_PRIVATE_USERS", "")
ALLOWED_PRIVATE_USERS: list[int] = _parse_qq_id_list(_raw_private_users)
ALLOW_PRIVATE_FRIENDS: bool = (
    not ALLOWED_PRIVATE_USERS
    or "好友" in _raw_private_users
    or "friend" in _raw_private_users.lower()
)

# 项目所有者标识，用于在 QQ 场景中给模型提供稳定身份说明。
QQ_OWNER_ID = _env_str("CLONOTH_QQ_OWNER_ID")
QQ_OWNER_NAME = _env_str("CLONOTH_QQ_OWNER_NAME")

# QQ 用户称呼 Profile（只影响称呼与身份说明，不授予权限）；含真实 QQ 号，不入库。
USER_PROFILES_PATH = _env_str(
    "CLONOTH_QQ_USER_PROFILES_PATH", os.path.join(CLONOTH_WORKSPACE, "config", "qq_user_profiles.yaml")
)

# ---- 收藏表情 -----------------------------------------------------------------
# AI 可见名称文件（只含基础名）与内部元数据（md5/resId/emojiId）。均不入库。
CUSTOM_FACE_NAMES_PATH = _env_str(
    "ONEBOT_CUSTOM_FACE_NAMES_PATH", os.path.join(CLONOTH_WORKSPACE, "config", "qq_custom_faces.txt")
)
CUSTOM_FACE_METADATA_PATH = _env_str(
    "ONEBOT_CUSTOM_FACE_METADATA_PATH", os.path.join(CLONOTH_WORKSPACE, "config", "qq_custom_faces.json")
)
CUSTOM_FACE_PROMPT_LIMIT = _env_int("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT", 200, min_value=0, max_value=200)
# 旧 bqbs.txt 顺序别名文件（可选）。生产环境用 ONEBOT_CUSTOM_EMOJI_INDEX_PATH，新部署用 CLONOTH_BQBS_PATH。
BQBS_PATH = _env_str("ONEBOT_CUSTOM_EMOJI_INDEX_PATH") or _env_str("CLONOTH_BQBS_PATH")

# ---- 触发与上下文 -------------------------------------------------------------
# 群聊触发模式：mention_only（默认，只 @Bot）、prefix（@Bot 或前缀）、all（所有消息）。
GROUP_TRIGGER = _env_str("ONEBOT_GROUP_TRIGGER", "mention_only").lower()
TRIGGER_PREFIXES = tuple(p for p in os.environ.get("ONEBOT_TRIGGER_PREFIXES", "!,！,/，/").split(",") if p)

GROUP_HISTORY_MAX = _env_int("ONEBOT_GROUP_HISTORY_MAX", 20, min_value=0)
HISTORY_TEXT_LIMIT = _env_int("ONEBOT_HISTORY_TEXT_LIMIT", 400, min_value=50)
QQ_MESSAGE_LIMIT = _env_int("ONEBOT_QQ_MESSAGE_LIMIT", 4300, min_value=500)

# 合并转发展开：递归深度、总条数与总字数上限。
ENABLE_FORWARD_MSG_INPUT = _env_bool("ONEBOT_ENABLE_FORWARD_MSG_INPUT", True)
FORWARD_MSG_MAX_DEPTH = _env_int("ONEBOT_FORWARD_MSG_MAX_DEPTH", 3, min_value=0, max_value=8)
FORWARD_MSG_MAX_MESSAGES = _env_int("ONEBOT_FORWARD_MSG_MAX_MESSAGES", 80, min_value=1, max_value=500)
FORWARD_MSG_TEXT_LIMIT = _env_int("ONEBOT_FORWARD_MSG_TEXT_LIMIT", 12000, min_value=500, max_value=100000)

# ---- 入站图片/文件 ------------------------------------------------------------
# 下载到 data/attachments/<稳定会话键>/，由 engine/data_cleanup.py 统一过期清理。
ENABLE_IMAGE_INPUT = _env_bool("ONEBOT_ENABLE_IMAGE_INPUT", True)
ENABLE_FILE_INPUT = _env_bool("ONEBOT_ENABLE_FILE_INPUT", True)
IMAGE_MAX_BYTES = _env_int("ONEBOT_IMAGE_MAX_BYTES", 10 * 1024 * 1024, min_value=1024)
FILE_MAX_BYTES = _env_int("ONEBOT_FILE_MAX_BYTES", 50 * 1024 * 1024, min_value=1024)
IMAGE_DOWNLOAD_TIMEOUT = _env_float("ONEBOT_IMAGE_DOWNLOAD_TIMEOUT", 15.0, min_value=1.0)
IMAGE_CACHE_TTL_SECONDS = _env_int("ONEBOT_IMAGE_CACHE_TTL_SECONDS", 24 * 3600, min_value=60)
MAX_IMAGES_PER_TURN = _env_int("ONEBOT_MAX_IMAGES_PER_TURN", 4, min_value=1, max_value=16)
MAX_FILES_PER_TURN = _env_int("ONEBOT_MAX_FILES_PER_TURN", 3, min_value=1, max_value=16)
# “再看看图”这类追问：只取同一发送者最近一条消息里的图片。
RECENT_IMAGE_MAX_ITEMS = _env_int("ONEBOT_RECENT_IMAGE_MAX_ITEMS", 20, min_value=1, max_value=200)
RECENT_IMAGE_MAX_AGE_SECONDS = _env_float("ONEBOT_RECENT_IMAGE_MAX_AGE_SECONDS", 60.0, min_value=1.0)
# 引用消息附件索引（message_id -> 已落盘图片），get_msg 失败时兜底。
REPLY_ATTACHMENT_CACHE_FILE = _env_str(
    "ONEBOT_REPLY_ATTACHMENT_CACHE_FILE",
    os.path.join(CLONOTH_WORKSPACE, "data", "cache", "onebot_reply_attachments.json"),
)

# ---- 出站 ---------------------------------------------------------------------
# 多图合并转发：NapCat 逐张发送更稳且不阻塞同一条 WS，默认关闭。
ENABLE_IMAGE_FORWARD_MERGE = _env_bool("ONEBOT_ENABLE_IMAGE_FORWARD_MERGE", False)
IMAGE_FORWARD_MERGE_THRESHOLD = _env_int("ONEBOT_IMAGE_FORWARD_MERGE_THRESHOLD", 2, min_value=2, max_value=16)
IMAGE_FORWARD_MERGE_NICKNAME = _env_str("ONEBOT_IMAGE_FORWARD_MERGE_NICKNAME", "Clonoth")

# Markdown 样式剥离：默认剥离 *斜体* / **粗体** 的星号，不把下划线当 Markdown（保留 user_id 等原文）。
STRIP_MARKDOWN_ASTERISK_STYLES = _env_bool("ONEBOT_STRIP_MARKDOWN_ASTERISK_STYLES", True)
STRIP_MARKDOWN_UNDERSCORE_STYLES = _env_bool("ONEBOT_STRIP_MARKDOWN_UNDERSCORE_STYLES", False)

ENABLE_REACTIONS = _env_bool("ONEBOT_ENABLE_REACTIONS", True)
REPLY_TO_TRIGGER = _env_bool("ONEBOT_REPLY_TO_TRIGGER", True)

# ---- 排队与抢占 ---------------------------------------------------------------
ENABLE_QQ_QUEUE = _env_bool("ONEBOT_ENABLE_QQ_QUEUE", False)
QQ_QUEUE_INTERVAL = _env_float("ONEBOT_QQ_QUEUE_INTERVAL", 2.0, min_value=0.0)
QQ_QUEUE_REPLY_TIMEOUT = _env_float("ONEBOT_QQ_QUEUE_REPLY_TIMEOUT", 120.0, min_value=1.0)
# 多 worker + 默认不等待回复，避免单个慢任务阻塞 QQ 聊天。
QQ_QUEUE_WORKERS = _env_int("ONEBOT_QQ_QUEUE_WORKERS", 4, min_value=1, max_value=32)
QQ_QUEUE_WAIT_FOR_REPLY = _env_bool("ONEBOT_QQ_QUEUE_WAIT_FOR_REPLY", False)
# 抢占需显式开启；默认新消息创建新 inbound 并行处理。
ENABLE_PREEMPT = _env_bool("ONEBOT_ENABLE_PREEMPT", False)
ENABLE_AUTO_LIKE = _env_bool("ONEBOT_ENABLE_AUTO_LIKE", False)
AUTO_LIKE_TIMES = _env_int("ONEBOT_AUTO_LIKE_TIMES", 10, min_value=1, max_value=20)

# ---- 自然语言转发 Bridge ------------------------------------------------------
# AI 通过 qq_forward 工具经本地 HTTP Bridge 调用 Bot 进程转发消息；真实群号/QQ 号只留在 Bot 进程内。
ENABLE_FORWARD_BRIDGE = _env_bool("ONEBOT_ENABLE_FORWARD_BRIDGE", True)
FORWARD_BRIDGE_HOST = _env_str("ONEBOT_FORWARD_BRIDGE_HOST", "127.0.0.1")
FORWARD_BRIDGE_PORT = _env_int("ONEBOT_FORWARD_BRIDGE_PORT", 8769, min_value=1, max_value=65535)
# Bridge 共享令牌，防止本机其他进程随意调用转发能力。
FORWARD_BRIDGE_TOKEN = _env_str("ONEBOT_FORWARD_BRIDGE_TOKEN")
FORWARD_BRIDGE_MAX_MESSAGES = _env_int("ONEBOT_FORWARD_BRIDGE_MAX_MESSAGES", 30, min_value=1, max_value=200)

# ---- 匿名化与本地状态 ---------------------------------------------------------
# 提交给 Supervisor 的会话键用 HMAC 稳定哈希，真实群号/QQ 号只留在适配器本地。
CONVERSATION_HASH_SECRET = _env_str("ONEBOT_CONVERSATION_HASH_SECRET")
# 匿名映射（真实号 <-> UserX/GroupX）是去匿名化字典，属于敏感文件，不得入库。
ANON_MAP_FILE = _env_str("ONEBOT_ANON_MAP_FILE", os.path.join(CLONOTH_WORKSPACE, "data", "onebot_anon_map.json"))
ANON_MAP_SAVE_MIN_INTERVAL = _env_float("ONEBOT_ANON_MAP_SAVE_MIN_INTERVAL", 5.0, min_value=0.0)
# 路由状态：稳定会话键/session_id -> 真实 QQ 群/用户目标。
ONEBOT_STATE_FILE = _env_str("ONEBOT_STATE_FILE", os.path.join(CLONOTH_WORKSPACE, "data", "onebot_plugin_state.json"))
# 出站幂等：pending/sent 记录需跨 SDK 重放与适配器重启保留。
ONEBOT_IDEMPOTENCY_STORE_FILE = _env_str(
    "ONEBOT_IDEMPOTENCY_STORE_FILE", os.path.join(CLONOTH_WORKSPACE, "data", "onebot_outbound_idempotency.sqlite3")
)
ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS = _env_float("ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS", 7 * 24 * 3600.0, min_value=300.0)
ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS = _env_float(
    "ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS", 600.0, min_value=30.0
)
ONEBOT_IDEMPOTENCY_MAX_ITEMS = _env_int("ONEBOT_IDEMPOTENCY_MAX_ITEMS", 50_000, min_value=100, max_value=2_000_000)

# ---- 语音输入与语音回复 -------------------------------------------------------
ONEBOT_VOICE_INPUT = _env_bool("CLONOTH_ONEBOT_VOICE_INPUT", True)
ONEBOT_VOICE_REPLY = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY", False)
ONEBOT_VOICE_REPLY_ON_VOICE_INPUT = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_ON_VOICE_INPUT", True)
ONEBOT_VOICE_REPLY_AUTO_ON_TEXT = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_AUTO_ON_TEXT", True)
ONEBOT_VOICE_REPLY_PRIVATE = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_PRIVATE", True)
ONEBOT_VOICE_REPLY_GROUP = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_GROUP", True)
ONEBOT_DELETE_AUDIO_AFTER_USE = _env_bool("CLONOTH_ONEBOT_DELETE_AUDIO_AFTER_USE", True)
ONEBOT_TTS_FORMAT = _env_str("CLONOTH_ONEBOT_TTS_FORMAT", "wav")
ONEBOT_TTS_VOICE = _env_str("CLONOTH_ONEBOT_TTS_VOICE")
ONEBOT_TTS_INSTRUCTION = _env_str("CLONOTH_ONEBOT_TTS_INSTRUCTION")
