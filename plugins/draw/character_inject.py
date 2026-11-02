"""draw.novelai_planner 角色库自动注入。

当用户请求（instruction + 最近几条历史）命中 character_tags 中的角色时，把该角色的
danbooru / appearance / outfits 作为一条 system 消息插在系统提示之后、历史之前，
不再依赖模型主动调用 draw_context。

目标节点只有 draw.novelai_planner：danbooru tag 体系属于 NovelAI 节点，
draw.image_gen 的提示词明确把它划给 NovelAI 节点。
角色库按 drawtools 规则读取：character_tags.yaml 优先，缺失回退 example。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from engine.hooks.types import HookResult

from .paths import drawtools_file

logger = logging.getLogger(__name__)

TARGET_NODE_IDS = frozenset({"draw.novelai_planner"})

# 回看的历史条数（覆盖“再画一张她”这类指代）
_SCAN_HISTORY_DEPTH = 4
# 单次最多注入的角色数，避免上下文膨胀
_MAX_INJECT_CHARACTERS = 8


def load_characters(workspace_root: Path) -> list[dict[str, Any]]:
    path = drawtools_file(workspace_root, "character_tags.yaml")
    if path is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("draw.character_inject: 读取角色库失败: %s", exc)
        return []
    chars = data.get("characters") if isinstance(data, dict) else []
    if not isinstance(chars, list):
        return []
    return [c for c in chars if isinstance(c, dict)]


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _character_keywords(ch: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for key in ("name", "danbooru"):
        value = str(ch.get(key) or "").strip()
        if value:
            keys.append(value)
            keys.append(value.replace("_", " "))
    aliases = ch.get("aliases") if isinstance(ch.get("aliases"), list) else []
    keys.extend(str(a).strip() for a in aliases if str(a).strip())
    return list(dict.fromkeys(keys))


def _matches(query_norm: str, ch: dict[str, Any]) -> bool:
    for key in _character_keywords(ch):
        k = _norm(key)
        if k and k in query_norm:
            return True
    return False


def _build_scan_text(instruction: str, history: Any) -> str:
    parts: list[str] = [str(instruction or "")]
    if isinstance(history, list) and history:
        for msg in history[-_SCAN_HISTORY_DEPTH:]:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            parts.append(str(content or ""))
    return " ".join(parts)


def _format_outfits(outfits: Any) -> list[str]:
    lines: list[str] = []
    if not isinstance(outfits, list):
        return lines
    for outfit in outfits:
        if not isinstance(outfit, dict):
            continue
        name = str(outfit.get("name") or "").strip()
        tags = re.sub(r"\s+", " ", str(outfit.get("tags") or "").strip())
        if not tags:
            continue
        lines.append(f"      * {name}: {tags}" if name else f"      * {tags}")
    return lines


def build_character_block(matched: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for ch in matched:
        name = str(ch.get("name") or "").strip()
        danbooru = str(ch.get("danbooru") or "").strip()
        ctype = str(ch.get("type") or "").strip()
        appearance = re.sub(r"\s+", " ", str(ch.get("appearance") or "").strip())
        lines = [f"- 角色「{name}」"]
        if danbooru:
            lines.append(f"    danbooru: {danbooru}")
        else:
            lines.append("    danbooru: （无官方 tag，禁止把中文名当 tag，用下面外貌描述表现身份）")
        if ctype:
            lines.append(f"    type: {ctype}")
        if appearance:
            lines.append(f"    appearance: {appearance}")
        outfit_lines = _format_outfits(ch.get("outfits"))
        if outfit_lines:
            lines.append("    outfits:")
            lines.extend(outfit_lines)
        blocks.append("\n".join(lines))

    header = (
        "[已录入角色库 · 自动命中]\n"
        "以下角色已在角色标签库中命中当前绘图请求，请务必优先使用这里给出的"
        " danbooru / appearance / outfits 英文 tag 来表现角色身份与服装，"
        "从 outfits 中挑选最贴合场景的一套（可微调，不要机械堆叠所有服装）。\n"
        "严禁把中文角色名直接写进任何 prompt / character 字段；danbooru 为空时，"
        "靠 appearance 里的英文特征 tag 体现身份。\n\n"
    )
    return header + "\n\n".join(blocks)


class DrawCharacterInjector:
    """before_prompt_build：命中角色时注入角色库信息。"""

    name = "draw.character_inject"
    priority = 40

    async def handle(self, ctx: Any) -> HookResult | None:
        node_id = str(getattr(getattr(ctx, "node", None), "id", "") or "")
        if node_id not in TARGET_NODE_IDS:
            return None
        workspace_root = getattr(getattr(ctx, "rctx", None), "workspace_root", None)
        if workspace_root is None:
            return None
        characters = load_characters(Path(workspace_root))
        if not characters:
            return None

        scan = _norm(_build_scan_text(
            str(ctx.extra.get("instruction_text") or ""), ctx.extra.get("history") or [],
        ))
        if not scan:
            return None
        matched: list[dict[str, Any]] = []
        for ch in characters:
            if _matches(scan, ch):
                matched.append(ch)
                if len(matched) >= _MAX_INJECT_CHARACTERS:
                    break
        if not matched:
            return None

        # 插到开头连续 system 消息之后（历史之前）。
        messages = ctx.messages
        insert_at = 0
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                insert_at = idx + 1
            else:
                break
        messages.insert(insert_at, {"role": "system", "content": build_character_block(matched)})
        logger.info(
            "draw.character_inject: 命中并注入角色 [%s] node=%s",
            ", ".join(str(c.get("name") or "") for c in matched), node_id,
        )
        return HookResult(modified=True)
