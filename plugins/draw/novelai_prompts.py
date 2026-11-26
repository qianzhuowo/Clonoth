"""draw.novelai_planner 的 NovelAI 提示词模板注入。

替代旧节点 YAML 里的三条 ``{{include:drawtools/prompts/novelai/*.md}}``（通用
include 只解析 config/nodes/，不为 drawtools 开特例）。按旧 prompt 的结构与顺序
输出“## 绘图提示词模板 / ### Top System / ### TAG Guide / ### Scene Rules”，
作为一条静态 system 消息（prompt_sections static 作用域，缓存友好，位于系统提示
之后、历史之前）。文件按 drawtools 规则读取：私有 ``<name>.md`` 优先，缺失回退
``<name>.example.md``；只读，不创建用户文件。
"""
from __future__ import annotations

import logging
from typing import Any

from .paths import drawtools_file

logger = logging.getLogger(__name__)

SECTION_NAME = "draw.novelai_prompts"
TARGET_NODE_ID = "draw.novelai_planner"

# (标题, drawtools 下相对路径)，顺序即 planner 消息链中的顺序。TAG Guide 那一行按默认预设的模型选（V5 用 tag-guide-v5.md）。
NOVELAI_PROMPT_PARTS: tuple[tuple[str, str], ...] = (
    ("Top System", "prompts/novelai/top-system.md"),
    ("TAG Guide", "prompts/novelai/tag-guide.md"),
    ("Scene Rules", "prompts/novelai/scene-rules.md"),
)
V5_GUIDE_PATH = "prompts/novelai/tag-guide-v5.md"


def _prompt_parts(workspace_root: Any) -> tuple[tuple[str, str], ...]:
    try:
        from .agent.config import guide_key_for_preset, load_draw_settings, selected_preset

        if guide_key_for_preset(selected_preset(load_draw_settings(workspace_root))) != "guide_v5":
            return NOVELAI_PROMPT_PARTS
    except Exception:  # noqa: BLE001 - 设置读不到时照旧用 V4.5 指南
        return NOVELAI_PROMPT_PARTS
    return tuple((title, V5_GUIDE_PATH if title == "TAG Guide" else rel) for title, rel in NOVELAI_PROMPT_PARTS)


def render_novelai_prompts(ctx: Any) -> dict[str, str] | None:
    """prompt section render：仅对 draw.novelai_planner 返回模板消息。"""
    node_id = str(getattr(getattr(ctx, "node", None), "id", "") or "")
    if node_id != TARGET_NODE_ID:
        return None
    workspace_root = getattr(ctx, "workspace_root", None)
    if workspace_root is None:
        return None
    blocks: list[str] = []
    for title, rel_path in _prompt_parts(workspace_root):
        path = drawtools_file(workspace_root, rel_path)
        if path is None:
            logger.warning("draw.novelai_prompts: 缺少 %s（私有文件与 example 模板都不存在）", rel_path)
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            blocks.append(f"### {title}\n{text}")
    if not blocks:
        return None
    return {"role": "system", "content": "## 绘图提示词模板\n\n" + "\n\n".join(blocks)}
