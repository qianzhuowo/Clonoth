"""list_dir — list one or more directories under workspace root.

Supports batch listing via ``paths`` array, optional recursive mode.
Directories are listed first, then files, each group sorted by name.
Ignores ``.git`` by default.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import resolve_under_root


# Default directories to ignore
_DEFAULT_IGNORE_DIRS = frozenset({".git"})


def _should_ignore(rel_parts: tuple[str, ...], ignore_dirs: frozenset[str]) -> bool:
    """Return True if any path component is in the ignore set."""
    return any(part in ignore_dirs for part in rel_parts)


def _list_entries_flat(
    base: Path,
    ignore_dirs: frozenset[str],
) -> tuple[list[dict[str, str]], int, int]:
    """Non-recursive: list immediate children. Returns (entries, fileCount, dirCount)."""
    dirs: list[str] = []
    files: list[str] = []

    try:
        children = sorted(base.iterdir(), key=lambda c: c.name)
    except Exception:
        children = []

    for child in children:
        if child.name in ignore_dirs:
            continue
        if child.is_dir():
            dirs.append(child.name + "/")
        elif child.is_file():
            files.append(child.name)

    entries: list[dict[str, str]] = []
    for d in dirs:
        entries.append({"name": d, "type": "directory"})
    for f in files:
        entries.append({"name": f, "type": "file"})

    return entries, len(files), len(dirs)


def _list_entries_recursive(
    base: Path,
    ignore_dirs: frozenset[str],
) -> tuple[list[dict[str, str]], int, int]:
    """Recursive: walk tree. Returns (entries, fileCount, dirCount)."""
    dirs: list[str] = []
    files: list[str] = []

    for child in sorted(base.rglob("*")):
        try:
            rel_parts = child.relative_to(base).parts
        except ValueError:
            continue
        if _should_ignore(rel_parts, ignore_dirs):
            continue

        rel_posix = child.relative_to(base).as_posix()
        if child.is_dir():
            dirs.append(rel_posix + "/")
        elif child.is_file():
            files.append(rel_posix)

    entries: list[dict[str, str]] = []
    for d in sorted(dirs):
        entries.append({"name": d, "type": "directory"})
    for f in sorted(files):
        entries.append({"name": f, "type": "file"})

    return entries, len(files), len(dirs)


async def list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # ---- Parse paths parameter ----
    paths_arg = args.get("paths")
    if isinstance(paths_arg, list) and paths_arg:
        dir_paths = [str(p).strip() for p in paths_arg if isinstance(p, str) and str(p).strip()]
    else:
        # Legacy single-path mode
        dir_paths = [str(args.get("path", "."))]

    if not dir_paths:
        return {
            "ok": False, "success": False, "error": "no paths specified",
            "data": {"results": [], "totalFiles": 0, "totalDirs": 0, "totalPaths": 0},
        }

    recursive = bool(args.get("recursive", False))
    ignore_dirs = _DEFAULT_IGNORE_DIRS

    # ---- Iterate directories ----
    results: list[dict[str, Any]] = []
    total_files = 0
    total_dirs = 0

    for dir_path in dir_paths:
        try:
            p = resolve_under_root(ctx.workspace_root, dir_path)
        except ValueError as exc:
            results.append({
                "path": dir_path, "success": False, "error": str(exc),
                "entries": [], "fileCount": 0, "dirCount": 0,
            })
            continue

        if not p.exists():
            results.append({
                "path": dir_path, "success": False, "error": "File not found",
                "entries": [], "fileCount": 0, "dirCount": 0,
            })
            continue

        if not p.is_dir():
            results.append({
                "path": dir_path, "success": False, "error": "Not a directory",
                "entries": [], "fileCount": 0, "dirCount": 0,
            })
            continue

        if recursive:
            entries, fc, dc = _list_entries_recursive(p, ignore_dirs)
        else:
            entries, fc, dc = _list_entries_flat(p, ignore_dirs)

        results.append({
            "path": dir_path, "success": True,
            "entries": entries, "fileCount": fc, "dirCount": dc,
        })
        total_files += fc
        total_dirs += dc

    # ---- Build response ----
    all_ok = all(r.get("success") for r in results)
    response: dict[str, Any] = {
        "ok": all_ok,
        "success": all_ok,
        "data": {
            "results": results,
            "totalFiles": total_files,
            "totalDirs": total_dirs,
            "totalPaths": len(dir_paths),
        },
    }
    if not all_ok:
        fail_count = sum(1 for r in results if not r.get("success"))
        response["error"] = f"Some directories failed to list" if fail_count > 1 else results[next(i for i, r in enumerate(results) if not r.get("success"))].get("error", "unknown")

    # Backward compat: single path → set top-level path/items
    if len(dir_paths) == 1 and results:
        r0 = results[0]
        response["path"] = r0.get("path", "")
        if r0.get("success"):
            response["items"] = r0.get("entries", [])

    return response
