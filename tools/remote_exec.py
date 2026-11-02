from __future__ import annotations

"""
External tool (Clonoth): run a shell command on a pre-configured remote host over SSH.

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout

The SSH target comes from ``REMOTE_EXEC_HOST`` (an ``~/.ssh/config`` alias or
``user@host``); the repository never carries a real host. Reusing a ControlMaster
connection in ``~/.ssh/config`` keeps latency low.
"""

SPEC = {
    "name": "remote_exec",
    "description": (
        "在预先配置的远程服务器上通过 SSH 执行 shell 命令（目标由运维在 REMOTE_EXEC_HOST 中配置）。"
        "用于扫描任务、远程文件操作等。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要在远程服务器上执行的 shell 命令"},
            "timeout_sec": {"type": "number", "default": 30, "description": "超时秒数，默认 30"},
        },
        "required": ["command"],
    },
}

TIMEOUT_SEC = 35.0

MAX_OUTPUT_CHARS = 50000


def _combine(out: str, err: str) -> str:
    combined = out or ""
    if err:
        if combined:
            combined += "\n--- stderr ---\n"
        combined += err
    if len(combined) > MAX_OUTPUT_CHARS:
        combined = combined[:MAX_OUTPUT_CHARS] + "\n...(truncated)"
    return combined


if __name__ == "__main__":
    import json
    import os
    import subprocess
    import sys

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    args = json.loads(sys.stdin.read() or "{}")
    cmd = str(args.get("command") or "")
    try:
        timeout = int(args.get("timeout_sec", 30))
    except (TypeError, ValueError):
        timeout = 30
    if not cmd:
        fail("command is required")

    host = os.environ.get("REMOTE_EXEC_HOST", "").strip()
    if not host:
        fail("REMOTE_EXEC_HOST 未配置：请在 .env 中设置 SSH 目标（~/.ssh/config 别名或 user@host）")

    try:
        result = subprocess.run(["ssh", host, cmd], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(f"Command timed out after {timeout}s")
    except Exception as e:
        fail(str(e))

    combined = _combine(result.stdout, result.stderr)
    rc = result.returncode
    output({"ok": True, "data": {"result": f"returncode={rc}\n{combined}", "returncode": rc, "output": combined}})
