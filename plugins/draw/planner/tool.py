"""submit_scene_plan 工具 schema 与字段说明覆写（对应 LWB scene-plan-tool.js）。

字段语义只在这里定义；散文提示词只决定"画什么"。后端差异（center 支持、
interact 语法、uc 范围、post_prompt）来自 profile。本项目去掉了 insert_after。
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .contract import SCENE_TITLE_MAX_LENGTH, SUBMIT_SCENE_PLAN_TOOL_NAME
from .profile import PlannerProfile, normalize_profile

CHARACTER_TYPES: tuple[str, ...] = ("girl", "boy", "woman", "man", "other", "no_humans")

INTERACT_DESCRIPTIONS = {
    "directional": "与其他角色的接触动作，带方向前缀：`source#动作` 发起方、`target#动作` 接受方、`mutual#动作` 互相。"
                   "同一个互动分别写进每位参与角色自己的 interact；一个角色参与多个互动时逗号并列。没有互动时省略。",
    "plain": "与其他角色的接触动作 tag，如 `hugging`、`holding hands`。没有互动时省略。",
}

UC_DESCRIPTIONS = {
    "character": "只针对该角色排除的 tag：与当前状态互斥的特征（无胸罩→`bra`、脱帽→`hat`）、因视角或遮挡不可见的部位、"
                 "与另一角色相反的情绪。作为该角色专属的负向生效。通用画质负面由用户预设负责，不写在这里。没有时省略。",
    "global": "Tags to exclude from the entire image. All characters' uc values are merged into one global negative prompt, "
              "so include a tag only when it is unwanted for every subject and the background. A feature or emotion needed "
              "by another character stays out of uc. General quality negatives come from the user's preset. Omit when there "
              "is nothing to exclude.",
}

TOOL_DESCRIPTION = "提交本次全部图片任务。一个回合只调用一次。"

IMAGES_DESCRIPTION = "图片任务，按阅读顺序排列。数量约束见绘图要求。"

CHARACTERS_DESCRIPTION = (
    "画面中出现的角色，按重要度排序。纯风景、物件、建筑主体时为空数组。"
    "每项的字段按 type → danbooru → appear → costume → action → interact 的顺序拼成该角色的提示词。"
    "同一个角色的外貌（已录入角色是角色清单里的固定外貌，未录入角色是你写的 appear）、costume、action 合起来就是这个角色的提示词："
    "相同或同义的词只写一处，按 外貌 > costume > action 的优先级留在靠前的字段；属于某个角色的词不写进整图 scene；不同角色之间不受此限。"
)

TITLE_DESCRIPTION = (
    f"本次绘画的标题：简体中文，{SCENE_TITLE_MAX_LENGTH} 字内，只输出标题本身，不加标点。\n"
    "把它当成这一格画面的小标题，只写画里最具体的那个动作、物件或瞬间，不要概括主题。\n"
    "同一批的几张图，句式和开头都要各不相同，少用「XX的XX」。可参考：\n"
    "那只手终于探了出来 / 听到她说晚上好 / 距离正在缩到三米 / 凌晨两点的便利店 / 路灯吞掉了影子"
)

ORIENTATION_DESCRIPTION = (
    "本图画幅方向，先定景别与主体排布再决定，人数只作参考。"
    "portrait（竖幅）：单人、直立站姿、close-up / upper body 等近景、双人贴近构图；"
    "landscape（横幅）：三人及以上群像、wide shot / 全景远景、开阔环境、左右展开的互动与站位。"
    "两人同框不必然横幅；方向须与景别一致。拿不准时填 portrait。"
)

SCENE_DESCRIPTION = "画面整体：人数与关系、构图与视角、背景、光影、氛围，逗号分隔的英文 tag。角色个体的外貌与动作不写在这里。拼在正向提示词最前。"

POST_PROMPT_DESCRIPTION = "本字段留空不写"

NAME_DESCRIPTION = "角色名。已录入角色用【已录入角色】中的名称，原文用别名也归一到该名称。未知角色用原文名字，无名时用简短称呼。"
FORM_DESCRIPTION = "多形态角色必填：本图出现的形态名，取自该角色的“形态”列表；单形态角色省略。"
TYPE_DESCRIPTION = (
    f"Character category: {' / '.join(CHARACTER_TYPES)}. Use `no_humans` for non-humanoid subjects such as animals or "
    "monsters. Fill this for every unregistered character; omit for registered characters, whose category comes from the "
    "character library."
)
DANBOORU_DESCRIPTION = "该角色的 Danbooru 身份 tag，下划线格式：同人角色 `character_name_(series)`，原创角色 `中文名_(original)`。仅在确定时提交；已录入角色省略。"
APPEAR_DESCRIPTION = (
    "Stable appearance visible in this composition, such as hair, eyes or build. Every unregistered character needs a "
    "non-empty appearance description. Omit for registered characters, whose appearance comes from the character library."
)
COSTUME_DESCRIPTION = (
    "持续状态：换一张图仍会保持的、固定外貌以外的一切——衣着及其穿戴状态（敞开、湿透、破损）、身体部件、非人特征、配饰等。"
    "写画面里有的东西。脱掉的衣物不再写，露出的部位写明（topless、bare chest），其它身体部件、非人特征和配饰照旧保留；"
    '全部都没有才写 "costume": ""。'
    "这个字段必须提交：没有衣服（全裸、半裸、只剩配饰）是合法且常见的取值，这时只写其余状态，不要为了让这个字段有内容而去补一套衣服。"
    "取值优先级：正文写的穿搭 > 前文画面里这个角色的 costume > 服装参考、世界书、数据表里的穿搭描述（没有前文画面时，最后一条不存在）。"
    "沿用记忆时直接照抄记忆里的 costume，不要再用服装参考补一遍。"
    "要选衣服时（新角色、换了形态、或正文写了新的衣物），已录入角色有服装参考的，选一套或其变体，衣物以参考为准，只按画面调整状态，"
    "不要额外补充与参考里已有衣物重复或同义的内容，也不把多套参考拼在一起；正文写到而参考里没有的衣物可以补上，正文穿的整套衣服参考里都没有可以直接整套写。"
)
ACTION_DESCRIPTION = (
    "只写瞬时内容：这一瞬间的姿态、动作、视线、面向、表情；撩衣、握物这类一瞬间的动作也写这里。"
    "身上一直有的东西（身体部件、非人特征、配饰、穿着本身）不写在这里，写进 costume。"
)
CENTER_GRID_DESCRIPTION = "角色在画面中的位置，5×5 网格：列 A–E 从左到右，行 1–5 从上到下。居中（C3）时省略。多个角色可以重叠（拥抱、亲吻）。"
CENTER_NORMALIZED_DESCRIPTION = "角色在画面中的位置，左上角 (0, 0)，右下角 (1, 1)。居中 (0.5, 0.5) 时省略。多个角色可以重叠（拥抱、亲吻）。"


def _normalize_limit(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _string_schema(description: str, *, min_length: int = 0, max_length: int = 0) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if min_length > 0:
        schema["minLength"] = min_length
    if max_length > 0:
        schema["maxLength"] = max_length
    schema["description"] = description
    return schema


def _center_schema(mode: str) -> dict[str, Any]:
    if mode == "normalized":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y"],
            "properties": {
                "x": {"type": "number", "minimum": 0, "maximum": 1},
                "y": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "description": CENTER_NORMALIZED_DESCRIPTION,
        }
    return {"type": "string", "pattern": "^[A-E][1-5]$", "description": CENTER_GRID_DESCRIPTION}


def get_character_properties(profile: PlannerProfile | Mapping[str, Any] | None) -> dict[str, Any]:
    effective = normalize_profile(profile)
    properties: dict[str, Any] = {
        "name": _string_schema(NAME_DESCRIPTION, min_length=1),
        "form": _string_schema(FORM_DESCRIPTION),
        "type": _string_schema(TYPE_DESCRIPTION),
        "danbooru": _string_schema(DANBOORU_DESCRIPTION),
        "appear": _string_schema(APPEAR_DESCRIPTION),
        "costume": _string_schema(COSTUME_DESCRIPTION),
        "action": _string_schema(ACTION_DESCRIPTION, min_length=1),
        "interact": _string_schema(INTERACT_DESCRIPTIONS[effective.interact_syntax]),
        "uc": _string_schema(UC_DESCRIPTIONS[effective.uc_scope]),
    }
    if effective.center_mode != "none":
        properties["center"] = _center_schema(effective.center_mode)
    return properties


def apply_field_description_overrides(tool: dict[str, Any], descriptions: Mapping[str, Any] | None) -> None:
    """路径同 list_field_descriptions：`images`、`images[].scene`、`images[].characters[].name`…；`tool` 为函数级。"""
    if not descriptions:
        return

    def walk(schema: Any, path: str) -> None:
        if not isinstance(schema, dict):
            return
        override = descriptions.get(path)
        if isinstance(schema.get("description"), str) and isinstance(override, str) and override.strip():
            schema["description"] = override
        if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
            for key, child in schema["properties"].items():
                walk(child, f"{path}.{key}" if path else key)
        elif schema.get("type") == "array" and isinstance(schema.get("items"), dict):
            walk(schema["items"], f"{path}[]")

    walk(tool["function"].get("parameters"), "")
    tool_description = descriptions.get("tool")
    if isinstance(tool_description, str) and tool_description.strip():
        tool["function"]["description"] = tool_description


def list_field_descriptions(tool: Mapping[str, Any]) -> list[dict[str, str]]:
    """扁平化工具里所有带 description 的字段：[{path, description}]，首项为 `tool`。"""
    out: list[dict[str, str]] = [{"path": "tool", "description": str(tool["function"].get("description") or "")}]

    def walk(schema: Any, path: str) -> None:
        if not isinstance(schema, Mapping):
            return
        if path and isinstance(schema.get("description"), str):
            out.append({"path": path, "description": schema["description"]})
        if schema.get("type") == "object" and isinstance(schema.get("properties"), Mapping):
            for key, child in schema["properties"].items():
                walk(child, f"{path}.{key}" if path else key)
        elif schema.get("type") == "array" and isinstance(schema.get("items"), Mapping):
            walk(schema["items"], f"{path}[]")

    walk(tool["function"].get("parameters"), "")
    return out


def create_submit_scene_plan_tool(*, max_images: Any = 0, max_plan_images: Any = 0,
                                  max_characters_per_image: Any = 0,
                                  profile: PlannerProfile | Mapping[str, Any] | None = None,
                                  descriptions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """OpenAI function-calling 形状：{type:'function', function:{name, description, parameters}}。"""
    exact = _normalize_limit(max_images)
    plan_cap = _normalize_limit(max_plan_images)
    char_cap = _normalize_limit(max_characters_per_image)
    effective = normalize_profile(profile)
    max_plan_items = exact or plan_cap

    characters_schema: dict[str, Any] = {"type": "array"}
    if char_cap:
        characters_schema["maxItems"] = char_cap
    characters_schema["description"] = CHARACTERS_DESCRIPTION
    characters_schema["items"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "costume", "action"],
        "properties": get_character_properties(effective),
    }

    image_properties: dict[str, Any] = {
        "index": {"type": "integer", "minimum": 1, "description": "本图序号，从 1 起。"},
        "title": _string_schema(TITLE_DESCRIPTION, min_length=1, max_length=SCENE_TITLE_MAX_LENGTH),
        "orientation": {"type": "string", "enum": ["portrait", "landscape"], "description": ORIENTATION_DESCRIPTION},
        "scene": _string_schema(SCENE_DESCRIPTION, min_length=1),
        "characters": characters_schema,
    }
    if effective.post_prompt == "append":
        image_properties["post_prompt"] = _string_schema(POST_PROMPT_DESCRIPTION)

    images_schema: dict[str, Any] = {"type": "array", "minItems": exact or 1}
    if max_plan_items:
        images_schema["maxItems"] = max_plan_items
    images_schema["description"] = IMAGES_DESCRIPTION
    images_schema["items"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "title", "orientation", "scene", "characters"],
        "properties": image_properties,
    }

    tool: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": SUBMIT_SCENE_PLAN_TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["images"],
                "properties": {"images": images_schema},
            },
        },
    }
    apply_field_description_overrides(tool, descriptions)
    return tool


def default_field_descriptions(profile: PlannerProfile | Mapping[str, Any] | None = None) -> dict[str, str]:
    """面板"恢复默认"用：不带覆写的默认字段说明表。"""
    tool = create_submit_scene_plan_tool(profile=profile)
    return {item["path"]: item["description"] for item in list_field_descriptions(copy.deepcopy(tool))}
