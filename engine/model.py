from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .node import Node


@dataclass
class ResolvedProvider:
    model: str
    # [provider-registry 2026-05-03] provider_type 是 ProviderRegistry 的 key。
    # 原因：provider 名称已插件化；做法：这里只保存字符串，不列死内置集合；目的：保持模型解析层与具体 provider 解耦。
    provider_type: str = "openai"
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认


def resolve_provider(workspace_root: Path, node: Node, provider_default: str) -> ResolvedProvider:
    """根据节点配置解析模型和可选的独立 api_key/base_url。"""
    model = node.model.strip() if node.model else ""
    if not model:
        model = provider_default or "gpt-4o-mini"

    api_key = node.api_key.strip() if node.api_key else None
    base_url = node.base_url.strip() if node.base_url else None

    # 从 node.provider 获取 ProviderRegistry key，空字符串时默认 "openai"
    provider_type = (node.provider.strip() if node.provider else "") or "openai"

    return ResolvedProvider(
        model=model,
        provider_type=provider_type,
        api_key=api_key or None,
        base_url=base_url or None,
    )
