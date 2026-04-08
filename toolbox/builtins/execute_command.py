"""execute_command — shell command execution (policy + approval guarded)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config

from ..context import ToolContext
from .._common import request_guard, safe_subprocess_env


async def execute_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    default_timeout_sec = get_float(
        runtime_cfg,
        "meta.execute_command.default_timeout_sec",
        60.0,
        min_value=1.0,
        max_value=3600.0,
    )
    max_output_chars = get_int(
        runtime_cfg,
        "meta.execute_command.max_output_chars",
        8000,
        min_value=1000,
        max_value=200_000,
    )

    timeout_raw = args.get("timeout_sec")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else float(default_timeout_sec)
    except Exception:
        timeout_sec = float(default_timeout_sec)

    _op, err = await request_guard(ctx, "execute_command", {"command": command})
    if err is not None:
        return err

    if await ctx.check_cancelled():
        return {"ok": False, "error": "task cancelled", "cancelled": True}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ctx.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
        )
        waiter = asyncio.create_task(proc.communicate())
        started = time.monotonic()
        while True:
            done, _pending = await asyncio.wait({waiter}, timeout=0.2)
            if waiter in done:
                stdout_bytes, stderr_bytes = waiter.result()
                break
            if await ctx.check_cancelled():
                proc.kill()
                await asyncio.gather(waiter, return_exceptions=True)
                return {"ok": False, "error": "task cancelled", "cancelled": True}
            if time.monotonic() - started >= timeout_sec:
                proc.kill()
                await asyncio.gather(waiter, return_exceptions=True)
                return {"ok": False, "error": f"timeout after {timeout_sec}s"}

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        out = (stdout_text or "") + ("\n" + stderr_text if stderr_text else "")
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + "\n...<truncated>"
        return {"ok": True, "returncode": int(proc.returncode or 0), "output": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}
