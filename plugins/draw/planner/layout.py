"""请求块布局：固定三阶段 + 自定义消息（对应 LWB scene-planner-layout.js 的纯函数子集）。

布局 = 内置阶段（次序由本文件统一定义）+ 自定义消息；自定义消息只能放在阶段之前、
之间或之后（阶段内部不能插入），排在"提交确认"之后的标 ``afterApproval``。
稀疏存储：预设只有在布局偏离默认时才带 layout 字段；内置块只存 ``{id}``。
本项目不移植"附带工具代码"与世界书块。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

LAYOUT_VERSION = 2
APPROVAL_MARKER_ID = "approval"


@dataclass(frozen=True)
class BuiltinBlock:
    id: str
    label: str
    role: str
    required: bool = False


BUILTIN_BLOCKS: tuple[BuiltinBlock, ...] = (
    BuiltinBlock("opening", "开场与视角", "system"),
    BuiltinBlock("frame", "材料说明与输出去向", "system"),
    BuiltinBlock("tools", "可用工具", "system"),
    BuiltinBlock("content", "角色与绘图请求", "tool", required=True),
    BuiltinBlock("guide", "TAG指南", "tool"),
    BuiltinBlock("rules", "场景规则", "tool"),
    BuiltinBlock("limits", "数量约束", "tool", required=True),
)


@dataclass(frozen=True)
class Stage:
    id: str
    label: str
    block_ids: tuple[str, ...]


STAGES: tuple[Stage, ...] = (
    Stage("setup", "角色与工具", ("opening", "frame", "tools")),
    Stage("materials", "知识材料", ("content",)),
    Stage("guide", "绘图规范", ("guide", "rules", "limits")),
)

BUILTIN_BLOCK_IDS: tuple[str, ...] = tuple(bid for stage in STAGES for bid in stage.block_ids)
BUILTIN_BY_ID: dict[str, BuiltinBlock] = {block.id: block for block in BUILTIN_BLOCKS}
PROMPT_ROLE_LABELS = {"system": "系统设定", "user": "用户消息", "assistant": "模型消息", "tool": "工具返回", "tools": "工具定义"}
CUSTOM_BLOCK_ROLES: tuple[str, ...] = ("system", "user", "assistant")
_STAGE_BY_BLOCK: dict[str, int] = {bid: i for i, stage in enumerate(STAGES) for bid in stage.block_ids}
_CUSTOM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CUSTOM_ID_PREFIX = "msg-"


def is_builtin_id(block_id: Any) -> bool:
    return isinstance(block_id, str) and block_id in BUILTIN_BY_ID


def create_default_layout() -> dict[str, Any]:
    return {"version": LAYOUT_VERSION, "blocks": [{"id": bid} for bid in BUILTIN_BLOCK_IDS]}


def _coerce_text(value: Any) -> tuple[str, bool]:
    """返回 (文本, 是否被强制转换)。对象 / 数组 → ''。"""
    if value is None:
        return "", False
    if isinstance(value, str):
        return value, False
    if isinstance(value, (bool, int, float)):
        return str(value), True
    return "", True


def _next_free_id(used: set[str], counter: list[int]) -> str:
    while True:
        counter[0] += 1
        candidate = f"{_CUSTOM_ID_PREFIX}{counter[0]:06d}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def _fix_stage_order(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内置块归位；自定义消息按最近经过的阶段安置（leading / 各阶段之后 / trailing）。"""
    leading: list[dict[str, Any]] = []
    trailing: list[dict[str, Any]] = []
    customs: list[list[dict[str, Any]]] = [[] for _ in STAGES]
    stage_index = -1
    last_custom_stage = -1
    for block in blocks:
        bid = block["id"]
        if bid in _STAGE_BY_BLOCK:
            stage_index = _STAGE_BY_BLOCK[bid]
        elif block.get("afterApproval"):
            trailing.append(block)
        else:
            last_custom_stage = max(last_custom_stage, stage_index)
            (leading if last_custom_stage < 0 else customs[last_custom_stage]).append(block)
    ordered: list[dict[str, Any]] = list(leading)
    for index, stage in enumerate(STAGES):
        ordered.extend({"id": bid} for bid in stage.block_ids)
        ordered.extend(customs[index])
    ordered.extend(trailing)
    return ordered


@dataclass
class NormalizedLayout:
    layout: dict[str, Any]
    repaired: bool = False
    notes: list[str] = field(default_factory=list)


