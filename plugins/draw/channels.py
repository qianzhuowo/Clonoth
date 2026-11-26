"""生图渠道是否启用的唯一判定。

某渠道“可用”当且仅当 data/config.yaml 的 system_models 里配置了对应槽位
（设置 → 系统模型 → 生图）：gpt → image_gpt，gemini → image。读取走 clonoth_channels，
与 tools/gpt_image_2.py、tools/gemini_image.py 一致。

被渠道门控（gating）和 draw.image_gen 可用性注入（image_gen_inject）共用。
"""
from __future__ import annotations

from pathlib import Path

import clonoth_channels as chn

# 渠道 -> (system_models 槽位, 生图工具名)
IMAGE_CHANNELS: dict[str, tuple[str, str]] = {
    "gpt": ("image_gpt", "gpt_image_2"),
    "gemini": ("image", "gemini_image"),
}
IMAGE_GEN_TOOLS = frozenset(tool for (_slot, tool) in IMAGE_CHANNELS.values())
# 两个渠道都未配置时从委派目标中移除的节点
IMAGE_GEN_NODE_ID = "draw.image_gen"


def image_channel_availability(workspace_root: Path | str) -> dict[str, bool]:
    """返回 {'gpt': bool, 'gemini': bool}。"""
    cfg = chn.load_config(workspace_root)
    result: dict[str, bool] = {}
    for channel, (slot, _tool) in IMAGE_CHANNELS.items():
        try:
            result[channel] = chn.slot_channel(cfg, slot, root=workspace_root) is not None
        except chn.ChannelError:
            result[channel] = False
    return result
