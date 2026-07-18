"""Clonoth OneBot 11 适配器配置。

所有敏感/实例特定的参数通过环境变量注入，避免硬编码。
"""
from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    """解析布尔环境变量，兼容 onebot11_adapter.py 的 ONEBOT_* 配置风格。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.environ.get(name)
    try:
        value = float(str(raw).strip()) if raw is not None else float(default)
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_first(*names: str, default: str = "") -> str:
    """按优先级读取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _parse_qq_id_list(raw: str) -> list[int]:
    """解析逗号分隔 QQ 号/群号列表；忽略占位符和非法项。"""
    result: list[int] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if not token:
            continue
        try:
            result.append(int(token))
        except ValueError:
            # 允许配置文件保留 [占位符] 这类说明性文本；非法项不应导致 Bot 启动失败。
            continue
    return result


# Clonoth Supervisor API 地址
CLONOTH_BASE_URL = _env_first("CLONOTH_BASE_URL", "CLONOTH_SUPERVISOR_URL", default="http://127.0.0.1:8765")

# Clonoth 工作区根目录（用于 clonoth_sdk 导入和附件路径解析）
CLONOTH_WORKSPACE = _env_first("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT", default="/www/wwwroot/Clonoth")

# 入口节点 ID。默认使用 QQ 综合入口，兼顾联网搜索、调度、重启和取消任务。
# 如需搜索-only 安全窄入口，可显式设置 CLONOTH_ENTRY_NODE=qq.web_search。
ENTRY_NODE_ID = _env_first("CLONOTH_ENTRY_NODE", "ONEBOT_ENTRY_NODE_ID", default="qq.orchestrator")

# QQ 收藏表情 AI 可见名称文件路径。默认使用该文件给 AI 注入可用表情名。
CUSTOM_FACE_NAMES_PATH = _env_first(
    "ONEBOT_CUSTOM_FACE_NAMES_PATH",
    "CLONOTH_QQ_CUSTOM_FACES_PATH",
    default=os.path.join(CLONOTH_WORKSPACE, "config", "qq_custom_faces.txt"),
)
CUSTOM_FACE_METADATA_PATH = _env_first(
    "ONEBOT_CUSTOM_FACE_METADATA_PATH",
    "CLONOTH_QQ_CUSTOM_FACES_METADATA_PATH",
    default=os.path.join(CLONOTH_WORKSPACE, "config", "qq_custom_faces.json"),
)
CUSTOM_FACE_PROMPT_LIMIT = _env_int("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT", 50, min_value=0, max_value=200)

# 旧 bqbs.txt 顺序别名文件路径（可选）。默认不使用；仅配置 env 时参与兼容匹配/同步。
BQBS_PATH = _env_first("ONEBOT_CUSTOM_EMOJI_INDEX_PATH", "CLONOTH_BQBS_PATH", default="")

# QQ 用户身份/称呼配置文件路径（可选，支持 JSON/YAML）。只用于模型可见称呼，不授予权限。
# 默认读取 Clonoth 工作区 config/qq_user_profiles.yaml；文件不存在时静默跳过。
USER_PROFILES_PATH = _env_first(
    "CLONOTH_QQ_USER_PROFILES_PATH",
    default=os.path.join(CLONOTH_WORKSPACE, "config", "qq_user_profiles.yaml"),
)

# 群聊触发模式：mention_only（默认，只 @Bot）、prefix（@Bot 或前缀）、all（所有消息）。
GROUP_TRIGGER = _env_first("ONEBOT_GROUP_TRIGGER", default="mention_only").lower()
TRIGGER_PREFIXES = tuple(p for p in os.environ.get("ONEBOT_TRIGGER_PREFIXES", "!,！,/，/").split(",") if p)

# QQ 输出与上下文限制。
GROUP_HISTORY_MAX = _env_int("ONEBOT_GROUP_HISTORY_MAX", 20, min_value=0)
HISTORY_TEXT_LIMIT = _env_int("ONEBOT_HISTORY_TEXT_LIMIT", 400, min_value=50)
QQ_MESSAGE_LIMIT = _env_int("ONEBOT_QQ_MESSAGE_LIMIT", 4300, min_value=500)

