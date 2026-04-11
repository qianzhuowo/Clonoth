from __future__ import annotations

import locale
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .node import Node

DEFAULT_PROMPT = "你是一个 AI 节点。请根据指令完成处理。\n"

_INCLUDE_RE = re.compile(r'\{\{include:([^}]+)\}\}')


def _expand_includes(text: str, nodes_dir: Path, *, depth: int = 0) -> str:
    """展开 {{include:filename}} 指令，读取 config/nodes/ 下的文件并内联。

    最多递归 3 层，防止循环引用。
    """
    if depth > 3:
        return text

    def _repl(m: re.Match) -> str:
        filename = m.group(1).strip()
        if not filename:
            return m.group(0)
        fp = (nodes_dir / filename).resolve()
        # 安全检查：不能逃逸出 nodes_dir
        try:
            fp.relative_to(nodes_dir.resolve())
        except ValueError:
            return m.group(0)
        try:
            content = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return m.group(0)
        # 递归展开被包含文件中的 include
        return _expand_includes(content, nodes_dir, depth=depth + 1)

    return _INCLUDE_RE.sub(_repl, text)


def _render_variables(text: str, variables: dict[str, str]) -> str:
    """替换 {{key}} 占位符。未匹配的保留原样。"""
    def _repl(m: re.Match) -> str:
        key = m.group(1).strip()
        return variables.get(key, m.group(0))
    return re.sub(r'\{\{(\w+)\}\}', _repl, text)


def assemble_prompt(
    workspace_root: Path,
    node: Node,
    *,
    variables: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """渲染节点的 prompt 模板，返回多条 system 消息。

    返回 list[{"role": "system", "content": str}]。
    第一条为静态内容（跨 turn 稳定，利于 prompt cache），
    第二条为动态内容（含 {{now}}、{{instruction}} 等每 turn 变化的内容）。

    处理顺序：
    1. 展开 {{include:filename}}（从 config/nodes/ 目录读取）
    2. 替换 {{变量}} 占位符
    3. 按 `# %%DYNAMIC%%` 标记拆分静态/动态段
    """
    raw = node.prompt.strip() if node.prompt else ""
    if not raw:
        return [{"role": "system", "content": DEFAULT_PROMPT}]

    # 第一步：展开 include
    nodes_dir = workspace_root / "config" / "nodes"
    expanded = _expand_includes(raw, nodes_dir)

    # 第二步：变量替换
    merged: dict[str, str] = {}
    if variables:
        merged.update(variables)
    merged.setdefault("node_id", node.id)
    merged.setdefault("node_name", node.name)
    merged.setdefault("now", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    merged.setdefault("default_language", "zh-CN")
    merged.setdefault("workspace_root", str(workspace_root.resolve()))
    merged.setdefault("os_name", platform.system())
    merged.setdefault("os_version", platform.version())
    merged.setdefault("timezone", str(datetime.now().astimezone().tzinfo or "UTC"))
    merged.setdefault("user_language", locale.getdefaultlocale()[0] or "en_US")

    rendered = _render_variables(expanded, merged)

    # 第三步：拆分静态/动态段
    # 节点 prompt 中可用 `# %%DYNAMIC%%` 标记分隔，标记之前为静态段，之后为动态段
    marker = "# %%DYNAMIC%%"
    if marker in rendered:
        idx = rendered.index(marker)
        static_part = rendered[:idx].strip()
        dynamic_part = rendered[idx + len(marker):].strip()
        msgs: list[dict[str, Any]] = []
        if static_part:
            msgs.append({"role": "system", "content": static_part})
        if dynamic_part:
            msgs.append({"role": "system", "content": dynamic_part})
        return msgs if msgs else [{"role": "system", "content": rendered}]

    # 没有标记，整体作为一条 system 消息
    return [{"role": "system", "content": rendered}]
