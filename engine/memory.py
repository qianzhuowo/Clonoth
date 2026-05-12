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

import threading
import time
from pathlib import Path
from typing import Any

# Why: memory activation must use the same matcher as skill activation.
# How: import shared helpers instead of maintaining a second local copy.
# Purpose: preserve behavior while removing duplicated keyword code.
from engine.knowledge_match import build_scan_text, compile_keyword, match_keywords

import yaml


# ---------------------------------------------------------------------------
#  Cache
# ---------------------------------------------------------------------------

# Why: _update_last_hit_bg runs in a daemon thread and may fire concurrently
# for the same book file. How: serialize all YAML writes through one lock.
# Purpose: prevent partial-write corruption without adding heavyweight flock.
_yaml_write_lock = threading.Lock()


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

                source = str(entry.get("source", "")).strip()
                created_at = str(entry.get("created_at", "")).strip()
                last_hit_at = str(entry.get("last_hit_at", "")).strip()

                items.append({
                    "book": book,
                    "id": eid,
                    "keywords": keywords,
                    "compiled_keywords": [compile_keyword(k) for k in keywords],
                    "content": content,
                    "constant": constant,
                    "priority": priority,
                    "scan_depth": scan_depth,
                    "node_ids": node_ids,
                    "source": source,
                    "created_at": created_at,
                    "last_hit_at": last_hit_at,
                })
        except Exception:
            continue

    _MemoryCache.put(workspace_root, items)
    return items


# ---------------------------------------------------------------------------
#  Hit-count tracking
# ---------------------------------------------------------------------------

def _update_last_hit_bg(workspace_root: Path, entries: list[dict[str, Any]]) -> None:
    """Update last_hit_at for matched entries. Debounced: skip if <24h old."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    threshold = 86400  # 24h debounce

    by_book: dict[str, list[str]] = {}
    for e in entries:
        b, eid = e.get("book", ""), e.get("id", "")
        if b and eid:
            by_book.setdefault(b, []).append(eid)
    mem_dir = memory_dir(workspace_root)
    for book, eids in by_book.items():
        bp = mem_dir / f"{book}.yaml"
        if not bp.exists():
            continue
        try:
            data = yaml.safe_load(bp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            eid_set = set(eids)
            changed = False
            for entry in data.get("entries", []):
                if not isinstance(entry, dict) or entry.get("id") not in eid_set:
                    continue
                old = entry.get("last_hit_at", "")
                if old:
                    try:
                        old_dt = datetime.fromisoformat(old)
                        if (now - old_dt).total_seconds() < threshold:
                            continue  # debounce: skip if <24h
                    except Exception:
                        pass
                entry["last_hit_at"] = now_iso
                changed = True
            if changed:
                with _yaml_write_lock:
                    bp.write_text(
                        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
                        encoding="utf-8",
                    )
        except Exception:
            continue
    _MemoryCache.invalidate(workspace_root)


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
    """Build system messages for memory injection through the unified pipeline."""
    # Why: memory filtering, matching, budgeting, rendering, and last_hit_at
    # tracking now share the same engine-level path used by skills. How:
    # normalize only memory catalog entries and disable skills in the shared
    # helper. Purpose: keep this legacy public function compatible without
    # retaining a separate memory injection implementation.
    from engine.builtin.knowledge_inject import build_knowledge_messages, normalize_memory_entries

    _skill_static, _skill_dynamic, memory_static, memory_dynamic = build_knowledge_messages(
        workspace_root,
        normalize_memory_entries(load_memory_catalog(workspace_root)),
        node_id=node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode="none",
        skill_allow=None,
        memory_mode=memory_mode,
        memory_allow=memory_allow,
        skill_max_budget_chars=0,
        memory_max_budget_chars=max_budget_chars,
        knowledge_max_budget_chars=0,
    )
    return memory_static, memory_dynamic
