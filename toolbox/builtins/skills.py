"""Skill management tools: create_or_update_skill, list_skills, delete_skill."""
from __future__ import annotations

import re
import shutil
from typing import Any

import yaml

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots
from ..skills_runtime import parse_skill_frontmatter
from . import SKILL_NAME_RE


async def create_or_update_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    content = args.get("content")
    enabled = bool(args.get("enabled", True))

    strategy = str(args.get("strategy", "")).strip().lower() or None
    raw_keywords = args.get("keywords")
    keywords: list[str] | None = None
    if isinstance(raw_keywords, list):
        keywords = [str(k).strip() for k in raw_keywords if isinstance(k, str) and str(k).strip()]

    order: int | None = None
    if args.get("order") is not None:
        try:
            order = int(args["order"])
        except (TypeError, ValueError):
            pass
    priority: int | None = None
    if args.get("priority") is not None:
        try:
            priority = int(args["priority"])
        except (TypeError, ValueError):
            pass
    scan_depth: int | None = None
    if args.get("scan_depth") is not None:
        try:
            scan_depth = max(0, int(args["scan_depth"]))
        except (TypeError, ValueError):
            pass

    if not name:
        return {"ok": False, "error": "empty skill name"}
    if not SKILL_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "invalid skill name: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed"}

    path = f"skills/{name}/SKILL.md"
    if not isinstance(content, str) or not content.strip():
        meta: dict[str, Any] = {
            "name": name,
            "description": description,
            "enabled": enabled,
        }
        if strategy:
            meta["strategy"] = strategy
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        body = description or f"Skill {name}"
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + body.strip() + "\n"
    else:
        meta, body = parse_skill_frontmatter(content)
        if not isinstance(meta, dict):
            meta = {}
        meta["name"] = name
        if description:
            meta["description"] = description
        elif not isinstance(meta.get("description"), str):
            meta["description"] = ""
        meta["enabled"] = enabled
        if strategy:
            meta["strategy"] = strategy
        if keywords is not None:
            meta["keywords"] = keywords
        if order is not None:
            meta["order"] = order
        if priority is not None:
            meta["priority"] = priority
        if scan_depth is not None:
            meta["scan_depth"] = scan_depth
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + str(body or "").strip() + "\n"

    # Reuse write_file logic
    from .write_file import write_file
    res = await write_file({"path": path, "content": content}, ctx)
    if not res.get("ok"):
        return res
    return {"ok": True, "path": path, "name": name, "enabled": enabled}


async def list_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    skills_dir = ctx.workspace_root / "skills"
    if not skills_dir.exists():
        return {"ok": True, "skills": []}

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            rel = skill_md.relative_to(ctx.workspace_root).as_posix()
            text = skill_md.read_text(encoding="utf-8")
            meta, _body = parse_skill_frontmatter(text)
            if not isinstance(meta, dict):
                meta = {}
            strategy = str(meta.get("strategy") or "normal").strip().lower()
            if strategy not in ("constant", "normal"):
                strategy = "normal"
            raw_kw = meta.get("keywords")
            kw_list: list[str] = []
            if isinstance(raw_kw, list):
                kw_list = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]
            item_order = 0
            if isinstance(meta.get("order"), (int, float)):
                item_order = int(meta["order"])
            item_priority = 0
            if isinstance(meta.get("priority"), (int, float)):
                item_priority = int(meta["priority"])
            item_scan_depth = 0
            if isinstance(meta.get("scan_depth"), (int, float)):
                item_scan_depth = max(0, int(meta["scan_depth"]))
            items.append(
                {
                    "name": str(meta.get("name") or skill_md.parent.name),
                    "description": str(meta.get("description") or ""),
                    "enabled": bool(meta.get("enabled", True)),
                    "strategy": strategy,
                    "keywords": kw_list,
                    "order": item_order,
                    "priority": item_priority,
                    "scan_depth": item_scan_depth,
                    "path": rel,
                }
            )
        except Exception as e:
            items.append({"name": skill_md.parent.name, "path": skill_md.relative_to(ctx.workspace_root).as_posix(), "error": str(e)})

    return {"ok": True, "skills": items}


async def delete_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(args.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "empty skill name"}
    if not SKILL_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "invalid skill name"}

    skill_dir = resolve_under_allowed_roots(ctx.workspace_root, f"skills/{name}")
    if not skill_dir.exists():
        return {"ok": False, "error": f"skill not found: {name}"}
    if not skill_dir.is_dir():
        return {"ok": False, "error": f"not a skill directory: {name}"}

    _op, err = await request_guard(ctx, "write_file", {"path": f"skills/{name}/SKILL.md", "delete": True})
    if err is not None:
        return err

    shutil.rmtree(skill_dir)
    return {"ok": True, "deleted": True, "name": name}