def normalize_layout(raw: Any) -> NormalizedLayout:
    """任何来历的数据 → 规范布局；修复项记入 notes。None → 默认布局。"""
    if raw is None:
        return NormalizedLayout(create_default_layout())
    notes: list[str] = []
    if isinstance(raw, list):
        raw_blocks: Any = raw
        notes.append("布局缺少 version，按旧格式读取")
    elif isinstance(raw, Mapping):
        version = raw.get("version")
        if version not in (1, 2):
            notes.append(f"布局 version={version!r} 不受支持，已尽力读取")
        raw_blocks = raw.get("blocks")
    else:
        return NormalizedLayout(create_default_layout(), True, ["布局不是对象，已恢复默认"])
    if not isinstance(raw_blocks, list):
        return NormalizedLayout(create_default_layout(), True, notes + ["布局缺少 blocks 数组，已恢复默认"])

    used_ids: set[str] = set()
    counter = [0]
    seen_builtin: set[str] = set()
    blocks: list[dict[str, Any]] = []
    for entry in raw_blocks:
        if not isinstance(entry, Mapping):
            notes.append("丢弃了一个非对象的块")
            continue
        bid = entry.get("id")
        if is_builtin_id(bid):
            if bid in seen_builtin:
                notes.append(f"内置块 {bid} 重复，只保留首次")
                continue
            seen_builtin.add(bid)
            blocks.append({"id": bid})
            continue
        role = entry.get("role")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            notes.append("自定义消息 enabled 不是布尔值，已按启用处理" if enabled else "自定义消息 enabled 不是布尔值，已按停用处理")
            enabled = bool(enabled)
        if role not in CUSTOM_BLOCK_ROLES:
            notes.append(f"自定义消息角色 {role!r} 无效，已改为 user 并停用")
            role = "user"
            enabled = False
        name, name_coerced = _coerce_text(entry.get("name"))
        content, content_coerced = _coerce_text(entry.get("content"))
        if name_coerced or content_coerced:
            notes.append("自定义消息的名称或内容不是文本，已转换")
        block: dict[str, Any] = {
            "id": "",
            "role": role,
            "name": name.strip(),
            "content": content,
            "enabled": enabled,
        }
        if entry.get("afterApproval") is True:
            block["afterApproval"] = True
        valid_id = isinstance(bid, str) and bool(_CUSTOM_ID_PATTERN.match(bid)) and bid != APPROVAL_MARKER_ID
        if valid_id and bid not in used_ids:
            used_ids.add(bid)
            block["id"] = bid
        else:
            block["id"] = _next_free_id(used_ids, counter)
            notes.append("自定义消息 id 非法或重复，已重新生成")
        blocks.append(block)
    missing = [bid for bid in BUILTIN_BLOCK_IDS if bid not in seen_builtin]
    if missing:
        notes.append(f"补回缺失的内置块：{', '.join(missing)}")
    ordered = _fix_stage_order(blocks)
    layout = {"version": LAYOUT_VERSION, "blocks": ordered}
    return NormalizedLayout(layout, bool(notes), notes)


def is_default_layout(raw: Any) -> bool:
    return normalize_layout(raw).layout == create_default_layout()


def layout_for_storage(raw: Any) -> dict[str, Any] | None:
    """规范化后等于默认 → None（稀疏存储，不写 layout 字段）。"""
    normalized = normalize_layout(raw).layout
    return None if normalized == create_default_layout() else normalized


@dataclass
class ProjectedStage:
    id: str
    label: str
    block_ids: tuple[str, ...]
    leading_blocks: list[dict[str, Any]]
    trailing_blocks: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    custom_blocks: list[dict[str, Any]]


def project_layout(raw: Any) -> list[ProjectedStage]:
    """编辑器和组装器共用的阶段投影；第一阶段之前的在 leading，提交确认之后的在最后阶段的 trailing。"""
    blocks = normalize_layout(raw).layout["blocks"]
    first_trailing = next((i for i, b in enumerate(blocks) if b.get("afterApproval")), -1)
    stages_end = len(blocks) if first_trailing == -1 else first_trailing
    projected: list[ProjectedStage] = []
    for index, stage in enumerate(STAGES):
        start = next(i for i, b in enumerate(blocks) if b["id"] == stage.block_ids[0])
        last = index + 1 == len(STAGES)
        if last:
            end = stages_end
        else:
            end = next(i for i, b in enumerate(blocks) if b["id"] == STAGES[index + 1].block_ids[0])
        projected.append(ProjectedStage(
            id=stage.id,
            label=stage.label,
            block_ids=stage.block_ids,
            leading_blocks=blocks[:start] if index == 0 else [],
            trailing_blocks=blocks[stages_end:] if last else [],
            blocks=blocks[start:end],
            custom_blocks=blocks[start + len(stage.block_ids):end],
        ))
    return projected


def describe_block(block: Mapping[str, Any]) -> dict[str, Any]:
    bid = block.get("id")
    if is_builtin_id(bid):
        builtin = BUILTIN_BY_ID[bid]
        return {"id": bid, "builtin": True, "role": builtin.role, "label": builtin.label}
    return {"id": bid, "builtin": False, "role": block.get("role", "user"),
            "label": str(block.get("name") or "").strip() or "自定义消息"}


def custom_blocks_of(raw: Any) -> list[dict[str, Any]]:
    return [b for b in normalize_layout(raw).layout["blocks"] if not is_builtin_id(b.get("id"))]


def builtin_block_ids_in_order(raw: Any) -> Sequence[str]:
    return [b["id"] for b in normalize_layout(raw).layout["blocks"] if is_builtin_id(b["id"])]
