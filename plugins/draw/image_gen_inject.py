"""draw.image_gen 生图渠道可用性注入。

把节点 prompt 中的占位符 ``<<IMAGE_TOOLS_AVAILABILITY>>`` 替换成“哪个生图工具可用 /
哪个未配置不可用”的说明。渠道判定与门控共用 channels.image_channel_availability。
（未配置渠道的工具本身已被门控从工具列表移除，这里负责让提示词与之保持一致。）
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from engine.hooks.types import HookResult

from .channels import IMAGE_GEN_NODE_ID, image_channel_availability

logger = logging.getLogger(__name__)

PLACEHOLDER = "<<IMAGE_TOOLS_AVAILABILITY>>"


def build_availability_text(gpt_ok: bool, gemini_ok: bool) -> str:
    lines = ["【当前可用的生图渠道】"]
    if gpt_ok:
        lines.append("- gpt_image_2：【可用】写实/照片/图片编辑/精确分辨率生图。")
    else:
        lines.append("- gpt_image_2：【未配置·不可用】禁止调用，也不要向用户提及。")
    if gemini_ok:
        lines.append("- gemini_image：【可用】二次元/插画/风格化/按宽高比生图。")
    else:
        lines.append("- gemini_image：【未配置·不可用】禁止调用，也不要向用户提及。")
    if not gpt_ok and not gemini_ok:
        lines.append(
            "\n当前**没有任何生图渠道被配置**。不要调用任何生图工具，直接用 finish "
            "如实告诉用户：生图功能尚未配置，需要在环境变量或工作区 .env 中配置 "
            "CLONOTH_IMAGE_GPT_MODEL/BASE_URL/API_KEY 或 "
            "CLONOTH_IMAGE_GEMINI_MODEL/BASE_URL/API_KEY。"
        )
    return "\n".join(lines)


class ImageGenAvailabilityInjector:
    """before_prompt_build：在 draw.image_gen 替换渠道可用性占位符。"""

    name = "draw.image_gen_inject"
    priority = 45

    async def handle(self, ctx: Any) -> HookResult | None:
        node_id = str(getattr(getattr(ctx, "node", None), "id", "") or "")
        if node_id != IMAGE_GEN_NODE_ID:
            return None
        workspace_root = getattr(getattr(ctx, "rctx", None), "workspace_root", None)
        if workspace_root is None:
            return None
        availability = image_channel_availability(Path(workspace_root))
        text = build_availability_text(bool(availability.get("gpt")), bool(availability.get("gemini")))

        replaced = False
        for msg in ctx.messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str) and PLACEHOLDER in content:
                msg["content"] = content.replace(PLACEHOLDER, text)
                replaced = True
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        and PLACEHOLDER in part["text"]
                    ):
                        part["text"] = part["text"].replace(PLACEHOLDER, text)
                        replaced = True
        if not replaced:
            return None
        logger.info(
            "draw.image_gen_inject: gpt=%s gemini=%s node=%s",
            availability.get("gpt"), availability.get("gemini"), node_id,
        )
        return HookResult(modified=True)
