"""Memory management tools: save_memory, list_memories, delete_memory."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..context import ToolContext

_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_BOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def _memory_dir(workspace_root: Path) -> Path:
    return workspace_root / "data" / "memory"


def _load_book(path: Path) -> dict[str, Any]:
    """Load a memory book yaml.  Returns default structure if missing."""
    if not path.exists():
        return {"book": path.stem, "entries": []}
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception:
        return {"book": path.stem, "entries": []}
    if not isinstance(data, dict):
        return {"book": path.stem, "entries": []}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def _save_book(path: Path, data: dict[str, Any]) -> None:
    """Write a memory book yaml back to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    path.write_text(text, encoding="utf-8")


def _invalidate_cache(workspace_root: Path) -> None:
    """Clear the engine-side memory cache so next prompt build picks up changes."""
    try:
        from engine.memory import _MemoryCache
        _MemoryCache.invalidate(workspace_root)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  save_memory
# ---------------------------------------------------------------------------

async def save_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a memory entry in a book."""
    mid = str(args.get("id") or "").strip()
    if not mid:
        return {"ok": False, "error": "empty memory id"}
    if not _MEMORY_ID_RE.fullmatch(mid):
        return {
            "ok": False,
            "error": "invalid id: only [A-Za-z0-9][A-Za-z0-9_.-]{0,127} allowed",
        }

    book = str(args.get("book") or "default").strip()
    if not _BOOK_NAME_RE.fullmatch(book):
        return {"ok": False, "error": "invalid book name"}

    content = str(args.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "empty content"}

    # keywords
    raw_keywords = args.get("keywords")
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        keywords = [
            str(k).strip()
            for k in raw_keywords
            if isinstance(k, str) and str(k).strip()
        ]
    elif isinstance(raw_keywords, str) and raw_keywords.strip():
        keywords = [raw_keywords.strip()]

    constant = bool(args.get("constant", False))
    enabled = bool(args.get("enabled", True))

    # node_ids: 逗号分隔字符串或列表，空 = 全局
    raw_node_ids = args.get("node_ids")
    node_ids: list[str] = []
    if isinstance(raw_node_ids, list):
        node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
    elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
        node_ids = [n.strip() for n in raw_node_ids.split(",") if n.strip()]

    priority = 0
    if args.get("priority") is not None:
        try:
            priority = int(args["priority"])
        except (TypeError, ValueError):
            pass

    scan_depth = 0
    if args.get("scan_depth") is not None:
        try:
            scan_depth = max(0, int(args["scan_depth"]))
        except (TypeError, ValueError):
            pass

    book_path = _memory_dir(ctx.workspace_root) / f"{book}.yaml"
    data = _load_book(book_path)
    data.setdefault("book", book)

    new_entry: dict[str, Any] = {
        "id": mid,
        "content": content,
        "keywords": keywords,
        "constant": constant,
        "enabled": enabled,
        "priority": priority,
        "scan_depth": scan_depth,
    }

    # upsert
    entries = data["entries"]
    found = False
    for i, e in enumerate(entries):
        if isinstance(e, dict) and str(e.get("id") or "").strip() == mid:
            entries[i] = new_entry
            found = True
            break
    if not found:
        entries.append(new_entry)

    _save_book(book_path, data)
    _invalidate_cache(ctx.workspace_root)
    return {"ok": True, "book": book, "id": mid, "updated": found}


# ---------------------------------------------------------------------------
#  list_memories
# ---------------------------------------------------------------------------

async def list_memories(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List memory entries, optionally filtered by book."""
    book_filter = str(args.get("book") or "").strip() or None
    mem_dir = _memory_dir(ctx.workspace_root)
    if not mem_dir.exists():
        return {"ok": True, "entries": []}

    result: list[dict[str, Any]] = []
    for yaml_path in sorted(mem_dir.glob("*.yaml")):
        try:
            data = _load_book(yaml_path)
            bname = str(data.get("book") or yaml_path.stem).strip()
            if book_filter and bname != book_filter:
                continue
            for e in data.get("entries", []):
                if not isinstance(e, dict):
                    continue
                result.append({
                    "book": bname,
                    "id": str(e.get("id") or ""),
                    "content": str(e.get("content") or "")[:200],
                    "keywords": e.get("keywords", []),
                    "constant": bool(e.get("constant", False)),
                    "enabled": bool(e.get("enabled", True)),
                    "priority": int(e.get("priority") or 0),
                    "scan_depth": int(e.get("scan_depth") or 0),
                    "node_ids": e.get("node_ids", []),
                })
        except Exception:
            continue

    return {"ok": True, "entries": result}


# ---------------------------------------------------------------------------
#  delete_memory
# ---------------------------------------------------------------------------

async def delete_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a memory entry from a book."""
    mid = str(args.get("id") or "").strip()
    if not mid:
        return {"ok": False, "error": "empty memory id"}

    book = str(args.get("book") or "default").strip()
    book_path = _memory_dir(ctx.workspace_root) / f"{book}.yaml"
    if not book_path.exists():
        return {"ok": False, "error": f"book not found: {book}"}

    data = _load_book(book_path)
    entries = data.get("entries", [])

    # 禁止删除 constant 记忆
    for e in entries:
        if isinstance(e, dict) and str(e.get("id") or "").strip() == mid:
            if bool(e.get("constant", False)):
                return {"ok": False, "error": f"cannot delete constant memory: {mid}"}
            break

    new_entries = [
        e for e in entries
        if not (isinstance(e, dict) and str(e.get("id") or "").strip() == mid)
    ]
    if len(new_entries) == len(entries):
        return {"ok": False, "error": f"memory not found: {mid}"}

    data["entries"] = new_entries
    if new_entries:
        _save_book(book_path, data)
    else:
        # book empty → remove file
        try:
            book_path.unlink()
        except Exception:
            pass

    _invalidate_cache(ctx.workspace_root)
    return {"ok": True, "book": book, "id": mid, "deleted": True}