# QQ 图片/多模态输入配置。
ENABLE_IMAGE_INPUT = _env_bool("ONEBOT_ENABLE_IMAGE_INPUT", True)
IMAGE_MAX_BYTES = _env_int("ONEBOT_IMAGE_MAX_BYTES", 10 * 1024 * 1024, min_value=1024)
IMAGE_DOWNLOAD_TIMEOUT = _env_float("ONEBOT_IMAGE_DOWNLOAD_TIMEOUT", 15.0, min_value=1.0)
IMAGE_CACHE_TTL_SECONDS = _env_int("ONEBOT_IMAGE_CACHE_TTL_SECONDS", 24 * 3600, min_value=60)
RECENT_IMAGE_MAX_ITEMS = _env_int("ONEBOT_RECENT_IMAGE_MAX_ITEMS", 20, min_value=1, max_value=200)
RECENT_IMAGE_MAX_AGE_SECONDS = _env_float("ONEBOT_RECENT_IMAGE_MAX_AGE_SECONDS", 60.0, min_value=1.0)
IMAGE_WAIT_AFTER_TEXT_SECONDS = _env_float("ONEBOT_IMAGE_WAIT_AFTER_TEXT_SECONDS", 2.5, min_value=0.0, max_value=10.0)
IMAGE_PREFER_SAME_SENDER = _env_bool("ONEBOT_IMAGE_PREFER_SAME_SENDER", True)
MAX_IMAGES_PER_TURN = _env_int("ONEBOT_MAX_IMAGES_PER_TURN", 4, min_value=1, max_value=16)

# [2026-07-17] 多图合并转发（forward node）发送：
# NapCat/NTQQ 逐张 send_group_msg 发大图时，每张都要 base64 上传并等 NTQQ ack。
# [2026-07-18] 实测结论：合并转发反而更慢，且会阻塞其它任务——
# 所有 QQ 事件走同一条反向 WebSocket 并在 SDK 里串行 await，而
# send_group_forward_msg 把 4 张大图一次交给 NapCat 处理需几十秒，
# 期间这条 WS 上的其它 call_api（发消息/React）全部排队，整个 bot 卡死。
# 因此默认关闭，回到“逐张直发”（逐张已通过 sendMsg 超时容错与单张
# 容错解决了重复发/丢图问题）。如需重新启用可设 ONEBOT_ENABLE_IMAGE_FORWARD_MERGE=1。
ENABLE_IMAGE_FORWARD_MERGE = _env_bool("ONEBOT_ENABLE_IMAGE_FORWARD_MERGE", False)
# 至少多少张图才使用合并转发（少于此数继续逐张直发，保留“图片直接出现在聊天”的观感）。
IMAGE_FORWARD_MERGE_THRESHOLD = _env_int("ONEBOT_IMAGE_FORWARD_MERGE_THRESHOLD", 2, min_value=2, max_value=16)
# 合并转发单个 node 的署名。
IMAGE_FORWARD_MERGE_NICKNAME = _env_first("ONEBOT_IMAGE_FORWARD_MERGE_NICKNAME", default="Clonoth")

# QQ 文件/附件输入配置。用于管理员自然语言转发“这文件/上面的文件”等请求。
ENABLE_FILE_INPUT = _env_bool("ONEBOT_ENABLE_FILE_INPUT", True)
FILE_MAX_BYTES = _env_int("ONEBOT_FILE_MAX_BYTES", 50 * 1024 * 1024, min_value=1024)
MAX_FILES_PER_TURN = _env_int("ONEBOT_MAX_FILES_PER_TURN", 3, min_value=1, max_value=16)

# OneBot 扩展功能开关。
ENABLE_REACTIONS = _env_bool("ONEBOT_ENABLE_REACTIONS", True)
REPLY_TO_TRIGGER = _env_bool("ONEBOT_REPLY_TO_TRIGGER", True)
ENABLE_FORWARD_MSG_INPUT = _env_bool("ONEBOT_ENABLE_FORWARD_MSG_INPUT", True)
FORWARD_MSG_MAX_DEPTH = _env_int("ONEBOT_FORWARD_MSG_MAX_DEPTH", 3, min_value=0, max_value=8)
FORWARD_MSG_MAX_MESSAGES = _env_int("ONEBOT_FORWARD_MSG_MAX_MESSAGES", 80, min_value=1, max_value=500)
FORWARD_MSG_TEXT_LIMIT = _env_int("ONEBOT_FORWARD_MSG_TEXT_LIMIT", 12000, min_value=500, max_value=100000)
ENABLE_QQ_QUEUE = _env_bool("ONEBOT_ENABLE_QQ_QUEUE", False)
QQ_QUEUE_INTERVAL = max(0.0, float(os.environ.get("ONEBOT_QQ_QUEUE_INTERVAL", "2.0")))
QQ_QUEUE_REPLY_TIMEOUT = max(1.0, float(os.environ.get("ONEBOT_QQ_QUEUE_REPLY_TIMEOUT", "120.0")))
# [QQ parallel 2026-06-21] 多 worker + 默认不等待回复，避免单个慢/僵死任务阻塞 QQ 聊天。
QQ_QUEUE_WORKERS = max(1, min(32, int(os.environ.get("ONEBOT_QQ_QUEUE_WORKERS", "4"))))
QQ_QUEUE_WAIT_FOR_REPLY = _env_bool("ONEBOT_QQ_QUEUE_WAIT_FOR_REPLY", False)
# [QQ parallel 2026-06-21] preempt 改为显式开启，默认新消息创建新 inbound 并行处理。
ENABLE_PREEMPT = _env_bool("ONEBOT_ENABLE_PREEMPT", False)
ENABLE_AUTO_LIKE = _env_bool("ONEBOT_ENABLE_AUTO_LIKE", False)
AUTO_LIKE_TIMES = max(1, min(20, int(os.environ.get("ONEBOT_AUTO_LIKE_TIMES", "10"))))

