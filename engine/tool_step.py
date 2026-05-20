from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "x").strip() or "x")[:80]



def result_to_raw(tool_name: str, result: Any) -> tuple[str, str]:
    """把工具结果转为 (format, raw_text)。"""
    if tool_name == "read_file" and isinstance(result, dict):
        # New batch format: data.results
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            parts: list[str] = []
            for r in data["results"]:
                if not isinstance(r, dict):
                    continue
                p = r.get("path", "")
                if r.get("success") and r.get("type") == "text":
                    c = r.get("content", "")
                    if c:
                        parts.append(f"── {p} ──\n{c}")
                elif r.get("success") and r.get("type") == "multimodal":
                    parts.append(f"── {p} ── [image: {r.get('mimeType', '?')}, {r.get('size', 0)} bytes]")
                elif r.get("success") and r.get("type") == "binary":
                    parts.append(f"── {p} ── [binary: {r.get('size', 0)} bytes]")
                elif not r.get("success"):
                    parts.append(f"── {p} ── ERROR: {r.get('error', 'unknown')}")
            if parts:
                return "text", "\n".join(parts)
        # Legacy single-file format
        c = result.get("content")
        if isinstance(c, str) and c.strip():
            return "text", c
    if isinstance(result, dict) and "returncode" in result and isinstance(result.get("output"), str):
        return "text", f"returncode={result.get('returncode')}\n{result.get('output', '')}"
    try:
        return "json", json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return "json", str(result)


def _one_line_text(value: Any) -> str:
    """Return a whitespace-normalized single-line string for progress logs."""
    # [summary-args 2026-05-19] Why: handoff_progress is displayed as one log row,
    # but commands, queries, and final text can contain newlines. How: collapse all
    # whitespace into single spaces before composing summaries. Purpose: keep every
    # summarize_result() output safe for one-line progress messages.
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _clip_one_line(value: Any, limit: int) -> str:
    """Normalize text to one line and append an ellipsis when it is too long."""
    text = _one_line_text(value)
    if limit <= 0:
        return ""
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _summary_line(text: Any, limit: int = 120) -> str:
    """Enforce the final one-line and reasonable-length summary contract."""
    # [summary-args 2026-05-19] Why: individual argument snippets are clipped, but
    # a long path plus prefix can still exceed the desired log width. How: apply a
    # final 120-character cap after composing the message. Purpose: preserve the
    # legacy handoff_progress shape without creating overly long progress rows.
    line = _one_line_text(text)
    if len(line) > limit:
        return line[:max(0, limit - 3)] + "..."
    return line


_SENSITIVE_ARG_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization|bearer)", re.IGNORECASE)


def _brief_args(args: dict | None, *, value_limit: int = 30) -> str:
    """Build a compact fallback argument summary for tools without custom rules."""
    # [summary-args 2026-05-19] Why: the fallback rule requested by operators is
    # intentionally narrow: unknown tools should expose the first key=value only,
    # not a dump of every argument. How: take the first insertion-ordered item,
    # clip its key and value, and redact obvious secret-bearing argument names.
    # Purpose: make generic handoff_progress rows informative while preserving the
    # one-line, under-120-character summary contract.
    if not isinstance(args, dict) or not args:
        return ""
    key, value = next(iter(args.items()))
    key_text = _clip_one_line(key, 24)
    if _SENSITIVE_ARG_RE.search(str(key)):
        value_text = "<redacted>"
    elif isinstance(value, (dict, list, tuple)):
        try:
            value_text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            value_text = str(value)
        value_text = _clip_one_line(value_text, value_limit)
    else:
        value_text = _clip_one_line(value, value_limit)
    return _summary_line(f"{key_text}={value_text}", 100)


def _dispatch_target(tool_name: str, args: dict) -> str:
    """Resolve the target node name for dynamic and legacy dispatch-style tools."""
    # [summary-args 2026-05-19] Why: dispatch:{target} stores the node in the tool
    # name, while older dispatch_to_* names may also carry target in arguments. How:
    # prefer the fixed tool-name target and fall back to explicit argument fields.
    # Purpose: show a stable target_node in the progress summary for both forms.
    if tool_name.startswith("dispatch:"):
        return _clip_one_line(tool_name.split(":", 1)[1], 40)
    suffix = tool_name.removeprefix("dispatch_to_") if tool_name.startswith("dispatch_to_") else ""
    return _clip_one_line(args.get("target_node") or args.get("target") or suffix, 40)


