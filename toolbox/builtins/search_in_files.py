"""search_in_files — substring search in workspace files."""
from __future__ import annotations

from typing import Any

from clonoth_runtime import get_int, load_runtime_config

from ..context import ToolContext
from .._common import resolve_under_root


async def search_in_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query", ""))
    rel_path = str(args.get("path", "."))

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    max_file_size_bytes = get_int(
        runtime_cfg,
        "meta.search.max_file_size_bytes",
        2_000_000,
        min_value=100_000,
        max_value=50_000_000,
    )
    max_matches = get_int(runtime_cfg, "meta.search.max_matches", 50, min_value=1, max_value=5000)

    if not query:
        return {"ok": False, "error": "empty query"}

    root = resolve_under_root(ctx.workspace_root, rel_path)
    if not root.exists():
        return {"ok": False, "error": "path not found", "path": rel_path}

    matches: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.endswith((".pyc",)):
            continue
        try:
            if p.stat().st_size > max_file_size_bytes:
                continue
        except Exception:
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if query in text:
            rel = p.relative_to(ctx.workspace_root).as_posix()
            matches.append({"path": rel})
            if len(matches) >= max_matches:
                break

    return {"ok": True, "query": query, "matches": matches}
