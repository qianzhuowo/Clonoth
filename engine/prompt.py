from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

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


def assemble_prompt(workspace_root: Path, node: Node, *, variables: dict[str, str] | None = None) -> str:
    """渲染节点的 prompt 模板。

    处理顺序：
    1. 展开 {{include:filename}}（从 config/nodes/ 目录读取）
    2. 替换 {{变量}} 占位符
    """
    raw = node.prompt.strip() if node.prompt else ""
    if not raw:
        return DEFAULT_PROMPT

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

    return _render_variables(expanded, merged)
