from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict, resolve_env_ref


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
class Node:
    id: str
    type: str  # "ai" | "tool"
    name: str = ""
    description: str = ""
    model: str = ""          # 模型名称，空 = 使用全局默认
    api_key: str = ""        # 可选独立 API key（支持 $ENV{} 语法）
    base_url: str = ""       # 可选独立 base URL（支持 $ENV{} 语法）
    prompt: str = ""         # 完整 system prompt 模板（支持 {{var}} 和 {{include:file}} ）
    tool_access: ToolAccess = field(default_factory=ToolAccess)
    skill_access: SkillAccess = field(default_factory=SkillAccess)
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

    # prompt：字符串直接用，dict 兼容旧格式（忽略）
    raw_prompt = data.get("prompt")
    prompt = str(raw_prompt).strip() if isinstance(raw_prompt, str) else ""

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
        delegate_targets=delegate_targets,
    )
