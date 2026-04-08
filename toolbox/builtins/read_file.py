"""read_file — read one or more files (text / image / binary).

Supports batch reading via ``files`` array parameter.
Backward-compatible with legacy single-file ``path`` parameter.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


# ---------------------------------------------------------------------------
#  Image dimension helpers (pure stdlib, no Pillow needed)
# ---------------------------------------------------------------------------

def _make_dimensions(w: int, h: int) -> dict[str, Any]:
    from math import gcd
    g = gcd(w, h) if w > 0 and h > 0 else 1
    return {"width": w, "height": h, "aspectRatio": f"{w // g}:{h // g}"}


def _parse_jpeg_dimensions(data: bytes) -> dict[str, Any] | None:
    import struct
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 9 <= len(data):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return _make_dimensions(w, h)
            break
        if marker in (0xD9, 0xDA):
            break
        if i + 4 <= len(data):
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len
        else:
            break
    return None


def _get_image_dimensions(p: Path) -> dict[str, Any] | None:
    try:
        import struct
        data = p.read_bytes()
        ext = p.suffix.lower()
        if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return _make_dimensions(w, h)
        if ext in (".jpg", ".jpeg") and len(data) >= 2 and data[:2] == b"\xff\xd8":
            return _parse_jpeg_dimensions(data)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
#  Single-file reader
# ---------------------------------------------------------------------------

async def _read_single_file(
    path_str: str,
    start_line: Any,
    end_line: Any,
    ctx: ToolContext,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read one file.  Returns ``(result_entry, multimodal_entry | None)``."""
    if not path_str:
        return {"path": "", "success": False, "error": "empty path"}, None

    _op, err = await request_guard(
        ctx, "read_file",
        {"path": path_str, "start_line": start_line, "end_line": end_line},
    )
    if err is not None:
        return {"path": path_str, "success": False, "error": err.get("error", "denied")}, None

    try:
        p = resolve_under_allowed_roots(ctx.workspace_root, path_str)
    except ValueError as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    if not p.exists() or not p.is_file():
        return {"path": path_str, "success": False, "error": "File not found"}, None

    ext = p.suffix.lower()

    # ---- multimodal (image) ----
    if ext in _IMAGE_EXTENSIONS:
        try:
            size = p.stat().st_size
            mime = _MIME_MAP.get(ext, "application/octet-stream")
            entry: dict[str, Any] = {
                "path": path_str, "success": True,
                "type": "multimodal", "mimeType": mime, "size": size,
            }
            dims = _get_image_dimensions(p)
            if dims:
                entry["dimensions"] = dims
            data_bytes = p.read_bytes()
            b64 = base64.b64encode(data_bytes).decode("ascii")
            mm: dict[str, Any] = {"mimeType": mime, "data": b64, "name": p.name}
            return entry, mm
        except Exception as exc:
            return {"path": path_str, "success": False, "error": str(exc)}, None

    # ---- text file ----
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            size = p.stat().st_size
            return {"path": path_str, "success": True, "type": "binary", "size": size}, None
        except Exception as exc:
            return {"path": path_str, "success": False, "error": str(exc)}, None
    except Exception as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    lines = text.splitlines()
    total_lines = len(lines)

    start_val = start_line if isinstance(start_line, int) and start_line > 0 else 1
    if isinstance(start_line, int) and start_line > total_lines:
        return {
            "path": path_str, "success": False,
            "error": f"startLine {start_line} exceeds total lines",
            "totalLines": total_lines,
        }, None

    s = start_val - 1
    if isinstance(end_line, int) and end_line >= start_val:
        e = min(end_line, total_lines)
    else:
        e = total_lines

    sliced = lines[s:e]
    width = max(4, len(str(e)))
    numbered = "\n".join([f"{i + s + 1:>{width}} | {ln}" for i, ln in enumerate(sliced)])

    result: dict[str, Any] = {
        "path": path_str, "success": True, "type": "text",
        "content": numbered, "lineCount": len(sliced),
    }
    if isinstance(start_line, int) or isinstance(end_line, int):
        result["totalLines"] = total_lines
        result["startLine"] = s + 1
        result["endLine"] = e
    return result, None


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

async def read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    files_arg = args.get("files")
    if isinstance(files_arg, list) and files_arg:
        file_entries = [e for e in files_arg if isinstance(e, dict)]
    else:
        # Legacy single-file mode
        path = str(args.get("path", ""))
        if not path:
            return {
                "ok": False, "success": False, "error": "no path specified",
                "data": {"results": [], "successCount": 0, "failCount": 0, "totalCount": 0, "multiRoot": False},
            }
        entry_dict: dict[str, Any] = {"path": path}
        if args.get("start_line") is not None:
            entry_dict["startLine"] = args["start_line"]
        if args.get("end_line") is not None:
            entry_dict["endLine"] = args["end_line"]
        file_entries = [entry_dict]

    if not file_entries:
        return {
            "ok": False, "success": False, "error": "empty files list",
            "data": {"results": [], "successCount": 0, "failCount": 0, "totalCount": 0, "multiRoot": False},
        }

    results: list[dict[str, Any]] = []
    multimodal_data: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for fe in file_entries:
        path_str = str(fe.get("path", "")).strip()
        sl = fe.get("startLine") if fe.get("startLine") is not None else fe.get("start_line")
        el = fe.get("endLine") if fe.get("endLine") is not None else fe.get("end_line")
        r, mm = await _read_single_file(path_str, sl, el, ctx)
        results.append(r)
        if r.get("success"):
            success_count += 1
        else:
            fail_count += 1
        if mm is not None:
            multimodal_data.append(mm)

    total_count = success_count + fail_count
    response: dict[str, Any] = {
        "ok": fail_count == 0,
        "success": fail_count == 0,
        "data": {
            "results": results,
            "successCount": success_count,
            "failCount": fail_count,
            "totalCount": total_count,
            "multiRoot": False,
        },
    }
    if fail_count > 0:
        response["error"] = f"{fail_count} file(s) failed to read"
    if multimodal_data:
        response["multimodal"] = multimodal_data

    # Backward compat: single text file → set top-level path/content
    if total_count == 1 and results:
        r0 = results[0]
        response["path"] = r0.get("path", "")
        if r0.get("success") and r0.get("type") == "text":
            response["content"] = r0.get("content", "")
        elif not r0.get("success"):
            response["error"] = r0.get("error", "read failed")

    return response
