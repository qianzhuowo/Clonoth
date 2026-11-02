"""生图渠道门控：未配置渠道的生图工具对模型不可见。

- 未显式配置 model 的渠道 → 对应生图工具（gpt_image_2 / gemini_image）从模型
  可见工具列表与授权集合中移除；
- 两个渠道都未配置 → ``draw.image_gen`` 从所有节点的委派目标（dispatch_to）中移除。

通过引擎通用的 tool_visibility 注册面实现（build_node_tool_specs 与 dispatch_to
刷新都会询问它），推理循环里没有任何生图相关逻辑。
"""
from __future__ import annotations

import logging
from typing import Any

from .channels import IMAGE_CHANNELS, IMAGE_GEN_NODE_ID, image_channel_availability

logger = logging.getLogger(__name__)

FILTER_NAME = "draw.image_gen_gating"


def image_gen_visibility(ctx: Any) -> dict[str, set[str]] | None:
    """tool_visibility 过滤器：返回应隐藏的生图工具与委派目标。"""
    workspace_root = getattr(ctx, "workspace_root", None)
    if workspace_root is None:
        return None
    availability = image_channel_availability(workspace_root)
    hidden_tools = {tool for channel, (_env, tool) in IMAGE_CHANNELS.items() if not availability.get(channel)}
    hidden_targets = set() if any(availability.values()) else {IMAGE_GEN_NODE_ID}
    if not hidden_tools and not hidden_targets:
        return None
    return {"tools": hidden_tools, "delegate_targets": hidden_targets}
