from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from clonoth_runtime import get_float, load_runtime_config

from . import builtins as _builtins
from ._common import kill_process_group as _kill_process_group
from ._common import safe_subprocess_env as _safe_subprocess_env
from . import mcp_runtime


ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


def _extract_tool_spec(py: Path) -> tuple[dict[str, Any] | None, float | None]:
    """Extract tool spec from a python file via AST.

    External tool format:
        SPEC = { ... }
        TIMEOUT_SEC = 60  # optional

        if __name__ == "__main__":
            # reads JSON from stdin, writes JSON to stdout

    Security:
    - We do NOT import or execute the module at registration time.
    - We only parse literals via ast.literal_eval.
    - At invocation, the script runs as an isolated subprocess.
    """

    try:
        text = py.read_text(encoding="utf-8")
    except Exception:
        return None, None

    try:
        tree = ast.parse(text, filename=str(py))
    except SyntaxError:
        return None, None

    vals: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            name = tgt.id
            if name not in {"SPEC", "TIMEOUT_SEC"}:
                continue
            try:
                vals[name] = ast.literal_eval(node.value)
            except Exception:
                continue

    spec = vals.get("SPEC")
    if not isinstance(spec, dict):
        return None, None

    timeout_sec: float | None = None
    if isinstance(vals.get("TIMEOUT_SEC"), (int, float)):
        timeout_sec = float(vals.get("TIMEOUT_SEC"))

    return spec, timeout_sec


def _make_script_tool(*, script_path: Path, timeout_sec: float | None) -> ToolFunc:
    """Create a tool function that runs a Python script as a subprocess.

    Protocol:
    - Input: tool arguments as JSON on stdin
    - Output: result as JSON on stdout
    - Environment: sensitive variables stripped
    - Timeout: configurable
    """

    async def _run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        root = getattr(ctx, "workspace_root", None)
        default_timeout_sec = 60.0
        if isinstance(root, Path):
            try:
                runtime_cfg = load_runtime_config(root)
                default_timeout_sec = get_float(
                    runtime_cfg,
                    "tools.script.default_timeout_sec",
                    60.0,
                    min_value=1.0,
                    max_value=3600.0,
                )
            except Exception:
                default_timeout_sec = 60.0

        timeout_val = float(timeout_sec) if timeout_sec is not None else float(default_timeout_sec)
        max_output = 16000

        input_json = json.dumps(args or {}, ensure_ascii=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root) if isinstance(root, Path) else None,
                env=_safe_subprocess_env(),
                # Fix: run in new session so os.killpg can kill the entire
                # process tree, preventing orphaned grandchildren from holding
                # pipe fds and causing communicate() to hang forever.
                start_new_session=True,
            )
            waiter = asyncio.create_task(proc.communicate(input=input_json.encode("utf-8")))
            started = asyncio.get_running_loop().time()
            while True:
                done, _pending = await asyncio.wait({waiter}, timeout=0.2)
                if waiter in done:
                    stdout_bytes, stderr_bytes = waiter.result()
                    break
                try:
                    if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                        # Fix: kill entire process group, not just the interpreter
                        _kill_process_group(proc)
                        # Fix: 5s safety timeout — if killpg didn't clean all
                        # grandchildren, don't hang forever waiting for pipe EOF
                        try:
                            await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                        return {"ok": False, "error": "script task cancelled", "cancelled": True}
                except Exception:
                    pass
                if asyncio.get_running_loop().time() - started >= timeout_val:
                    _kill_process_group(proc)
                    try:
                        await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
                    return {"ok": False, "error": f"script timeout after {timeout_val}s"}
        except asyncio.TimeoutError:
            try:
                _kill_process_group(proc)  # type: ignore[union-attr]
            except Exception:
                pass
            return {"ok": False, "error": f"script timeout after {timeout_val}s"}
        except Exception as e:
            return {"ok": False, "error": f"script execution failed: {e}"}

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"script exited with code {proc.returncode}",
                "stderr": stderr_text[:2000] if stderr_text else "",
                "stdout": stdout_text[:2000] if stdout_text else "",
            }

        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output]

        try:
            result = json.loads(stdout_text)
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        except json.JSONDecodeError:
            return {"ok": True, "output": stdout_text}

    return _run


