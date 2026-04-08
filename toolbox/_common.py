"""Shared utilities and constants for built-in tools."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from clonoth_runtime import load_policy_config, parse_extra_roots

from .context import ToolContext


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

_SENSITIVE_ENV_KEYS_UPPER = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
}


# ---------------------------------------------------------------------------
#  Environment
# ---------------------------------------------------------------------------

def safe_subprocess_env() -> dict[str, str]:
    """Return a subprocess env with common secret variables stripped.

    This is not a complete security boundary, but it meaningfully reduces
    accidental leakage from command execution.
    """
    env = os.environ.copy()
    for k in list(env.keys()):
        ku = k.upper()
        if ku in _SENSITIVE_ENV_KEYS_UPPER:
            env.pop(k, None)
            continue
        if ku.endswith("_API_KEY"):
            env.pop(k, None)
            continue
    return env


# ---------------------------------------------------------------------------
#  Path resolution
# ---------------------------------------------------------------------------

def resolve_under_root(root: Path, rel_path: str) -> Path:
    """Resolve *rel_path* under *root*, raising if it escapes."""
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace root")
    return p


def _load_allowed_extra_roots(workspace_root: Path) -> list[Path]:
    """Load extra_roots from policy.yaml for defense in depth path checks."""
    data = load_policy_config(workspace_root)
    return parse_extra_roots(
        workspace_root,
        data.get("extra_roots") if isinstance(data, dict) else None,
    )


def resolve_under_allowed_roots(workspace_root: Path, path_str: str) -> Path:
    """Resolve path under workspace_root or policy-configured extra_roots."""
    extra_roots = _load_allowed_extra_roots(workspace_root)

    raw = Path(path_str)
    p = raw.resolve() if raw.is_absolute() else (workspace_root / path_str).resolve()

    try:
        p.relative_to(workspace_root)
        return p
    except ValueError:
        pass

    for r in extra_roots:
        try:
            p.relative_to(r)
            return p
        except ValueError:
            continue

    raise ValueError("path escapes workspace root")


# ---------------------------------------------------------------------------
#  Policy / approval guard
# ---------------------------------------------------------------------------

async def request_guard(
    ctx: ToolContext,
    op: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Request supervisor policy decision and wait for approval when needed.

    Returns (op_response, error_result).
    error_result is None when the operation may proceed.
    """
    op_res = await ctx.request_op(op, parameters)
    safety_level = str(op_res.get("safety_level") or "")

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    if safety_level == "deny":
        return op_res, {"ok": False, "error": op_res.get("reason", "denied")}

    if safety_level == "approval_required":
        approval_id = op_res.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") == "cancelled":
            return op_res, {"ok": False, "error": "task cancelled", "approval_id": approval_id, "cancelled": True}
        if approval.get("status") != "allowed":
            return op_res, {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    return op_res, None
