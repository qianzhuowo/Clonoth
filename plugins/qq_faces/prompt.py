"""QQ 收藏表情可用列表：每次请求时临时注入，不写进聊天记录。

Why: 列表以前拼进每条 QQ 入站消息并随会话持久化；改名/删除表情后旧列表仍留在
历史里，模型会沿用已不存在的名称，发送时只能退化。How: 注册为 engine 的
dynamic prompt section，每次 LLM 调用从名称文件实时读取，只对 QQ 适配器提交的
任务（task_context.route_hints.platform == "qq"）渲染；dispatch 子任务不继承
route_hints，因此不会拿到列表。Purpose: 模型永远只看到当前名称。

名称文件由 QQ Bot 进程（adapters/onebot）的“同步表情列表”等命令写入；路径与
数量上限沿用同一组环境变量（ONEBOT_CUSTOM_FACE_NAMES_PATH /
ONEBOT_CUSTOM_FACE_PROMPT_LIMIT），默认 <workspace>/config/qq_custom_faces.txt。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SECTION_NAME = "qq_custom_faces"
_DEFAULT_LIMIT = 200


def names_path(workspace_root: Path) -> Path:
    raw = (os.environ.get("ONEBOT_CUSTOM_FACE_NAMES_PATH") or "").strip()
    if raw:
        return Path(raw)
    workspace = (os.environ.get("CLONOTH_WORKSPACE") or "").strip()
    return Path(workspace or workspace_root) / "config" / "qq_custom_faces.txt"


def prompt_limit() -> int:
    try:
        value = int((os.environ.get("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT") or "").strip() or _DEFAULT_LIMIT)
    except ValueError:
        value = _DEFAULT_LIMIT
    return max(0, min(_DEFAULT_LIMIT, value))


def load_names(path: Path) -> list[str]:
    """一行一个名称；# 开头为注释；按去空白后的小写去重。"""
    try:
        content = path.read_text("utf-8")
    except OSError:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        key = "".join(item.split()).lower()
        if key not in seen:
            seen.add(key)
            names.append(item)
    return names


def build_block(names: list[str], limit: int) -> str:
    if limit <= 0 or not names:
        return ""
    shown = names[:limit]
    more = "" if len(names) <= limit else f"（另有 {len(names) - limit} 个未展示）"
    return (
        "【QQ可用收藏表情】\n"
        "你可以在回复中用 [表情:名称] 发送 QQ 收藏表情，把“名称”换成下列某一项。"
        "只使用下列名称；聊天记录里出现过但不在下列的名称已失效，不要使用，也不要臆造。\n"
        f"可用名称：{'、'.join(shown)}{more}"
    )


def is_qq_task(task_context: dict[str, Any] | None) -> bool:
    hints = (task_context or {}).get("route_hints")
    return isinstance(hints, dict) and str(hints.get("platform") or "").strip().lower() == "qq"


def render(sctx: Any) -> str | None:
    if not is_qq_task(getattr(sctx, "task_context", None)):
        return None
    block = build_block(load_names(names_path(Path(sctx.workspace_root))), prompt_limit())
    return block or None