# QQ 自然语言转发 Bridge Server 配置。
# AI 通过 qq_forward 工具（子进程）经本地 HTTP Bridge 调用 QQ Bot 进程完成
# “把上面聊到的 xxx 私发给我 / 合并转发到群 xxx”等多选多条消息转发任务。
# 真实 QQ 群号/QQ 号只留在 Bot 进程内，模型上下文只接触匿名下标/关键词。
ENABLE_FORWARD_BRIDGE = _env_bool("ONEBOT_ENABLE_FORWARD_BRIDGE", True)
FORWARD_BRIDGE_HOST = _env_first("ONEBOT_FORWARD_BRIDGE_HOST", default="127.0.0.1")
FORWARD_BRIDGE_PORT = _env_int("ONEBOT_FORWARD_BRIDGE_PORT", 8769, min_value=1, max_value=65535)
# Bridge 共享令牌，防止本机其他进程随意调用转发能力。工具通过环境变量拿到同一令牌。
FORWARD_BRIDGE_TOKEN = os.environ.get("ONEBOT_FORWARD_BRIDGE_TOKEN", "").strip()
# 单次转发允许挑选的最大消息条数，避免刷屏或超出 OneBot 限制。
FORWARD_BRIDGE_MAX_MESSAGES = _env_int("ONEBOT_FORWARD_BRIDGE_MAX_MESSAGES", 30, min_value=1, max_value=200)

# 提交给 Supervisor 的 QQ conversation_key 使用稳定哈希，真实群号/QQ 号只保留在插件本地路由里。
CONVERSATION_HASH_SECRET = os.environ.get("ONEBOT_CONVERSATION_HASH_SECRET", "").strip()

# 本地路由状态文件：保存 stable conversation_key/session_id 到真实 QQ 群/用户目标的映射。
ONEBOT_STATE_FILE = _env_first(
    "ONEBOT_STATE_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_plugin_state.json"),
)

# 引用消息附件索引缓存：保存 message_id -> 已落盘图片附件路径，用于 get_msg 失败时兜底转发。
# 与路由状态分离，避免 onebot_plugin_state.json 被临时缓存污染。
REPLY_ATTACHMENT_CACHE_FILE = _env_first(
    "ONEBOT_REPLY_ATTACHMENT_CACHE_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "cache", "onebot_reply_attachments.json"),
)

# 匿名别名映射持久化文件：保存 真实 QQ 号/群号 <-> UserX/GroupX 别名 的双向映射。
# 目的：跨重启保持别名一致，避免历史/记忆里同一人出现不同别名，并支持别名反解。
# 注意：这是一份“去匿名化字典”（明文对照真实号 <-> 别名），属于敏感文件，
# 必须与 data/config.yaml 同级别保护、不得提交到版本库。与路由状态分离存放。
ANON_MAP_FILE = _env_first(
    "ONEBOT_ANON_MAP_FILE",
    default=os.path.join(CLONOTH_WORKSPACE, "data", "onebot_anon_map.json"),
)

# QQ 审批管理员白名单。只有这些 QQ 用户能批准/拒绝 Clonoth 审批请求。
_raw_admin_users = _env_first("CLONOTH_ADMIN_QQ_USERS", "ONEBOT_ADMIN_USERS", default="[占位符],[占位符]")
ADMIN_QQ_USERS: list[int] = _parse_qq_id_list(_raw_admin_users)

# 允许接入的群号列表（逗号分隔）。默认使用占位符且不会匹配任何真实群，避免空配置时开放所有群。
_raw_groups = _env_first("CLONOTH_ALLOWED_GROUPS", "ONEBOT_ALLOWED_GROUPS", default="[占位符]")
ALLOWED_GROUPS: list[int] = _parse_qq_id_list(_raw_groups)

# 私聊允许列表。默认策略为“只允许已通过好友请求的用户私聊”；也可额外填写 QQ 号白名单。
_raw_private_users = _env_first("CLONOTH_ALLOWED_PRIVATE_USERS", "ONEBOT_ALLOWED_PRIVATE_USERS", default="[私聊只允许已经通过好友请求的人]")
ALLOWED_PRIVATE_USERS: list[int] = _parse_qq_id_list(_raw_private_users)
ALLOW_PRIVATE_FRIENDS: bool = (
    not ALLOWED_PRIVATE_USERS
    or "好友" in _raw_private_users
    or "friend" in _raw_private_users.lower()
)
