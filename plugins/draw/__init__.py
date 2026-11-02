"""绘图插件包：绘图节点相关的引擎侧能力集中于此。

- finish_requires_tool：节点声明的 finish 前置工具硬校验（after_tool_call 记录成功，
  terminal_tool 拦截未满足的 finish；异步工具靠历史占位判定）。
- gating：未配置生图渠道的工具 / draw.image_gen 委派目标对模型不可见
  （引擎通用 tool_visibility 注册面）。
- character_inject：draw.novelai_planner 命中角色库时注入角色 tag（before_prompt_build）。
- image_gen_inject：draw.image_gen 生图渠道可用性说明（before_prompt_build）。
- novelai_prompts：draw.novelai_planner 的 NovelAI 提示词模板（prompt_sections static）。

渠道判定只在 channels.py，drawtools 私有/模板文件定位只在 paths.py。
"""
from __future__ import annotations

from typing import Any

PLUGIN_META = {
    "name": "draw",
    "version": "1.0.0",
    "description": "绘图节点支持：finish 前置工具校验、生图渠道门控、角色库/渠道可用性/NovelAI 模板注入。",
    "author": "Clonoth",
    "processes": ["engine"],
}


def register(ctx: Any) -> None:
    from .character_inject import DrawCharacterInjector
    from .finish_requires_tool import FinishRequiresToolGate, FinishSuccessRecorder
    from .gating import FILTER_NAME, image_gen_visibility
    from .image_gen_inject import ImageGenAvailabilityInjector
    from .novelai_prompts import SECTION_NAME, render_novelai_prompts

    ctx.hooks.register("after_tool_call", FinishSuccessRecorder())
    ctx.hooks.register("terminal_tool", FinishRequiresToolGate())
    ctx.hooks.register("before_prompt_build", DrawCharacterInjector())
    ctx.hooks.register("before_prompt_build", ImageGenAvailabilityInjector())

    visibility = ctx.contributions.get("tool_visibility")
    if visibility is None:
        raise RuntimeError("draw plugin requires the engine 'tool_visibility' contribution face")
    visibility.register_filter(FILTER_NAME, image_gen_visibility)

    sections = ctx.contributions.get("prompt_sections")
    if sections is None:
        raise RuntimeError("draw plugin requires the engine 'prompt_sections' contribution face")
    sections.register_section(SECTION_NAME, render_novelai_prompts, order=10, scope="static")
