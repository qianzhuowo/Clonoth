from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .node import Node


@dataclass
class ResolvedProvider:
    model: str
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认


def resolve_provider(workspace_root: Path, node: Node, provider_default: str) -> ResolvedProvider:
    """根据节点配置解析模型和可选的独立 api_key/base_url。"""
    model = node.model.strip() if node.model else ""
    if not model:
        model = provider_default or "gpt-4o-mini"

    api_key = node.api_key.strip() if node.api_key else None
    base_url = node.base_url.strip() if node.base_url else None

    return ResolvedProvider(
        model=model,
        api_key=api_key or None,
        base_url=base_url or None,
    )