class ToolRegistry:
    def __init__(self, *, workspace_root: Path, tools_dir: Path) -> None:
        self.workspace_root = workspace_root
        self.tools_dir = tools_dir

        self._tool_specs: dict[str, dict[str, Any]] = {}
        self._tool_funcs: dict[str, ToolFunc] = {}

        self._load_builtin_meta_tools()
        self._builtin_specs = dict(self._tool_specs)
        self._builtin_funcs = dict(self._tool_funcs)

        self.reload()

    def _load_builtin_meta_tools(self) -> None:
        builtins: list[tuple[str, str, dict[str, Any], ToolFunc]] = [
            (
                "list_dir",
                "List one or more directories under workspace root. Supports batch listing and recursive mode. Ignores .git by default.",
                {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "description": "Array of directory paths to list (relative to workspace root). MUST be an array even for single directory.",
                            "items": {"type": "string"},
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to list subdirectories recursively. Default false.",
                        },
                        "path": {"type": "string", "description": "(Legacy) Single directory path."},
                    },
                    "required": [],
                },
                _builtins.list_dir,
            ),
            (
                "read_file",
                "Read one or more files under workspace root. Supports text files (with optional line range), images (returned as multimodal), and binary files. Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "Array of file objects. Each has: path (required), startLine (optional, 1-based), endLine (optional, 1-based inclusive). MUST be an array even for single file.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "File path relative to workspace root."},
                                    "startLine": {"type": "integer", "description": "Start line (1-based, inclusive). Only for text files."},
                                    "endLine": {"type": "integer", "description": "End line (1-based, inclusive). Only for text files."},
                                },
                                "required": ["path"],
                            },
                        },
                        "path": {"type": "string", "description": "(Legacy single-file mode) File path relative to workspace root."},
                        "start_line": {"type": "integer", "description": "(Legacy) Start line number."},
                        "end_line": {"type": "integer", "description": "(Legacy) End line number."},
                    },
                    "required": [],
                },
                _builtins.read_file,
            ),
            (
                "write_file",
                "Write a text file under workspace root (policy+approval guarded).",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
                _builtins.write_file,
            ),
            (
                "apply_diff",
                "Apply sequential search/replace diffs to a file. "
                "Each diff specifies an exact 'search' string and a 'replace' string. "
                "The search must match 100% exactly (including whitespace and indentation). "
                "Multiple diffs are applied in order; each operates on the result of the previous. "
                "If 'start_line' is omitted and search matches multiple locations, the diff is rejected. "
                "Policy+approval guarded.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to workspace root.",
                        },
                        "diffs": {
                            "type": "array",
                            "description": "Array of diff objects to apply sequentially.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "search": {
                                        "type": "string",
                                        "description": "Exact text to find. Must match 100% including whitespace and indentation.",
                                    },
                                    "replace": {
                                        "type": "string",
                                        "description": "Replacement text.",
                                    },
                                    "start_line": {
                                        "type": "integer",
                                        "description": "(Recommended) 1-based line number to start searching from. Required when search matches multiple locations. Note: line numbers refer to content AFTER previous diffs in the array have been applied.",
                                    },
                                },
                                "required": ["search", "replace"],
                            },
                        },
                    },
                    "required": ["path", "diffs"],
                },
                _builtins.apply_diff,
            ),
            (
                "execute_command",
                "Execute a shell command in workspace root (policy+approval guarded; secrets are stripped from env).",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["command"],
                },
                _builtins.execute_command,
            ),
            (
                "search_in_files",
                "Search substring in files under workspace root.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "description": "Directory or file path to search in (relative or absolute). Supports single file. Default '.'"},
                        "mode": {"type": "string", "description": "search (default) or replace", "enum": ["search", "replace"]},
                        "pattern": {"type": "string", "description": "file glob pattern, e.g. '*.py', '**/*.js'. Default '**/*'"},
                        "isRegex": {"type": "boolean", "description": "treat query as regex. Default false"},
                        "maxResults": {"type": "integer", "description": "max matches for search mode. Default 100"},
                        "replace": {"type": "string", "description": "replacement string for replace mode. Supports $1 $2 capture groups"},
                        "maxFiles": {"type": "integer", "description": "max files to modify in replace mode. Default 50"},
                    },
                    "required": ["query"],
                },
                _builtins.search_in_files,
            ),
            (
                "create_or_update_skill",
                "Create or update a skill under skills/<name>/SKILL.md.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "content": {"type": "string", "description": "full SKILL.md content (optional; frontmatter will be normalized)"},
                        "enabled": {"type": "boolean"},
                        "strategy": {"type": "string", "description": "constant (always injected) or normal (keyword-triggered); default normal", "enum": ["constant", "normal"]},
                        "keywords": {"type": "array", "items": {"type": "string"}, "description": "activation keywords; supports /regex/flags syntax"},
                        "order": {"type": "integer", "description": "injection order within the same block; higher values are placed later (closer to conversation)"},
                        "priority": {"type": "integer", "description": "budget priority; higher values are kept first when token budget is exceeded"},
                        "scan_depth": {"type": "integer", "description": "number of recent conversation rounds to scan for keyword matching; 0 = current message only"},
                    },
                    "required": ["name"],
                },
                _builtins.create_or_update_skill,
            ),
            (
                "list_skills",
                "List local skills under skills/*/SKILL.md.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_skills,
            ),
            (
                "delete_skill",
                "Delete a skill directory under skills/<name>/.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
                _builtins.delete_skill,
            ),
            (
                "create_or_update_mcp_client",
                "Create or update an MCP client config in data/mcp_clients.yaml. Supports stdio, sse, and streamable_http transports.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http", "streamable-http", "http"]},
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "env": {"type": "object"},
                        "url": {"type": "string"},
                        "headers": {"type": "object"},
                    },
                    "required": ["id", "transport"],
                },
                _builtins.create_or_update_mcp_client,
            ),
            (
                "list_mcp_clients",
                "List configured MCP clients.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_mcp_clients,
            ),
            (
                "delete_mcp_client",
                "Delete an MCP client config by id.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                    },
                    "required": ["id"],
                },
                _builtins.delete_mcp_client,
            ),
            (
                "create_or_update_tool",
                "Create/update an external tool under tools/. "
                "Provide 'script' (Python code body). "
                "The tool runs as a subprocess: JSON in via stdin, JSON out via stdout.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "input_schema": {"type": "object"},
                        "script": {"type": "string", "description": "Python script body. Has access to: args (dict from stdin), output(result), fail(error)."},
                        "timeout_sec": {"type": "number"},
                    },
                    "required": ["name", "script"],
                },
                _builtins.create_or_update_tool,
            ),
            (
                "reload_tools",
                "Reload tools/ directory.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.reload_tools,
            ),
            (
                "request_restart",
                "Request supervisor to restart engine/all (policy+approval guarded; includes git diff summary).",
                {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": ["engine", "all"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["target"],
                },
                _builtins.request_restart,
            ),
            (
                "create_schedule",
                "Create or update a scheduled task in data/schedules.yaml. "
                "The task fires as an inbound message at the specified cron time (UTC). "
                "Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "unique schedule id"},
                        "cron": {"type": "string", "description": "5-field cron: minute hour day month weekday (UTC)"},
                        "text": {"type": "string", "description": "message text injected as inbound"},
                        "conversation_key": {"type": "string", "description": "conversation key (default: scheduler:{id})"},
                        "workflow_id": {"type": "string", "description": "optional workflow override"},
                        "enabled": {"type": "boolean"},
                        "once": {"type": "boolean", "description": "if true, auto-delete after first trigger"},
                    },
                    "required": ["id", "cron", "text"],
                },
                _builtins.create_schedule,
            ),
            (
                "list_schedules",
                "List all scheduled tasks from data/schedules.yaml.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.list_schedules,
            ),
            (
                "delete_schedule",
                "Delete a scheduled task by id. Requires approval.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "schedule id to delete"},
                    },
                    "required": ["id"],
                },
                _builtins.delete_schedule,
            ),
            (
                "cancel_active_tasks",
                "Cancel all active downstream tasks in the current session. "
                "Use when the user's new message makes the previous task unnecessary.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.cancel_active_tasks,
            ),
            (
                "get_context_window",
                "Get current context window token usage for this session. "
                "Returns actual LLM-reported tokens (if available), character-based estimates, "
                "compact threshold, and utilization ratio. "
                "Use this to check how much context budget remains before automatic compaction triggers.",
                {"type": "object", "properties": {}, "required": []},
                _builtins.get_context_window,
            ),
            (
                "save_memory",
                "Save or update a memory entry in a book. "
                "Use this when you learn something worth remembering across conversations: "
                "user preferences, corrections, project context, external resource pointers, "
                "or character profiles in group chat.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique entry id (e.g. user_zhangsan, rule_no_mock)."},
                        "book": {"type": "string", "description": "Book name (file grouping). Default 'default'. Use e.g. 'people' for character profiles, 'rules' for behavioral rules."},
                        "content": {"type": "string", "description": "Memory content text."},
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Activation keywords. Supports /regex/flags. When any keyword matches user input, this memory is injected into context.",
                        },
                        "constant": {"type": "boolean", "description": "If true, always injected regardless of keywords. Default false."},
                        "enabled": {"type": "boolean", "description": "Whether this entry is active. Default true."},
                        "priority": {"type": "integer", "description": "Budget priority; higher = kept first when budget exceeded."},
                        "scan_depth": {"type": "integer", "description": "Number of recent conversation rounds to scan for keywords. 0 = current message only."},
                    },
                    "required": ["id", "content"],
                },
                _builtins.save_memory,
            ),
            (
                "list_memories",
                "List memory entries, optionally filtered by book name.",
                {
                    "type": "object",
                    "properties": {
                        "book": {"type": "string", "description": "Filter by book name. Omit to list all."},
                    },
                    "required": [],
                },
                _builtins.list_memories,
            ),
            (
                "delete_memory",
                "Delete a memory entry from a book.",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Memory entry id to delete."},
                        "book": {"type": "string", "description": "Book name. Default 'default'."},
                    },
                    "required": ["id"],
                },
                _builtins.delete_memory,
            ),
        ]

        for name, desc, schema, func in builtins:
            self._tool_specs[name] = {
                "name": name,
                "description": desc,
                "input_schema": schema,
            }
            self._tool_funcs[name] = func

    def reload(self) -> int:
        """Reload external tools under tools/.

        Returns total number of tools available.
        """

        self._tool_specs = dict(self._builtin_specs)
        self._tool_funcs = dict(self._builtin_funcs)

        self.tools_dir.mkdir(parents=True, exist_ok=True)
        (self.tools_dir / "__init__.py").touch(exist_ok=True)

        if str(self.workspace_root) not in sys.path:
            sys.path.insert(0, str(self.workspace_root))

        builtin_names = set(self._tool_specs.keys())

        for py in self.tools_dir.glob("*.py"):
            if py.name == "__init__.py":
                continue

            spec, timeout_sec = _extract_tool_spec(py)
            if spec is None:
                continue

            name = spec.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()

            if name in builtin_names:
                continue

            description = str(spec.get("description", ""))
            input_schema = spec.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}, "required": []}

            self._tool_specs[name] = {
                "name": name,
                "description": description,
                "input_schema": input_schema,
            }
            self._tool_funcs[name] = _make_script_tool(
                script_path=py.resolve(), timeout_sec=timeout_sec,
            )

        return len(self._tool_specs)

    def list_specs(self) -> list[dict[str, Any]]:
        return list(self._tool_specs.values())

    async def execute(self, *, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if name not in self._tool_funcs:
            return {"ok": False, "error": f"tool not found: {name}"}

        func = self._tool_funcs[name]
        return await func(arguments, ctx)

    async def load_mcp_tools(self) -> int:
        """Scan enabled MCP clients and register their tools as first-class tools."""
        count = 0
        try:
            clients = mcp_runtime.list_clients(self.workspace_root)
        except Exception:
            return 0

        for client in clients:
            if not isinstance(client, dict):
                continue
            cid = str(client.get("id") or "").strip()
            if not cid or not client.get("enabled", True):
                continue

            try:
                result = await mcp_runtime.list_tools(self.workspace_root, cid)
                tools = result.get("tools") if isinstance(result, dict) else []
                if not isinstance(tools, list):
                    continue
            except Exception:
                logging.warning(f"[registry] MCP client '{cid}' tool list unavailable, skipping")
                continue

            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                raw_name = str(tool.get("name") or "").strip()
                if not raw_name:
                    continue

                reg_name = f"mcp_{cid}_{raw_name}"
                if reg_name in self._tool_specs:
                    continue

                desc = str(tool.get("description") or "").strip()
                schema = tool.get("input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}, "required": []}

                self._tool_specs[reg_name] = {
                    "name": reg_name,
                    "description": f"[MCP:{cid}] {desc}" if desc else f"[MCP:{cid}] {raw_name}",
                    "input_schema": schema,
                }
                self._tool_funcs[reg_name] = _make_mcp_tool(self.workspace_root, cid, raw_name)
                count += 1

        return count


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> ToolFunc:
    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            return await mcp_runtime.call_tool(workspace_root, client_id, tool_name, args)
        except Exception as e:
            return {"ok": False, "error": str(e), "mcp_client": client_id, "mcp_tool": tool_name}
    return _call
