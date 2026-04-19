from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict, load_runtime_config, resolve_env_ref


@dataclass
class ToolAccess:
    mode: str = "none"  # "none" | "all" | "allowlist"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class SkillAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class MemoryAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class Node:
    id: str
    type: str  # "ai" | "tool"
    name: str = ""
    description: str = ""
    model: str = ""          # 模型名称，空 = 使用全局默认
    api_key: str = ""        # 可选独立 API key（支持 $ENV{} 语法）
    base_url: str = ""       # 可选独立 base URL（支持 $ENV{} 语法）
    prompt: str | list = ""  # system prompt：字符串模式或 block 列表模式
    tool_access: ToolAccess = field(default_factory=ToolAccess)
    skill_access: SkillAccess = field(default_factory=SkillAccess)
    memory_access: MemoryAccess = field(default_factory=MemoryAccess)
    tool_mode: str = "native"  # "native" | "json"
    # hybrid output_mode: 纯文本输出不再 reject 重试，直接作为隐式 finish 投递给用户。
    # tool_only = 现有行为（强制 finish）；hybrid = 允许纯文本直接投递。
    output_mode: str = "tool_only"  # "tool_only" | "hybrid"
    delegate_targets: list[str] = field(default_factory=list)


def load_node(workspace_root: Path, node_id: str) -> Node | None:
    nid = (node_id or "").strip()
    if not nid:
        return None
    data = load_yaml_dict(workspace_root / "config" / "nodes" / f"{nid}.yaml")
    if not isinstance(data, dict):
        return None
    # kind 默认 "node"，允许省略
    if str(data.get("kind") or "node").strip() != "node":
        return None
    node_type = str(data.get("type") or "ai").strip().lower()
    if node_type not in {"ai", "tool"}:
        return None

    # prompt：字符串→字符串模式，列表→block 列表模式
    raw_prompt = data.get("prompt")
    if isinstance(raw_prompt, list):
        prompt = raw_prompt
    elif isinstance(raw_prompt, str):
        prompt = raw_prompt.strip()
    else:
        prompt = ""

    # model：直接值或 $ENV{} 引用
    raw_model = str(data.get("model") or "").strip()
    model = resolve_env_ref(raw_model) if raw_model else ""

    # 可选独立 api_key / base_url
    raw_api_key = str(data.get("api_key") or "").strip()
    api_key = resolve_env_ref(raw_api_key) if raw_api_key else ""
    raw_base_url = str(data.get("base_url") or "").strip()
    base_url = resolve_env_ref(raw_base_url) if raw_base_url else ""

    ta_raw = data.get("tool_access")
    if isinstance(ta_raw, str):
        m = ta_raw.strip().lower()
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none")
    elif isinstance(ta_raw, dict):
        m = str(ta_raw.get("mode") or "none").strip().lower()
        allow = [
            str(x).strip()
            for x in (ta_raw.get("allow") or [])
            if isinstance(x, str) and x.strip()
        ]
        deny = [
            str(x).strip()
            for x in (ta_raw.get("deny") or [])
            if isinstance(x, str) and x.strip()
        ]
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none", allow=allow, deny=deny)
    else:
        ta = ToolAccess()

    sa_raw = data.get("skills")
    if isinstance(sa_raw, str):
        sm = sa_raw.strip().lower()
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all")
    elif isinstance(sa_raw, dict):
        sm = str(sa_raw.get("mode") or "all").strip().lower()
        sa_allow = [str(x).strip() for x in (sa_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all", allow=sa_allow)
    else:
        sa = SkillAccess()

    ma_raw = data.get("memories")
    if isinstance(ma_raw, str):
        mm = ma_raw.strip().lower()
        ma = MemoryAccess(mode=mm if mm in {"all", "allowlist", "none"} else "all")
    elif isinstance(ma_raw, dict):
        mm = str(ma_raw.get("mode") or "all").strip().lower()
        ma_allow = [str(x).strip() for x in (ma_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        ma = MemoryAccess(mode=mm if mm in {"all", "allowlist", "none"} else "all", allow=ma_allow)
    else:
        ma = MemoryAccess()

    # tool_mode — 节点 yaml 优先，否则用 runtime.yaml engine.tool_mode 全局默认
    _node_tm = data.get("tool_mode")
    if _node_tm is not None:
        raw_tool_mode = str(_node_tm).strip().lower()
    else:
        _rt = load_runtime_config(workspace_root)
        raw_tool_mode = str((_rt.get("engine") or {}).get("tool_mode") or "native").strip().lower()
    tool_mode = raw_tool_mode if raw_tool_mode in {"native", "json"} else "native"

    # output_mode — 节点 yaml 优先，否则用 runtime.yaml engine.output_mode 全局默认
    # hybrid 模式下纯文本输出直接投递给用户，不 reject 不重试（RFC: rfc_hybrid_output_mode.md）
    _node_om = data.get("output_mode")
    if _node_om is not None:
        raw_output_mode = str(_node_om).strip().lower()
    else:
        _rt_om = load_runtime_config(workspace_root)
        raw_output_mode = str((_rt_om.get("engine") or {}).get("output_mode") or "tool_only").strip().lower()
    output_mode = raw_output_mode if raw_output_mode in {"tool_only", "hybrid"} else "tool_only"

    # delegate_targets
    dt_raw = data.get("delegate_targets")
    delegate_targets: list[str] = [
        str(x).strip() for x in (dt_raw or []) if isinstance(x, str) and x.strip()
    ] if isinstance(dt_raw, list) else []

    return Node(
        id=str(data.get("id") or nid).strip(),
        type=node_type,
        name=str(data.get("name") or nid).strip(),
        description=str(data.get("description") or "").strip(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        prompt=prompt,
        tool_access=ta,
        skill_access=sa,
        memory_access=ma,
        tool_mode=tool_mode,
        output_mode=output_mode,
        delegate_targets=delegate_targets,
    )
