"""布局 → 消息模板 → 实际消息（对应 LWB scene-planner-assembly.js + scene-planner.js 的组装段）。

顺序：leading 自定义 → system(opening+frame+tools) → setup 后自定义 → user REQUIRED ACTION
→ assistant read_scene_materials / tool 返回(content) → materials 后自定义
→ assistant read_drawing_guide / tool 返回(guide+rules+limits) → guide 后自定义
→ assistant request_draw_approval / tool APPROVED → afterApproval 自定义。

tools 块只放 TOOL_PROTOCOL_MARKER：原生协议时整段删掉；文本协议时由协议层替换成工具说明。
"""
from __future__ import annotations

from typing import Any, Mapping

from .handoff import SCENE_PLANNER_ACTION, build_exchange, render_message
from .layout import BUILTIN_BY_ID, is_builtin_id, project_layout

SEPARATOR = "\n\n"
TOOL_PROTOCOL_MARKER = "\u2063CLONOTH_DRAW_TOOL_PROTOCOL\u2063"


def _remove_marker(text: Any) -> str:
    return str(text if text is not None else "").replace(TOOL_PROTOCOL_MARKER, "")


def _block_text(block: Mapping[str, Any], builtin_texts: Mapping[str, Any]) -> str:
    bid = block.get("id")
    if is_builtin_id(bid):
        if bid == "tools":
            return TOOL_PROTOCOL_MARKER
        return _remove_marker(builtin_texts.get(bid)).strip()
    return _remove_marker(block.get("content")).strip()


def plan_messages(layout: Any, builtin_texts: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """返回模板项列表：{role, template, blockIds, tool_calls?, tool_call_id?, approved?, stageId?}。"""
    texts: Mapping[str, Any] = builtin_texts if isinstance(builtin_texts, Mapping) else {}
    templates: list[dict[str, Any]] = []

    def append_block(block: Mapping[str, Any]) -> None:
        builtin = is_builtin_id(block.get("id"))
        template = _block_text(block, texts)
        if not template or (not builtin and block.get("enabled") is False):
            return
        role = BUILTIN_BY_ID[block["id"]].role if builtin else block.get("role", "user")
        last = templates[-1] if templates else None
        if last is not None and last["role"] == role and last["blockIds"] and not last.get("tool_calls"):
            last["template"] += SEPARATOR + template
            last["blockIds"].append(block["id"])
        else:
            templates.append({"role": role, "template": template, "blockIds": [block["id"]]})

    stages = project_layout(layout)
    for stage in stages:
        for block in stage.leading_blocks:
            append_block(block)
        if stage.id == "setup":
            for block in stage.blocks:
                if block["id"] in stage.block_ids:
                    append_block(block)
        else:
            if stage.id == "materials":
                templates.append({"role": "user", "template": SCENE_PLANNER_ACTION, "blockIds": []})
            document = SEPARATOR.join(
                part for part in (_block_text({"id": bid}, texts) for bid in stage.block_ids) if part
            )
            for item in build_exchange(stage.id, document, list(stage.block_ids)):
                item["stageId"] = stage.id
                templates.append(item)
        for block in stage.custom_blocks:
            append_block(block)
    for item in build_exchange("approval"):
        item["stageId"] = "approval"
        templates.append(item)
    for block in stages[-1].trailing_blocks:
        append_block(block)
    return templates


def _strip_marker_from_text(text: str, tool_protocol_text: str | None) -> str:
    if tool_protocol_text:
        return text.replace(TOOL_PROTOCOL_MARKER, tool_protocol_text)
    parts = [p.strip() for p in text.split(TOOL_PROTOCOL_MARKER)]
    return SEPARATOR.join(p for p in parts if p)


def render_messages(templates: list[dict[str, Any]], *, tool_protocol_text: str | None = None) -> list[dict[str, Any]]:
    """模板 → 实际消息。tool_protocol_text 为空（原生协议）时 tools 标记整段删除；
    文本协议时替换成工具说明。非 tool 角色且内容为空的消息被丢弃。"""
    messages: list[dict[str, Any]] = []
    for item in templates:
        content = _strip_marker_from_text(str(item.get("template") or ""), tool_protocol_text)
        if item["role"] != "tool" and not content.strip() and not item.get("tool_calls"):
            continue
        messages.append(render_message(item, content))
    return messages


def build_planner_messages(layout: Any, builtin_texts: Mapping[str, Any] | None, *,
                           tool_protocol_text: str | None = None) -> list[dict[str, Any]]:
    return render_messages(plan_messages(layout, builtin_texts), tool_protocol_text=tool_protocol_text)


SECTION_KEYS = {
    "opening": "topSystem",
    "frame": "frame",
    "tools": "toolDeclaration",
    "content": "content",
    "guide": "tagGuide",
    "rules": "sceneRules",
    "limits": "limits",
}


def build_chain_preview(layout: Any, builtin_texts: Mapping[str, Any] | None, *,
                        tool: Mapping[str, Any] | None = None,
                        field_descriptions: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """面板预览：[{role, roleLabel, key, stageId, summary, sections:[{key, summary, content, builtin, fixed?, rendered?}]},
    …, {role:'tools', fields, tool}]。
    顺序与实际发送一致（复用 plan_messages）。"""
    from .layout import PROMPT_ROLE_LABELS, describe_block

    texts: Mapping[str, Any] = builtin_texts if isinstance(builtin_texts, Mapping) else {}
    custom_by_id = {b["id"]: b for b in _custom_blocks(layout)}
    chain: list[dict[str, Any]] = []
    for index, item in enumerate(plan_messages(layout, texts)):
        sections: list[dict[str, Any]] = []
        for bid in item.get("blockIds") or []:
            if is_builtin_id(bid):
                label = BUILTIN_BY_ID[bid].label
                content = "" if bid == "tools" else str(texts.get(bid) or "")
                sections.append({"key": SECTION_KEYS[bid], "summary": label, "content": content, "builtin": True})
            else:
                block = custom_by_id.get(bid, {"id": bid})
                sections.append({"key": bid, "summary": describe_block(block)["label"],
                                 "content": str(block.get("content") or ""), "builtin": False})
        if not sections:
            summary = "预先构建的提交确认" if item.get("stageId") == "approval" else (
                "预构建调用" if item.get("tool_calls") else "用户消息")
            fixed: dict[str, Any] = {"key": f"message-{index}", "summary": summary,
                                     "content": str(item.get("template") or ""), "builtin": True, "fixed": True}
            if item["role"] == "tool":
                # 工具返回实际发出去的是 JSON 包装；面板的“预览”显示它，行内摘要仍用 content。
                fixed["rendered"] = render_message(item, fixed["content"])["content"]
            sections.append(fixed)
        chain.append({
            "role": item["role"],
            "roleLabel": PROMPT_ROLE_LABELS.get(item["role"], item["role"]),
            "key": f"message-{index}",
            "stageId": item.get("stageId"),
            "summary": " + ".join(s["summary"] for s in sections),
            "sections": sections,
        })
    if tool is not None:
        chain.append({
            "role": "tools",
            "roleLabel": PROMPT_ROLE_LABELS["tools"],
            "key": "tools",
            "summary": "工具定义与字段说明",
            "fields": list(field_descriptions or []),
            "tool": dict(tool),
        })
    return chain


def _custom_blocks(layout: Any) -> list[dict[str, Any]]:
    from .layout import custom_blocks_of
    return custom_blocks_of(layout)
