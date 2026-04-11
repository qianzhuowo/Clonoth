"""write_file — write a text file (policy + approval guarded)."""
from __future__ import annotations

from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


async def write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = str(args.get("path", ""))
    content = str(args.get("content", ""))

    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": path,
            "content_preview": content[:200],
            "content_len": len(content),
        },
    )
    if err is not None:
        return err

    p = resolve_under_allowed_roots(ctx.workspace_root, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}
