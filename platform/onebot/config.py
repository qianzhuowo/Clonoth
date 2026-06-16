"""Clonoth OneBot 11 适配器配置。

所有敏感/实例特定的参数通过环境变量注入，避免硬编码。
"""
from __future__ import annotations

import os


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
CLONOTH_BASE_URL = os.environ.get("CLONOTH_BASE_URL", "http://127.0.0.1:8765")

# Clonoth 工作区根目录（用于 clonoth_sdk 导入和附件路径解析）
CLONOTH_WORKSPACE = os.environ.get("CLONOTH_WORKSPACE", "/www/wwwroot/Clonoth")

# 入口节点 ID
ENTRY_NODE_ID = os.environ.get("CLONOTH_ENTRY_NODE", "main")

# QQ 自定义表情名称索引文件路径（可选）
BQBS_PATH = os.environ.get("CLONOTH_BQBS_PATH", "")

# QQ 审批管理员白名单。只有这些 QQ 用户能批准/拒绝 Clonoth 审批请求。
_raw_admin_users = os.environ.get("CLONOTH_ADMIN_QQ_USERS", "[占位符],[占位符]")
ADMIN_QQ_USERS: list[int] = _parse_qq_id_list(_raw_admin_users)

# 允许接入的群号列表（逗号分隔）。默认使用占位符且不会匹配任何真实群，避免空配置时开放所有群。
_raw_groups = os.environ.get("CLONOTH_ALLOWED_GROUPS", "[占位符]")
ALLOWED_GROUPS: list[int] = _parse_qq_id_list(_raw_groups)

# 私聊允许列表。默认策略为“只允许已通过好友请求的用户私聊”；也可额外填写 QQ 号白名单。
_raw_private_users = os.environ.get("CLONOTH_ALLOWED_PRIVATE_USERS", "[私聊只允许已经通过好友请求的人]")
ALLOWED_PRIVATE_USERS: list[int] = _parse_qq_id_list(_raw_private_users)
ALLOW_PRIVATE_FRIENDS: bool = (
    not ALLOWED_PRIVATE_USERS
    or "好友" in _raw_private_users
    or "friend" in _raw_private_users.lower()
)
