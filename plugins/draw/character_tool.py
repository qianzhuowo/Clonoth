"""save_draw_character：在聊天里把一个角色录入 NovelAI 角色库（engine 进程的内置工具）。

- 只有平台管理员能用。身份取自 ToolContext.platform_is_admin，它来自适配器写进任务的
  platform_auth.is_admin（toolbox/context.py），不取自工具参数；没有身份信息一律按非管理员处理。
- 节点白名单决定哪个节点看得到这个工具（目前只有 qq.orchestrator）；这里的校验不依赖白名单。
- 写盘走 admin.record_character：和面板保存共用一把文件锁，写完后库的 revision 变化，
  面板上没保存的旧草稿会被 409 挡住，不会把这次录入冲掉。
- 空字符串 / 空数组按“没给”处理：模型常把可选字段填成空值，不能因此清掉已有内容。要清空请用面板。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

TOOL_NAME = "save_draw_character"
ADMIN_ONLY_MESSAGE = "只有 Clonoth 管理员可以录入角色，这次没有保存。"

_FIELD_KEYS = ("aliases", "type", "danbooru", "appearance", "negative_tags", "outfits", "dynamic_states")
_NAMED_ITEMS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "这一条的名称，如“校服”“哭泣”"},
            "tags": {"type": "string", "description": "英文 tag，逗号分隔"},
        },
        "required": ["tags"],
    },
}

DESCRIPTION = (
    "把一个角色录入 NovelAI 绘图角色库；之后绘图请求里提到这个角色的名字或别名时，会自动带上这里的外貌和服装。"
    "仅限 Clonoth 管理员，非管理员调用会被拒绝。"
    "角色已存在时默认不改动并返回现有内容；确认要补充或修改后带 if_exists=\"update\" 再调用："
    "只改这次给出的字段，别名并入，服装和动态标签按名称合并。"
    "appearance / negative_tags / outfits / dynamic_states 里的 tags 一律写英文 danbooru 风格 tag，逗号分隔。"
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "角色正式名（聊天里怎么称呼就怎么写，可以是中文）"},
        "aliases": {"type": "array", "items": {"type": "string"}, "description": "别名、昵称、外文名"},
        "type": {"type": "string", "description": "girl / woman / boy / man / other / no_humans，默认 girl"},
        "danbooru": {"type": "string", "description": "官方 danbooru 角色 tag，如 hatsune_miku；没有或不确定就不填，不要编造"},
        "appearance": {"type": "string", "description": "固定外貌：发色、发型、瞳色、体型等不随场景变化的特征"},
        "negative_tags": {"type": "string", "description": "画这个角色时要排除的 tag"},
        "outfits": {**_NAMED_ITEMS, "description": "服装参考，每套一条"},
        "dynamic_states": {**_NAMED_ITEMS, "description": "动态标签：按情境才出现的状态（表情、受伤、变身等），每种一条"},
        "group": {"type": "string", "description": "放进哪个分组（按分组名）；没有这个分组就新建。不填则不动分组"},
        "if_exists": {"type": "string", "enum": ["error", "update"], "description": "角色已存在时：error（默认）不改动并返回现有内容；update 补充或修改"},
    },
    "required": ["name"],
}


def _fail(message: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}", **fields}}


def _named(items: Any) -> str:
    parts = [f"{i.get('name') or '未命名'}（{i.get('tags')}）" for i in items or [] if isinstance(i, Mapping) and i.get("tags")]
    return "；".join(parts)


def describe_character(character: Mapping[str, Any]) -> str:
    """给模型转述用的一段文字：只列有内容的字段。"""
    rows = [
        ("别名", "、".join(character.get("aliases") or [])),
        ("type", character.get("type")),
        ("danbooru", character.get("danbooru")),
        ("外貌", character.get("appearance")),
        ("排除", character.get("negative_tags")),
        ("服装", _named(character.get("outfits"))),
        ("动态标签", _named(character.get("dynamic_states"))),
    ]
    return "；".join(f"{label}：{value}" for label, value in rows if value) or "（还没有填任何内容）"


async def save_draw_character(arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
    if getattr(ctx, "platform_is_admin", False) is not True:
        return _fail(ADMIN_ONLY_MESSAGE)
    root = getattr(ctx, "workspace_root", None)
    if root is None:
        return _fail("找不到工作区，无法写入角色库")
    from . import admin

    args = arguments if isinstance(arguments, Mapping) else {}
    fields: dict[str, Any] = {"name": args.get("name")}
    fields.update({key: args[key] for key in _FIELD_KEYS if args.get(key) not in (None, "", [])})
    if_exists = str(args.get("if_exists") or "error").strip().lower()
    group = args.get("group") if isinstance(args.get("group"), str) else ""
    try:
        result = await asyncio.to_thread(admin.record_character, Path(root), fields, if_exists=if_exists, group=group)
    except admin.AdminError as error:
        existing = error.details.get("character") if isinstance(error.details, Mapping) else None
        if isinstance(existing, Mapping):
            return _fail(
                f"{error.message}，这次没有改动。现有内容——{describe_character(existing)}。"
                "要补充或修改，先向用户确认，再带 if_exists=\"update\" 调用一次。",
                character=dict(existing),
            )
        return _fail(error.message)
    character = result["character"]
    text = f"{'已录入' if result['created'] else '已更新'}角色「{character['name']}」。当前内容——{describe_character(character)}。"
    if not result["active"]:
        text += "注意：这个角色或它所在的分组目前是停用的，出图时不会用到，需要到绘图面板的角色标签页里启用。"
    return {"ok": True, "data": {"result": text, "character": character, "created": result["created"], "active": result["active"]}}
