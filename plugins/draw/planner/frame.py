"""规划请求的固定部分：材料说明、输出去向、材料块与数量约束（对应 LWB scene-planner-frame.js
+ scene-planner.js 的 buildCharacterInfoForLLM / buildSessionLimitsLine）。

本项目的材料来源：已录入角色清单 + 本次绘图请求（无世界书、无插图点）。绘图请求由 bot 整理好再交给规划，
是唯一的叙事来源；不再附带近期对话（群聊杂音会干扰选角色和写 tag）。
用户可改的内容（开场、指南、场景规则）围绕这些固定文本注入。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .profile import PlannerProfile, normalize_profile

# 模板占位符：保存校验时要求 content 模板含这两项。
CONTENT_PLACEHOLDERS: tuple[str, ...] = ("{{characterInfo}}", "{{drawRequest}}")

CONTENT_TEMPLATE = (
    "<content>\n"
    "{{characterInfo}}\n"
    "---\n"
    "【绘图请求】\n"
    "{{drawRequest}}\n"
    "</content>"
)


def build_frame_text(profile: PlannerProfile | Mapping[str, Any] | None) -> str:
    name = normalize_profile(profile).image_model_name
    return (
        "## 你收到的材料\n"
        "- <content>：本次唯一的叙事来源。开头是【已录入角色】列表；固定外貌由角色库自动写入，你只写本图中的状态，"
        "不要重复固定外貌里已有的 tag；之后是本次的【绘图请求】。\n"
        "- 绘图要求中的数量约束：images 数量、每图人数上限。\n"
        "\n"
        "## 你的输出去哪\n"
        "The app uses each image's scene and character fields, together with the character library and the user's "
        f"generation settings, to build a request for {name}. Field meanings and model-specific controls are described "
        "in submit_scene_plan."
    )


def build_guide_section(guide: str, profile: PlannerProfile | Mapping[str, Any] | None) -> str:
    text = str(guide or "").strip()
    if not text:
        return ""
    return f"## {normalize_profile(profile).image_model_name} Tag 指南\n\n{text}"


def build_rules_section(rules: str) -> str:
    text = str(rules or "").strip()
    return f"## 场景规则\n\n{text}" if text else ""


def _reference_list(values: Any, label: str) -> str:
    if not isinstance(values, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            tags = str(item.get("tags") or "").strip()
            if name and tags:
                parts.append(f"{name}（{tags}）")
            elif name or tags:
                parts.append(name or tags)
        else:
            text = str(item or "").strip()
            if text:
                parts.append(text)
    return "；".join(parts)


NAME_LEGEND = "（name 照抄下面的值；别名只是正文里的叫法，不要写进 name）"


def build_character_info(present_characters: Sequence[Mapping[str, Any]] | None) -> str:
    """已录入角色的事实清单；怎么用由工具 schema 定义。"""
    if not present_characters:
        return "【已录入角色】: 无"
    lines: list[str] = []
    has_aliases = False
    for character in present_characters:
        if not isinstance(character, Mapping):
            continue
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        type_ = f"[{str(character.get('type') or 'girl').strip()}]"
        danbooru = str(character.get("danbooru") or "").strip()
        lines.append(f"- name: {name} {type_}" + (f" | danbooru: {danbooru}" if danbooru else ""))
        aliases = [str(a).strip() for a in (character.get("aliases") or []) if str(a).strip()]
        if aliases:
            has_aliases = True
            lines.append(f"  别名: {', '.join(aliases)}")
        appearance = str(character.get("appearance") or "").strip()
        if appearance:
            lines.append(f"  固定外貌: {appearance}")
        outfits = _reference_list(character.get("outfits"), "服装")
        if outfits:
            lines.append(f"  服装参考: {outfits}")
        states = _reference_list(character.get("dynamic_states"), "动态标签")
        if states:
            lines.append(f"  动态标签（画面里有才用，可用多个）: {states}")
    legend = f"{NAME_LEGEND}\n" if has_aliases else ""
    return f"【已录入角色】\n{legend}" + "\n".join(lines)


def build_content_block(template: str | None, *, character_info: str, draw_request: str) -> str:
    """字面替换占位符（不做二次展开，正文里的 {{…}} 不会被再次替换）。"""
    text = template if isinstance(template, str) and template.strip() else CONTENT_TEMPLATE
    text = text.replace("{{characterInfo}}", character_info or "【已录入角色】: 无")
    text = text.replace("{{drawRequest}}", str(draw_request or "").strip() or "（无）")
    return text


def missing_content_placeholders(template: str) -> list[str]:
    return [p for p in CONTENT_PLACEHOLDERS if p not in str(template or "")]


def build_limits_line(max_images: Any, max_characters_per_image: Any, max_plan_images: Any) -> str:
    def limit(value: Any) -> int:
        try:
            return int(value) if not isinstance(value, bool) and int(value) > 0 else 0
        except (TypeError, ValueError):
            return 0

    exact = limit(max_images)
    char_cap = limit(max_characters_per_image)
    plan_cap = limit(max_plan_images)
    clauses: list[str] = []
    if exact:
        clauses.append(f"images 必须恰好包含 {exact} 项")
    elif plan_cap:
        clauses.append(f"images 最多包含 {plan_cap} 项")
    if char_cap:
        clauses.append(f"每项 characters 最多 {char_cap} 人")
    return f"本次提交数量约束：{'；'.join(clauses)}。" if clauses else ""
