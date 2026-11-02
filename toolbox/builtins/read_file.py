"""read_file — read one or more files (text / image / binary).

Supports single-file ``path`` and batch reading via ``files`` (or ``items``).
"""
from __future__ import annotations

import asyncio
import shutil
import uuid as _uuid
from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_MAX_IMAGE_FILE_BYTES = 25 * 1024 * 1024      # 25 MB max image file size
_MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024       # 10 MB threshold for oversized text files (Strategy A)
_MAX_OVERSIZED_SCAN_CHARS = 50 * 1024 * 1024  # 50M chars max streaming skip scan for startLine on oversized files
_OVERSIZED_PREVIEW_LINES = 50                 # Head lines returned when text file > 10 MB without explicit startLine/endLine
_OVERSIZED_PREVIEW_CHARS = 8 * 1024           # 8 KB max chars for oversized file head preview
_MAX_BATCH_FILES = 8
_MAX_LINES_PER_READ = 500                     # Max lines returned in a single read_file call
_MAX_OUTPUT_CHARS = 64 * 1024                 # 64 KB max total output chars per file
_MAX_LINE_READ_CHARS = 8 * 1024               # Bound per readline() call to prevent 50MB single-line OOM
_MAX_LINE_KEEP_CHARS = 4 * 1024               # Max chars kept for a single long line
_BINARY_SNIFF_BYTES = 8 * 1024                # 8 KB prefix sniff for binary detection
_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Map from sniffed MIME to canonical extension
_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _sniff_image_mime(data: bytes) -> str | None:
    """Detect actual image MIME type from magic bytes."""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 2 and data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(data) >= 4 and data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 2 and data[:2] == b"BM":
        return "image/bmp"
    return None


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


def _get_image_dimensions(p: Path, data: bytes | None = None) -> dict[str, Any] | None:
    try:
        import struct
        if data is None:
            with p.open("rb") as f:
                data = f.read(65536)
        ext = p.suffix.lower()
        if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return _make_dimensions(w, h)
        if ext in (".jpg", ".jpeg") and len(data) >= 2 and data[:2] == b"\xff\xd8":
            return _parse_jpeg_dimensions(data)
    except Exception:
        pass
    return None


def _is_binary_prefix(sample: bytes) -> bool:
    """Detect binary data from an initial byte sample without reading the full file."""
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError as exc:
        # If the error is only because a multi-byte UTF-8 sequence was cut off
        # at the very end of the 8KB buffer (at most 3 trailing bytes), it's valid UTF-8 prefix.
        if exc.start >= len(sample) - 3:
            try:
                sample[:exc.start].decode("utf-8")
                return False
            except UnicodeDecodeError:
                return True
        return True


def _read_bounded_line(f: Any) -> tuple[str | None, bool, int]:
    """Read one line with bounded memory.

    Returns ``(line_without_newline | None_if_eof, was_line_truncated, raw_chars_read)``.
    Never holds more than ``_MAX_LINE_READ_CHARS`` in memory even on a 100MB single-line file.
    """
    chunk = f.readline(_MAX_LINE_READ_CHARS)
    if not chunk:
        return None, False, 0
    chars_read = len(chunk)
    if chunk.endswith("\n") or chunk.endswith("\r"):
        return chunk.rstrip("\r\n"), False, chars_read
    # Check if we hit EOF or if the line exceeds _MAX_LINE_READ_CHARS
    nxt = f.readline(65536)
    if not nxt:
        return chunk.rstrip("\r\n"), False, chars_read
    chars_read += len(nxt)
    kept = chunk[:_MAX_LINE_KEEP_CHARS] + " ... [line truncated]"
    while nxt and not (nxt.endswith("\n") or nxt.endswith("\r")):
        nxt = f.readline(65536)
        chars_read += len(nxt)
    return kept, True, chars_read


