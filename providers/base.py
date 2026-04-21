from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResponse:
    """A rust-like provider result.

    - ok=True  -> text/tool_calls are valid
    - ok=False -> error contains a human-readable error message

    Worker loops should NOT rely on exceptions for normal provider failures
    (timeout, 4xx/5xx, invalid payload...).
    """

    ok: bool
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # [refactor 2026-04-18] thinking → reasoning：统一命名，保留思维链内容
    reasoning: str | None = None
    error: str | None = None
    status_code: int | None = None
    raw: dict[str, Any] | None = None
    usage: dict[str, int] | None = None  # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    # [refactor 2026-04-18] 新增：provider 解析出的内联附件（图片等）
    inline_data: list[dict] = field(default_factory=list)
    # [refactor 2026-04-18] 新增：provider 私有元数据，engine 只搬运不解读
    provider_meta: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    # [fix 2026-04-18] 新增 name 参数：让 engine 能动态获取 provider 名称，
    # 不再硬编码 "openai"。各子类在 super().__init__ 时传入自身名称。
    def __init__(self, *, model: str, name: str = ""):
        self.model = model
        self.name = name

    @abstractmethod
    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ProviderResponse:
        raise NotImplementedError
