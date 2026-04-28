"""Memory system — lorebook-style keyword-triggered + constant memory injection.

Storage: data/memory/*.yaml, each file is a "book" containing grouped entries.
Format:
    book: <book_name>
    entries:
      - id: <unique_id>
        keywords: [...]         # activation keywords; supports /regex/flags
        content: "..."          # memory body text
        constant: false         # true = always injected
        enabled: true
        priority: 0             # higher = kept first under budget
        scan_depth: 0           # rounds of history to scan for keywords

Injection order in prompt:
    system_prompt → skill_msgs → memory_msgs → history → instruction
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
#  Keyword compilation (same logic as skills_runtime.py, duplicated to
#  avoid cross-layer import)
# ---------------------------------------------------------------------------

def _compile_keyword(kw: str) -> re.Pattern[str] | str:
    """Compile a keyword entry.

    If *kw* matches ``/pattern/flags``, compile as regex.
    Otherwise return lowercased string for substring matching.
    """
    kw = (kw or "").strip()
    if not kw:
        return ""
    if kw.startswith("/"):
        last_slash = kw.rfind("/")
        if last_slash > 0:
            pattern = kw[1:last_slash]
            flags_str = kw[last_slash + 1:]
            flags = 0
            if "i" in flags_str:
                flags |= re.IGNORECASE
            if "s" in flags_str:
                flags |= re.DOTALL
            if "m" in flags_str:
                flags |= re.MULTILINE
            try:
                return re.compile(pattern, flags)
            except re.error:
                pass
    return kw.lower()


def _match_keywords(compiled: list[re.Pattern[str] | str], text: str) -> bool:
    """Return True if any compiled keyword matches *text*."""
    if not compiled or not text:
        return False
    text_lower = text.lower()
    for kw in compiled:
        if not kw:
            continue
        if isinstance(kw, re.Pattern):
            if kw.search(text):
                return True
        elif kw in text_lower:
            return True
    return False


# ---------------------------------------------------------------------------
#  Scan text building (same logic as skills_runtime)
# ---------------------------------------------------------------------------

def _build_scan_text(
    instruction_text: str,
    history: list[dict[str, Any]] | None,
    scan_depth: int,
) -> str:
    """Build text to scan for keyword matching.

    *instruction_text* is always included.  When *scan_depth* > 0,
    the last *scan_depth* rounds from *history* are appended.
    """
    parts: list[str] = [instruction_text or ""]
    if history and scan_depth > 0:
        round_starts: list[int] = []
        for i in range(len(history) - 1, -1, -1):
            role = history[i].get("role", "")
            if role != "user":
                continue
            if i == 0 or history[i - 1].get("role", "") != "user":
                round_starts.append(i)
                if len(round_starts) >= scan_depth:
                    break
        if round_starts:
            start_idx = round_starts[-1]
            for msg in history[start_idx:]:
                content = msg.get("content")
                if isinstance(content, str) and msg.get("role") in ("user", "assistant"):
                    parts.append(content)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
#  Cache
# ---------------------------------------------------------------------------

class _MemoryCache:
    """Per-workspace memory catalog cache keyed on time."""
    _lock = threading.Lock()
    _entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _TTL = 2.0

    @classmethod
    def get(cls, workspace_root: Path) -> list[dict[str, Any]] | None:
        key = str(workspace_root)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]]) -> None:
        key = str(workspace_root)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)

    @classmethod
    def invalidate(cls, workspace_root: Path) -> None:
        key = str(workspace_root)
        with cls._lock:
            cls._entries.pop(key, None)


# ---------------------------------------------------------------------------
#  Catalog loading
# ---------------------------------------------------------------------------

def memory_dir(workspace_root: Path) -> Path:
    """Return the memory storage directory path."""
    return workspace_root / "data" / "memory"


def load_memory_catalog(
    workspace_root: Path,
    *,
    _use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Scan ``data/memory/*.yaml`` and return parsed entries."""
    if _use_cache:
        cached = _MemoryCache.get(workspace_root)
        if cached is not None:
            return cached

    mem_dir = memory_dir(workspace_root)
    if not mem_dir.exists() or not mem_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for yaml_path in sorted(mem_dir.glob("*.yaml")):
        try:
            text = yaml_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                continue
            book = str(data.get("book") or yaml_path.stem).strip()
            entries = data.get("entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                eid = str(entry.get("id") or "").strip()
                if not eid:
                    continue
                if not entry.get("enabled", True):
                    continue

                content = str(entry.get("content") or "").strip()
                if not content:
                    continue

                raw_kw = entry.get("keywords")
                keywords: list[str] = []
                if isinstance(raw_kw, list):
                    keywords = [
                        str(k).strip()
                        for k in raw_kw
                        if isinstance(k, str) and str(k).strip()
                    ]
                elif isinstance(raw_kw, str) and raw_kw.strip():
                    keywords = [raw_kw.strip()]

                constant = bool(entry.get("constant", False))
                priority = 0
                if isinstance(entry.get("priority"), (int, float)):
                    priority = int(entry["priority"])
                scan_depth = 0
                if isinstance(entry.get("scan_depth"), (int, float)):
                    scan_depth = max(0, int(entry["scan_depth"]))

                # node_ids: 逗号分隔字符串或列表，空/不写 = 全局
                raw_node_ids = entry.get("node_ids")
                node_ids: list[str] = []
                if isinstance(raw_node_ids, list):
                    node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
                elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
                    node_ids = [n.strip() for n in raw_node_ids.split(",") if n.strip()]

                items.append({
                    "book": book,
                    "id": eid,
                    "keywords": keywords,
                    "compiled_keywords": [_compile_keyword(k) for k in keywords],
                    "content": content,
                    "constant": constant,
                    "priority": priority,
                    "scan_depth": scan_depth,
                    "node_ids": node_ids,
                })
        except Exception:
            continue

    _MemoryCache.put(workspace_root, items)
    return items


# ---------------------------------------------------------------------------
#  Message building
# ---------------------------------------------------------------------------

def build_memory_messages(
    workspace_root: Path,
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    max_budget_chars: int = 0,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build system messages for memory injection.

    Returns ``(static_msgs, dynamic_msgs)`` tuple for prompt-cache
    optimisation.  The caller should place *static_msgs* in the stable
    prefix (before history) and *dynamic_msgs* after history.

    * *static_msgs*  — constant memories (stable across turns)
    * *dynamic_msgs* — active (keyword-matched) memories + discovery index
    """
    catalog = load_memory_catalog(workspace_root)
    if not catalog:
        return [], []

    # 按 memory_mode allowlist 过滤 book
    if memory_mode == "allowlist" and memory_allow:
        catalog = [e for e in catalog if e.get("book") in memory_allow]
        if not catalog:
            return [], []

    # 按 node_ids 过滤：空列表 = 全局可见
    if node_id:
        catalog = [e for e in catalog if not e.get("node_ids") or node_id in e["node_ids"]]

    constant_entries: list[dict[str, Any]] = []
    keyword_entries: list[dict[str, Any]] = []

    for entry in catalog:
        if entry["constant"]:
            constant_entries.append(entry)
        elif entry["keywords"]:
            keyword_entries.append(entry)

    # keyword matching
    dynamic_entries: list[dict[str, Any]] = []
    for entry in keyword_entries:
        scan_text = _build_scan_text(
            instruction_text, history, entry["scan_depth"],
        )
        if _match_keywords(entry["compiled_keywords"], scan_text):
            dynamic_entries.append(entry)

    # Deterministic ordering: sort by id within each group so identical
    # match sets always produce byte-identical output (prompt cache friendly).
    constant_entries.sort(key=lambda e: e.get("id", ""))
    dynamic_entries.sort(key=lambda e: e.get("id", ""))

    # budget enforcement
    if max_budget_chars > 0:
        all_injectable: list[tuple[str, dict[str, Any]]] = []
        for e in constant_entries:
            all_injectable.append(("constant", e))
        for e in dynamic_entries:
            all_injectable.append(("dynamic", e))
        all_injectable.sort(key=lambda x: x[1]["priority"], reverse=True)

        kept_constant: list[dict[str, Any]] = []
        kept_dynamic: list[dict[str, Any]] = []
        used = 0
        for kind, e in all_injectable:
            body_len = len(e.get("content") or "")
            if used + body_len <= max_budget_chars:
                used += body_len
                if kind == "constant":
                    kept_constant.append(e)
                else:
                    kept_dynamic.append(e)
        # Re-sort by id after budget truncation for deterministic output
        kept_constant.sort(key=lambda e: e.get("id", ""))
        kept_dynamic.sort(key=lambda e: e.get("id", ""))
        constant_entries = kept_constant
        dynamic_entries = kept_dynamic

    # ---- build messages ------------------------------------------------
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    # constant block
    if constant_entries:
        parts: list[str] = ["[MEMORY:CONSTANT]"]
        for e in constant_entries:
            parts.append(f"\n## {e['id']}\n")
            parts.append(e["content"])
        parts.append("\n[/MEMORY:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    # dynamic block (ACTIVE only, no INDEX)
    dynamic_parts: list[str] = []
    if dynamic_entries:
        dynamic_parts.append("[MEMORY:ACTIVE]")
        for e in dynamic_entries:
            dynamic_parts.append(f"\n## {e['id']}\n")
            dynamic_parts.append(e["content"])
        dynamic_parts.append("\n[/MEMORY:ACTIVE]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs
