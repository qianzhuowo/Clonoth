"""规划 profile：图片后端实际能从场景计划里消费什么。

规划提示词、工具 schema 与请求预览都从同一个 profile 渲染，模型不会被教会
一个编译器会丢弃的字段。对应 LWB scene-planner-profile.js。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CENTER_MODES = frozenset({"none", "grid", "normalized"})
INTERACT_SYNTAXES = frozenset({"plain", "directional"})
UC_SCOPES = frozenset({"global", "character"})
POST_PROMPT_MODES = frozenset({"none", "append"})


@dataclass(frozen=True)
class PlannerProfile:
    image_model_name: str = "图像模型"
    center_mode: str = "none"
    interact_syntax: str = "plain"
    uc_scope: str = "global"
    post_prompt: str = "none"


def normalize_profile(profile: Mapping[str, Any] | PlannerProfile | None) -> PlannerProfile:
    if isinstance(profile, PlannerProfile):
        return profile
    source: Mapping[str, Any] = profile if isinstance(profile, Mapping) else {}
    name = str(source.get("image_model_name") or source.get("imageModelName") or "").strip()

    def pick(key: str, camel: str, allowed: frozenset[str], default: str) -> str:
        value = source.get(key, source.get(camel))
        return value if isinstance(value, str) and value in allowed else default

    return PlannerProfile(
        image_model_name=name or PlannerProfile.image_model_name,
        center_mode=pick("center_mode", "centerMode", CENTER_MODES, PlannerProfile.center_mode),
        interact_syntax=pick("interact_syntax", "interactSyntax", INTERACT_SYNTAXES, PlannerProfile.interact_syntax),
        uc_scope=pick("uc_scope", "ucScope", UC_SCOPES, PlannerProfile.uc_scope),
        post_prompt=pick("post_prompt", "postPrompt", POST_PROMPT_MODES, PlannerProfile.post_prompt),
    )


def novelai_v45_profile() -> PlannerProfile:
    """NovelAI V4.5：网格坐标、方向化互动、角色级负面；不暴露 post_prompt。"""
    return PlannerProfile(
        image_model_name="NovelAI Diffusion V4.5",
        center_mode="grid",
        interact_syntax="directional",
        uc_scope="character",
        post_prompt="none",
    )


def novelai_v5_profile() -> PlannerProfile:
    """NovelAI V5：角色位置用 0～1 坐标，其它与 V4.5 相同。"""
    return PlannerProfile(
        image_model_name="NovelAI Diffusion V5",
        center_mode="normalized",
        interact_syntax="directional",
        uc_scope="character",
        post_prompt="none",
    )


def novelai_profile_for_model(model: Any) -> PlannerProfile:
    from ..presets import is_v5_model

    return novelai_v5_profile() if is_v5_model(model) else novelai_v45_profile()