def _read_single_file_sync(
    p: Path,
    path_str: str,
    start_line: Any,
    end_line: Any,
    workspace_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Synchronous worker for reading a single file (executed via asyncio.to_thread)."""
    try:
        file_size = p.stat().st_size
    except Exception as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    ext = p.suffix.lower()

    # ---- multimodal (image) ----
    if ext in _IMAGE_EXTENSIONS:
        if file_size > _MAX_IMAGE_FILE_BYTES:
            return {
                "path": path_str,
                "success": False,
                "error": f"Image file too large ({file_size / (1024 * 1024):.1f} MB > {_MAX_IMAGE_FILE_BYTES // (1024 * 1024)} MB limit)",
            }, None
        try:
            raw = p.read_bytes()
            size = len(raw)
            mime = _sniff_image_mime(raw) or _MIME_MAP.get(ext, "application/octet-stream")
            real_ext = _MIME_TO_EXT.get(mime, ext)
            entry: dict[str, Any] = {
                "path": path_str, "success": True,
                "type": "multimodal", "mimeType": mime, "size": size,
            }
            dims = _get_image_dimensions(p, raw[:65536])
            if dims:
                entry["dimensions"] = dims
            att_dir = workspace_root / "data" / "attachments" / "read_file"
            att_dir.mkdir(parents=True, exist_ok=True)
            att_name = f"{_uuid.uuid4().hex}{real_ext}"
            att_path = att_dir / att_name
            shutil.copy2(p, att_path)
            att_rel = att_path.relative_to(workspace_root).as_posix()
            mm: dict[str, Any] = {
                "type": "image",
                "path": att_rel,
                "mime_type": mime,
                "name": p.stem + real_ext,
            }
            return entry, mm
        except Exception as exc:
            return {"path": path_str, "success": False, "error": str(exc)}, None

    # ---- quick 8KB binary sniff ----
    try:
        with p.open("rb") as bf:
            sample = bf.read(_BINARY_SNIFF_BYTES)
        if _is_binary_prefix(sample):
            return {"path": path_str, "success": True, "type": "binary", "size": file_size}, None
    except Exception as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    start_val = start_line or 1
    has_explicit_range = start_line is not None or end_line is not None
    is_oversized = file_size > _MAX_TEXT_FILE_BYTES

    # ---- Strategy A: oversized text file (> 10 MB) without explicit line range -> head preview + hint ----
    if is_oversized and not has_explicit_range:
        preview_lines: list[str] = []
        total_chars = 0
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for _ in range(_OVERSIZED_PREVIEW_LINES):
                    ln, _, _ = _read_bounded_line(f)
                    if ln is None:
                        break
                    if preview_lines and total_chars + len(ln) > _OVERSIZED_PREVIEW_CHARS:
                        break
                    preview_lines.append(ln)
                    total_chars += len(ln) + 1
        except Exception as exc:
            return {"path": path_str, "success": False, "error": str(exc)}, None

        shown = len(preview_lines)
        width = max(4, len(str(max(1, shown))))
        numbered = "\n".join([f"{i + 1:>{width}} | {ln}" for i, ln in enumerate(preview_lines)])
        mb_str = f"{file_size / (1024 * 1024):.1f} MB"
        limit_mb = f"{_MAX_TEXT_FILE_BYTES // (1024 * 1024)} MB"
        hint = (
            f"File is too large ({mb_str} > {limit_mb} limit). "
            f"Only the first {shown} lines (up to {_OVERSIZED_PREVIEW_CHARS // 1024} KB) are shown as a head preview; "
            f"full-file loading is disabled to prevent memory exhaustion. "
            f"Use startLine/endLine (e.g. startLine={shown + 1}) to stream subsequent sections, "
            f"or use execute_command with head/tail/sed/grep."
        )
        return {
            "path": path_str,
            "success": True,
            "type": "text",
            "content": numbered,
            "lineCount": shown,
            "truncated": True,
            "oversized": True,
            "size": file_size,
            "shownLines": shown,
            "startLine": 1,
            "endLine": shown,
            "hint": hint,
        }, None

    # ---- Streaming bounded read (<= 10 MB, or > 10 MB with explicit startLine/endLine) ----
    requested_end = end_line  # None means up to EOF, capped by _MAX_LINES_PER_READ

    max_allowed_end = start_val + _MAX_LINES_PER_READ - 1
    if requested_end is None or requested_end > max_allowed_end:
        collect_end = max_allowed_end
    else:
        collect_end = requested_end

    sliced: list[str] = []
    accumulated_chars = 0
    char_cap_hit = False
    total_lines = 0
    scanned_chars = 0
    stopped_early_oversized = False
    has_more_after_collect = False

    try:
        with p.open("r", encoding="utf-8", errors="replace" if is_oversized else "strict") as f:
            while True:
                ln, _, raw_chars = _read_bounded_line(f)
                if ln is None:
                    break
                total_lines += 1
                scanned_chars += raw_chars
                if total_lines < start_val:
                    if is_oversized and scanned_chars > _MAX_OVERSIZED_SCAN_CHARS:
                        return {
                            "path": path_str,
                            "success": False,
                            "error": (
                                f"startLine {start_val} exceeds the {_MAX_OVERSIZED_SCAN_CHARS // (1024 * 1024)} MB "
                                f"streaming scan limit on oversized file (reached line {total_lines}). "
                                f"Use execute_command with sed/tail/grep for deep offsets in huge files."
                            ),
                        }, None
                    continue
                if total_lines <= collect_end and not char_cap_hit:
                    if sliced and accumulated_chars + len(ln) > _MAX_OUTPUT_CHARS:
                        char_cap_hit = True
                    else:
                        sliced.append(ln)
                        accumulated_chars += len(ln) + 1
                # 对 > 10 MB 的大文件，一旦收集完 collect_end 或触发 char_cap_hit，仅多探测 1 行即刻退出，绝不空扫全文件尾部
                if is_oversized and (total_lines >= collect_end or char_cap_hit):
                    if char_cap_hit and total_lines > len(sliced) + start_val - 1:
                        has_more_after_collect = True
                    else:
                        peek_ln, _, _ = _read_bounded_line(f)
                        if peek_ln is not None:
                            has_more_after_collect = True
                            total_lines += 1
                    stopped_early_oversized = True
                    break
    except UnicodeDecodeError:
        return {"path": path_str, "success": True, "type": "binary", "size": file_size}, None
    except Exception as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    if start_line is not None and start_line > total_lines:
        return {
            "path": path_str, "success": False,
            "error": f"startLine {start_line} exceeds total lines",
            "totalLines": total_lines,
        }, None

    s = start_val - 1
    e = s + len(sliced)
    if stopped_early_oversized:
        line_cap_hit = (not char_cap_hit) and has_more_after_collect and (requested_end is None or e < requested_end)
    else:
        desired_final_end = min(requested_end, total_lines) if requested_end is not None else total_lines
        line_cap_hit = (not char_cap_hit) and (e < desired_final_end)
    _auto_truncated = line_cap_hit or char_cap_hit or (is_oversized and has_more_after_collect)

    width = max(4, len(str(max(1, e))))
    numbered = "\n".join([f"{i + s + 1:>{width}} | {ln}" for i, ln in enumerate(sliced)])

    result: dict[str, Any] = {
        "path": path_str, "success": True, "type": "text",
        "content": numbered, "lineCount": len(sliced),
    }
    if is_oversized:
        result["oversized"] = True
        result["size"] = file_size
    if not stopped_early_oversized:
        result["totalLines"] = total_lines
    result["startLine"] = s + 1
    result["endLine"] = e
    if _auto_truncated:
        result["truncated"] = True
        result["shownLines"] = len(sliced)
        if is_oversized and stopped_early_oversized:
            mb_str = f"{file_size / (1024 * 1024):.1f} MB"
            result["hint"] = (
                f"Showing lines {s + 1}-{e} of oversized file ({mb_str}). "
                f"Use startLine={e + 1} to continue streaming, or use execute_command with sed/tail/grep."
            )
        elif char_cap_hit:
            result["hint"] = (
                f"Output reached {_MAX_OUTPUT_CHARS // 1024} KB character limit at line {e} "
                f"(total {total_lines} lines). Use startLine/endLine to read subsequent lines."
            )
        elif not has_explicit_range:
            result["hint"] = (
                f"File has {total_lines} lines but only first {_MAX_LINES_PER_READ} are shown. "
                f"Use startLine/endLine to read specific sections."
            )
        else:
            result["hint"] = (
                f"Requested range exceeded {_MAX_LINES_PER_READ} lines per read; "
                f"showing lines {s + 1}-{e} of {total_lines}. Use startLine={e + 1} to continue."
            )
    return result, None


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

    if start_line is not None or end_line is not None:
        try:
            if isinstance(start_line, bool) or isinstance(end_line, bool):
                raise ValueError
            start_line = int(start_line) if start_line is not None else None
            end_line = int(end_line) if end_line is not None else None
            if ((start_line is not None and start_line < 1)
                    or (end_line is not None and end_line < 1)
                    or (start_line is not None and end_line is not None and end_line < start_line)):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return {
                "path": path_str, "success": False,
                "error": "invalid line range (startLine/endLine must be >= 1 and endLine >= startLine)",
            }, None

    _op, err = await request_guard(
        ctx, "read_file",
        {"path": path_str, "start_line": start_line, "end_line": end_line},
    )
    if err is not None:
        result_entry = {"path": path_str, "success": False, "error": err.get("error", "denied")}
        cancelled = bool(err.get("cancelled", False))
        if cancelled:
            result_entry["cancelled"] = True
        return result_entry, None

    try:
        p = resolve_under_allowed_roots(ctx.workspace_root, path_str, workspace=ctx.workspace)
    except ValueError as exc:
        return {"path": path_str, "success": False, "error": str(exc)}, None

    if not p.exists() or not p.is_file():
        return {"path": path_str, "success": False, "error": "File not found"}, None

    return await asyncio.to_thread(
        _read_single_file_sync, p, path_str, start_line, end_line, ctx.workspace_root
    )


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------


def _read_file_result_text(results: list[dict[str, Any]]) -> str:
    # [AutoC 2026-05-31] Why: read_file should expose the exact readable transcript
    # in data.result under the unified tool response schema. How: reuse the existing
    # section header format for text, image, binary, and error entries. Purpose:
    # keep model-visible file contents stable without relying on formatter fallback.
    parts: list[str] = []
    if len(results) > 1:
        ok_n = sum(1 for r in results if r.get("success"))
        fail_n = len(results) - ok_n
        summary_lines = [f"[Batch read: {ok_n} succeeded, {fail_n} failed]"]
        for r in results:
            p = r.get("path", "")
            if r.get("success") and r.get("type") == "text":
                sl = r.get("startLine", 1)
                el = r.get("endLine", 0)
                tl = r.get("totalLines", 0)
                trunc = " (truncated)" if r.get("truncated") else ""
                summary_lines.append(f"- {p}: ok (L{sl}-{el}/{tl}{trunc})")
            elif r.get("success"):
                summary_lines.append(f"- {p}: ok ({r.get('type', 'file')}, {r.get('size', 0)} bytes)")
            else:
                summary_lines.append(f"- {p}: ERROR ({r.get('error', 'unknown')})")
        parts.append("\n".join(summary_lines))
    for r in results:
        p = r.get("path", "")
        if r.get("success") and r.get("type") == "text":
            c = r.get("content", "")
            hint = r.get("hint", "")
            body = c if c else "(empty file, 0 lines)"
            if hint:
                body = f"{body}\n\n[{hint}]"
            parts.append(f"── {p} ──\n{body}")
        elif r.get("success") and r.get("type") == "multimodal":
            parts.append(f"── {p} ── [image: {r.get('mimeType', '?')}, {r.get('size', 0)} bytes]")
        elif r.get("success") and r.get("type") == "binary":
            parts.append(f"── {p} ── [binary: {r.get('size', 0)} bytes]")
        elif not r.get("success"):
            parts.append(f"── {p} ── ERROR: {r.get('error', 'unknown')}")
    return "\n".join(parts) if parts else "No files read"


async def read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    files_arg = args.get("files") if args.get("files") is not None else args.get("items")
    error = None
    if files_arg is not None:
        if not isinstance(files_arg, list) or not files_arg:
            error = "empty files list"
        elif len(files_arg) > _MAX_BATCH_FILES:
            error = f"too many files in batch ({len(files_arg)} > {_MAX_BATCH_FILES}); read at most {_MAX_BATCH_FILES} files per call"
        elif any(not isinstance(entry, dict) for entry in files_arg):
            error = "invalid file entry in files list (expected object)"
        file_entries = files_arg
    else:
        path = str(args.get("path") or "").strip()
        if not path:
            error = "no path specified"
        sl = args.get("startLine") if args.get("startLine") is not None else args.get("start_line")
        el = args.get("endLine") if args.get("endLine") is not None else args.get("end_line")
        file_entries = [{"path": path, "startLine": sl, "endLine": el}]

    if error is not None:
        return {
            "ok": False, "success": False, "error": error,
            "data": {"result": f"ERROR: {error}", "results": [], "successCount": 0, "failCount": 0, "totalCount": 0, "multiRoot": False},
        }

    results: list[dict[str, Any]] = []
    multimodal_data: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0
    was_cancelled = False

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
        if r.get("cancelled"):
            was_cancelled = True
            break
        if mm is not None:
            multimodal_data.append(mm)

    total_count = success_count + fail_count
    response: dict[str, Any] = {
        "ok": fail_count == 0,
        "success": fail_count == 0,
        "data": {
            "result": _read_file_result_text(results),
            "results": results,
            "successCount": success_count,
            "failCount": fail_count,
            "totalCount": total_count,
            "multiRoot": False,
        },
    }
    if was_cancelled:
        response["cancelled"] = True
    if fail_count > 0:
        response["error"] = f"{fail_count} file(s) failed to read"
    if multimodal_data:
        # [AutoC 2026-05-31] Why: attachment metadata now lives under data but must
        # be mirrored at the top level during migration. How: write both locations
        # with the same list. Purpose: keep existing attachment collectors working
        # while new readers consume data.attachments.
        response["data"]["attachments"] = multimodal_data
        response["attachments"] = multimodal_data

    # Backward compat: single text file → set top-level path/content
    if total_count == 1 and results:
        r0 = results[0]
        response["path"] = r0.get("path", "")
        if r0.get("success") and r0.get("type") == "text":
            response["content"] = r0.get("content", "")
        elif not r0.get("success"):
            response["error"] = r0.get("error", "read failed")

    return response
