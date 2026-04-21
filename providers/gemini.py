from __future__ import annotations

from typing import Any

from .base import BaseProvider, ProviderResponse


class GeminiProvider(BaseProvider):
    # [fix 2026-04-19] 补传 name="gemini"，修复桩 provider 未传 name 的警告
    def __init__(self, *, model: str, **kw: Any):
        super().__init__(model=model, name="gemini", **kw)

    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ProviderResponse:
        return ProviderResponse(ok=False, error="Gemini provider not implemented yet")
