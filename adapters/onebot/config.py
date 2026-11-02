"""Clonoth OneBot 11 适配器配置。

所有敏感/实例特定的参数通过环境变量注入，避免硬编码。
"""
from __future__ import annotations

import os
from pathlib import Path

# Clonoth Supervisor API 地址
CLONOTH_BASE_URL = os.environ.get("CLONOTH_BASE_URL", "http://127.0.0.1:8765")

# Clonoth 工作区根目录（用于 clonoth_sdk 导入和附件路径解析）
CLONOTH_WORKSPACE = os.environ.get("CLONOTH_WORKSPACE", str(Path(__file__).resolve().parents[2]))

# 入口节点 ID
ENTRY_NODE_ID = os.environ.get("CLONOTH_ENTRY_NODE", "main")

# QQ 自定义表情名称索引文件路径（可选）
BQBS_PATH = os.environ.get("CLONOTH_BQBS_PATH", "")

def _parse_qq_id_list(raw: str) -> list[int]:
    """Parse a comma separated QQ/group id list; non-numeric items are ignored."""
    result: list[int] = []
    for item in (raw or "").split(","):
        token = item.strip()
        if token.isdigit():
            result.append(int(token))
    return result


# 允许接入的群号列表（逗号分隔）。空值表示不允许任何群（白名单制）。
_raw_groups = os.environ.get("CLONOTH_ALLOWED_GROUPS", "")
ALLOWED_GROUPS: list[int] = _parse_qq_id_list(_raw_groups)

# QQ 审批管理员：只有这些用户能批准/拒绝审批；为空时所有审批直接拒绝。
ADMIN_QQ_USERS: list[int] = _parse_qq_id_list(os.environ.get("CLONOTH_ADMIN_QQ_USERS", ""))

# 私聊白名单。为空、或写了“好友/friend”时，已通过好友请求的用户也可私聊。
_raw_private_users = os.environ.get("CLONOTH_ALLOWED_PRIVATE_USERS", "")
ALLOWED_PRIVATE_USERS: list[int] = _parse_qq_id_list(_raw_private_users)
ALLOW_PRIVATE_FRIENDS: bool = (
    not ALLOWED_PRIVATE_USERS
    or "好友" in _raw_private_users
    or "friend" in _raw_private_users.lower()
)

# 提交给 Supervisor 的会话键用 HMAC 稳定哈希，真实群号/QQ 号只留在适配器本地。
CONVERSATION_HASH_SECRET = os.environ.get("ONEBOT_CONVERSATION_HASH_SECRET", "").strip()

# 匿名映射（真实号 <-> UserX/GroupX）是去匿名化字典，属于敏感文件，不得入库。
ANON_MAP_FILE = os.environ.get(
    "ONEBOT_ANON_MAP_FILE", os.path.join(CLONOTH_WORKSPACE, "data", "onebot_anon_map.json")
)
ANON_MAP_SAVE_MIN_INTERVAL = max(0.0, float(os.environ.get("ONEBOT_ANON_MAP_SAVE_MIN_INTERVAL", "5.0") or "5.0"))

# 单条 QQ 消息长度上限。
QQ_MESSAGE_LIMIT = max(500, int(os.environ.get("ONEBOT_QQ_MESSAGE_LIMIT", "4300") or "4300"))

# QQ 最高权限用户列表（逗号分隔）。这些用户会在 inbound 文本中标记为最高权限管理员。
_raw_superusers = os.environ.get("CLONOTH_QQ_SUPERUSERS", "")
QQ_SUPERUSERS: set[int] = {int(u) for u in _raw_superusers.split(",") if u.strip().isdigit()}

# 项目所有者标识，用于在 QQ 场景中给模型提供稳定身份说明。
QQ_OWNER_ID = os.environ.get("CLONOTH_QQ_OWNER_ID", "")
QQ_OWNER_NAME = os.environ.get("CLONOTH_QQ_OWNER_NAME", "")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# QQ 语音输入与语音回复配置。
ONEBOT_VOICE_INPUT = _env_bool("CLONOTH_ONEBOT_VOICE_INPUT", True)
ONEBOT_VOICE_REPLY = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY", True)
ONEBOT_VOICE_REPLY_ON_VOICE_INPUT = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_ON_VOICE_INPUT", True)
ONEBOT_VOICE_REPLY_AUTO_ON_TEXT = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_AUTO_ON_TEXT", True)
ONEBOT_VOICE_REPLY_PRIVATE = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_PRIVATE", True)
ONEBOT_VOICE_REPLY_GROUP = _env_bool("CLONOTH_ONEBOT_VOICE_REPLY_GROUP", True)
ONEBOT_DELETE_AUDIO_AFTER_USE = _env_bool("CLONOTH_ONEBOT_DELETE_AUDIO_AFTER_USE", True)
ONEBOT_TTS_FORMAT = os.environ.get("CLONOTH_ONEBOT_TTS_FORMAT", "wav")
ONEBOT_TTS_VOICE = os.environ.get("CLONOTH_ONEBOT_TTS_VOICE", "")
ONEBOT_TTS_INSTRUCTION = os.environ.get("CLONOTH_ONEBOT_TTS_INSTRUCTION", "")
