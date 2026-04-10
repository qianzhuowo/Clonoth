"""apply_diff — apply sequential search/replace diffs to a file (policy + approval guarded).

Each diff in the array is applied in order. The search string must match exactly
(including whitespace and indentation). If start_line is omitted and the search
string appears more than once, the diff is rejected.

After diffs[0] is applied, diffs[1] operates on the *resulting* content, and so on.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import request_guard, resolve_under_allowed_roots


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE = 1024 * 1024 * 64  # 64 MiB — generous but bounded


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _find_and_replace(
    content: str,
    search: str,
    replace: str,
    start_line: int | None,
) -> tuple[str, str | None]:
    """Apply a single search/replace on *content*.

    Returns (new_content, error_message | None).
    error_message is None on success.
    """
    if not search:
        return content, "Empty search string is not allowed."

    if start_line is not None:
        # Scoped search: only look in the region starting at start_line.
        lines = content.split("\n")
        if start_line < 1 or start_line > len(lines):
            return content, (
                f"start_line {start_line} is out of range "
                f"(file has {len(lines)} lines)."
            )

        # Build the prefix (lines before start_line) and the search region.
        prefix = "\n".join(lines[: start_line - 1])
        region = "\n".join(lines[start_line - 1:])

        idx = region.find(search)
        if idx == -1:
            return content, (
                "No exact match found starting from line "
                f"{start_line}. Please verify the content matches exactly."
            )

        # Replace the *first* occurrence in region.
        new_region = region[:idx] + replace + region[idx + len(search):]
        separator = "\n" if prefix else ""
        new_content = prefix + separator + new_region
        return new_content, None

    # Global search: require unique match.
    count = content.count(search)
    if count == 0:
        return content, (
            "No exact match found. "
            "Please verify the content matches exactly."
        )
    if count > 1:
        return content, (
            f"Multiple matches found ({count}). "
            "Please provide 'start_line' parameter to specify which match to use."
        )

    new_content = content.replace(search, replace, 1)
    return new_content, None


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

async def apply_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path_str = str(args.get("path") or "").strip()
    diffs = args.get("diffs")

    # ------------------------------------------------------------------
    #  Basic validation
    # ------------------------------------------------------------------
    if not path_str:
        return {"success": False, "error": "'path' is required."}

    if not isinstance(diffs, list) or len(diffs) == 0:
        return {"success": False, "error": "'diffs' must be a non-empty array."}

    # ------------------------------------------------------------------
    #  Policy + approval guard (same flow as write_file)
    # ------------------------------------------------------------------
    _op, err = await request_guard(
        ctx,
        "write_file",
        {
            "path": path_str,
            "content_preview": f"apply_diff: {len(diffs)} diff(s)",
            "content_len": 0,
            "tool_name": "apply_diff",
        },
    )
    if err is not None:
        cancelled = err.get("cancelled", False)
        if cancelled:
            return {
                "success": False,
                "cancelled": True,
                "error": "Diff was cancelled by user",
                "data": {
                    "file": path_str,
                    "message": f"Diff for {path_str} was cancelled by user.",
                    "status": "rejected",
                    "diffCount": len(diffs),
                    "appliedCount": 0,
                    "failedCount": 0,
                },
            }
        return {"success": False, "error": err.get("error", "denied")}

    # ------------------------------------------------------------------
    #  Resolve & read file
    # ------------------------------------------------------------------
    try:
        resolved = resolve_under_allowed_roots(ctx.workspace_root, path_str)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not resolved.exists() or not resolved.is_file():
        return {
            "success": False,
            "error": f"File not found: {path_str}",
        }

    file_size = resolved.stat().st_size
    if file_size > _MAX_FILE_SIZE:
        return {
            "success": False,
            "error": (
                f"File is too large ({file_size} bytes). "
                f"Maximum editable size is {_MAX_FILE_SIZE} bytes."
            ),
        }

    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"success": False, "error": "File is not valid UTF-8 text."}
    except Exception as exc:
        return {"success": False, "error": f"Failed to read file: {exc}"}

    # ------------------------------------------------------------------
    #  Apply diffs sequentially
    # ------------------------------------------------------------------
    applied_count = 0
    failed_count = 0
    failed_diffs: list[dict[str, Any]] = []

    for i, diff_entry in enumerate(diffs):
        if not isinstance(diff_entry, dict):
            failed_count += 1
            failed_diffs.append({"index": i, "error": "diff entry must be an object."})
            continue

        search = diff_entry.get("search")
        replace = diff_entry.get("replace")

        if search is None:
            failed_count += 1
            failed_diffs.append({"index": i, "error": "'search' is required."})
            continue
        if replace is None:
            failed_count += 1
            failed_diffs.append({"index": i, "error": "'replace' is required."})
            continue

        search = str(search)
        replace = str(replace)

        start_line_raw = diff_entry.get("start_line")
        start_line: int | None = None
        if start_line_raw is not None:
            try:
                start_line = int(start_line_raw)
            except (TypeError, ValueError):
                failed_count += 1
                failed_diffs.append({
                    "index": i,
                    "error": f"'start_line' must be an integer, got {type(start_line_raw).__name__}.",
                })
                continue

        new_content, error = _find_and_replace(content, search, replace, start_line)
        if error is not None:
            failed_count += 1
            failed_diffs.append({"index": i, "error": error})
            # Don't update content — skip this diff and continue with next.
            continue

        content = new_content
        applied_count += 1

    diff_count = len(diffs)

    # ------------------------------------------------------------------
    #  All diffs failed → return error, do NOT write
    # ------------------------------------------------------------------
    if applied_count == 0:
        first_error = failed_diffs[0]["error"] if failed_diffs else "unknown"
        return {
            "success": False,
            "error": f"Failed to apply any diffs: {first_error}",
            "data": {
                "file": path_str,
                "message": f"Failed to apply any diffs to {path_str}.",
                "failedDiffs": failed_diffs,
                "appliedCount": 0,
                "totalCount": diff_count,
                "failedCount": failed_count,
            },
        }

    # ------------------------------------------------------------------
    #  At least one diff succeeded → write result to disk
    # ------------------------------------------------------------------
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except Exception as exc:
        return {
            "success": False,
            "error": f"Failed to write file: {exc}",
            "data": {
                "file": path_str,
                "appliedCount": applied_count,
                "failedCount": failed_count,
            },
        }

    # ------------------------------------------------------------------
    #  Build response
    # ------------------------------------------------------------------
    if failed_count == 0:
        message = f"Diff applied and saved to {path_str}"
    else:
        message = (
            f"Partially applied diffs to {path_str}: "
            f"{applied_count} succeeded, {failed_count} failed. "
            f"Saved successfully."
        )

    data: dict[str, Any] = {
        "file": path_str,
        "message": message,
        "status": "accepted",
        "diffCount": diff_count,
        "appliedCount": applied_count,
        "failedCount": failed_count,
    }
    if failed_diffs:
        data["failedDiffs"] = failed_diffs

    return {"success": True, "data": data}