def summarize_result(tool_name: str, result: Any, *, args: dict | None = None) -> str:
    """生成简短的工具结果摘要。"""
    # [summary-args 2026-05-19] Why: approval events already carry full details,
    # but handoff_progress only has this short summary. How: accept optional tool
    # arguments and use per-tool snippets for the parameters operators look for.
    # Purpose: keep the public message format unchanged while making each row
    # informative enough to identify the command, search, memory, or handoff.
    safe_args = args if isinstance(args, dict) else {}

    if tool_name.startswith("dispatch_to_") or tool_name.startswith("dispatch:"):
        target = _dispatch_target(tool_name, safe_args)
        instruction = _clip_one_line(safe_args.get("instruction", ""), 40)
        return _summary_line(f"委派 {target}: {instruction}" if instruction else f"委派 {target}")
    if tool_name == "finish":
        text = _clip_one_line(safe_args.get("text", ""), 40)
        return _summary_line(f"完成: {text}" if text else "完成")
    if tool_name == "reply":
        text = _clip_one_line(safe_args.get("text", ""), 40)
        return _summary_line(f"中间回复: {text}" if text else "中间回复")

    if not isinstance(result, dict):
        extra = _brief_args(safe_args)
        return _summary_line(f"已获得结果: {extra}" if extra else "已获得结果")
    if result.get("ok") is False:
        return _summary_line(f"失败: {result.get('error', 'unknown')}")
    if tool_name == "read_file":
        data = result.get("data")
        if isinstance(data, dict):
            sc = data.get("successCount", 0)
            fc = data.get("failCount", 0)
            tc = data.get("totalCount", 0)
            if tc == 1 and sc == 1:
                rs = data.get("results", [])
                p = rs[0].get("path", "") if rs else result.get("path", "")
                return _summary_line(f"已读取 {p}")
            if fc > 0:
                return _summary_line(f"读取 {tc} 个文件: {sc} 成功, {fc} 失败")
            return _summary_line(f"已读取 {sc} 个文件")
        return _summary_line(f"已读取 {result.get('path', '') or safe_args.get('path', '')}")
    if tool_name == "execute_command":
        rc = result.get("returncode")
        cmd_short = _clip_one_line(safe_args.get("command", ""), 60)
        return _summary_line(f"命令完成 (rc={rc}): {cmd_short}" if cmd_short else f"命令完成 (rc={rc})")
    if tool_name == "write_file":
        return _summary_line(f"已写入 {result.get('path', '') or safe_args.get('path', '')}")
    if tool_name == "search_in_files":
        q = _clip_one_line(safe_args.get("query", ""), 30)
        p = _clip_one_line(safe_args.get("path", "."), 40)
        data = result.get("data", {})
        count = data.get("count", "?") if isinstance(data, dict) else "?"
        return _summary_line(f'搜索 "{q}" in {p} ({count} 结果)')
    if tool_name == "apply_diff":
        p = _clip_one_line(safe_args.get("path", ""), 60)
        diffs = safe_args.get("diffs", [])
        n = len(diffs) if isinstance(diffs, list) else 0
        return _summary_line(f"差异应用 {p} ({n} 处修改)")
    if tool_name == "save_memory":
        memory_id = _clip_one_line(safe_args.get("id", ""), 40)
        book = _clip_one_line(safe_args.get("book", "default"), 40)
        return _summary_line(f"保存记忆 id={memory_id} book={book}")
    if tool_name == "delete_memory":
        memory_id = _clip_one_line(safe_args.get("id", ""), 40)
        return _summary_line(f"删除记忆 id={memory_id}")
    if tool_name == "list_dir":
        data = result.get("data")
        if isinstance(data, dict):
            tf = data.get("totalFiles", 0)
            td = data.get("totalDirs", 0)
            tp = data.get("totalPaths", 0)
            if tp == 1:
                return _summary_line(f"已列出目录 ({td} 目录, {tf} 文件)")
            return _summary_line(f"已列出 {tp} 个目录 ({td} 子目录, {tf} 文件)")
        return _summary_line(f"已列出 {result.get('path', '.')}")
    extra = _brief_args(safe_args)
    return _summary_line(f"已获得结果: {extra}" if extra else "已获得结果")


def format_tool_trace(entries: list[dict[str, Any]]) -> str:
    """把一批工具调用结果格式化为 CLONOTH_TOOL_TRACE 块。

    v2: 简化字段名，减少冗余前缀。
    """
    lines = ["[CLONOTH_TOOL_TRACE v2]"]
    for e in entries:
        lines.append(f"TOOL: {e['name']} {json.dumps(e.get('args', {}), ensure_ascii=False)}")
        lines.append(f"RESULT_FORMAT: {e.get('format', 'json')}")
        if e.get("truncated"):
            lines.append("RESULT_TRUNCATED: true")
        if e.get("ref"):
            lines.append(f"RESULT_REF: {e['ref']}")
        raw = e.get("raw_inline", "")
        if raw:
            lines.append("RESULT:")
            for ln in raw.splitlines():
                lines.append("  " + ln)
        else:
            lines.append("RESULT: <empty>")
        lines.append(f"SUMMARY: {e.get('summary', '')}")
        atts = e.get("attachments")
        if isinstance(atts, list) and atts:
            att_paths = [str(a.get('path', '')) for a in atts if isinstance(a, dict) and a.get('path')]
            if att_paths:
                lines.append(f"ATTACHMENTS: {', '.join(att_paths)}")
        lines.append("---")
    lines.append("[/CLONOTH_TOOL_TRACE]")
    return "\n".join(lines)
