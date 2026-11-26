"""绘图插件包：绘图节点相关的引擎侧能力集中于此。

- finish_requires_tool：节点声明的 finish 前置工具硬校验（after_tool_call 记录成功，
  terminal_tool 拦截未满足的 finish；异步工具靠历史占位判定）。
- gating：未配置生图渠道的工具 / draw.image_gen 委派目标对模型不可见
  （引擎通用 tool_visibility 注册面）。
- character_inject：draw.novelai_planner 命中角色库时注入角色 tag（before_prompt_build；agent 接管后不再执行）。
- agent.entry：draw.novelai_planner 的独立规划+生图 agent（before_prompt_build 高优先级接管，返回 finish）。
- image_gen_inject：draw.image_gen 生图渠道可用性说明（before_prompt_build）。
- character_tool：save_draw_character 内置工具，聊天里由平台管理员把角色录入角色库（工具自身校验管理员身份）。
- novelai_prompts：draw.novelai_planner 的 NovelAI 提示词模板（prompt_sections static）。

渠道判定只在 channels.py，drawtools 私有/模板文件定位只在 paths.py。
"""
from __future__ import annotations

from typing import Any

PLUGIN_META = {
    "name": "draw",
    "version": "2.0.0",
    "description": "绘图：NovelAI 场景规划 agent、finish 前置工具校验、生图渠道门控、角色库/模板注入；管理面板（API/参数/角色/图片/提示词模板）。",
    "author": "Clonoth",
    "processes": ["engine", "supervisor"],
    "web": {
        "panels": [
            {"id": "manager", "slot": "settings", "order": 55, "title": "绘图", "entry": "/v1/plugins/draw/web/"},
        ],
    },
}


def register(ctx: Any) -> None:
    get = getattr(ctx.contributions, "get", lambda _name: None)
    routes = get("routes")
    if routes is not None:
        # supervisor 进程：只挂管理 API 与面板静态页，不注册引擎钩子。
        from pathlib import Path

        from fastapi import APIRouter

        from engine.faces.routes import static_router

        from .routes import build_api_router

        routes.register(build_api_router(), description="draw 绘图管理 API（需鉴权）")
        client = APIRouter()
        client.include_router(static_router(Path(__file__).parent / "web"), prefix="/web")
        routes.register(client, description="draw 面板静态资源（需鉴权）")
    if get("tool_visibility") is None and get("prompt_sections") is None:
        return
    from .agent.entry import DrawAgentEntry
    from .character_inject import DrawCharacterInjector
    from .finish_requires_tool import FinishRequiresToolGate, FinishSuccessRecorder
    from .gating import FILTER_NAME, image_gen_visibility
    from .image_gen_inject import ImageGenAvailabilityInjector
    from .novelai_prompts import SECTION_NAME, render_novelai_prompts

    ctx.hooks.register("after_tool_call", FinishSuccessRecorder())
    ctx.hooks.register("terminal_tool", FinishRequiresToolGate())
    # tools face 就是引擎的 ToolRegistry（engine/inference/ai_step.py mount），agent 用它执行 search_web 等自定义工具。
    ctx.hooks.register("before_prompt_build", DrawAgentEntry(registry=ctx.contributions.get("tools")))
    ctx.hooks.register("before_prompt_build", DrawCharacterInjector())
    ctx.hooks.register("before_prompt_build", ImageGenAvailabilityInjector())

    # 录入角色的内置工具：节点白名单决定哪个节点看得到（qq.orchestrator），管理员身份由工具自己校验。
    tools = get("tools")
    if tools is not None:
        from .character_tool import DESCRIPTION, INPUT_SCHEMA, TOOL_NAME, save_draw_character

        tools.register_builtin_tool(TOOL_NAME, DESCRIPTION, INPUT_SCHEMA, save_draw_character)

    visibility = ctx.contributions.get("tool_visibility")
    if visibility is None:
        raise RuntimeError("draw plugin requires the engine 'tool_visibility' contribution face")
    visibility.register_filter(FILTER_NAME, image_gen_visibility)

    sections = ctx.contributions.get("prompt_sections")
    if sections is None:
        raise RuntimeError("draw plugin requires the engine 'prompt_sections' contribution face")
    sections.register_section(SECTION_NAME, render_novelai_prompts, order=10, scope="static")
