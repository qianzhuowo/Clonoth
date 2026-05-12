from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

# Why: skill and memory keyword matching now share one engine-level matcher.
# How: import the shared helpers instead of keeping local duplicate functions.
# Purpose: keep activation behavior identical across both injection paths.
from engine.knowledge_match import build_scan_text, compile_keyword, match_keywords

import yaml


# ---------------------------------------------------------------------------
#  Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from SKILL.md content.

    Returns ``(meta_dict, body_text)``.  When no valid frontmatter is
    found, returns ``({}, original_text)``.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    head = text[4:end]
    body = text[end + 5:]
    try:
        meta = yaml.safe_load(head) or {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _short_text(s: str, max_chars: int = 240) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


# ---------------------------------------------------------------------------
#  Catalog loading
# ---------------------------------------------------------------------------


class _SkillCache:
    """Per-workspace skill catalog cache keyed on file mtimes."""

    _lock = threading.Lock()
    _entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # ws_path -> (ts, items)
    _mtimes: dict[str, dict[str, float]] = {}  # ws_path -> {file_posix: mtime}
    _TTL = 2.0  # seconds before rechecking mtimes

    @classmethod
    def get(cls, workspace_root: Path) -> list[dict[str, Any]] | None:
        key = str(workspace_root)
        with cls._lock:
            entry = cls._entries.get(key)
            if entry is None:
                return None
            ts, items = entry
            if time.monotonic() - ts > cls._TTL:
                return None  # stale, caller should reload
            return items

    @classmethod
    def put(cls, workspace_root: Path, items: list[dict[str, Any]]) -> None:
        key = str(workspace_root)
        with cls._lock:
            cls._entries[key] = (time.monotonic(), items)


def load_skill_catalog(workspace_root: Path, *, _use_cache: bool = True) -> list[dict[str, Any]]:
    """Scan ``skills/*/SKILL.md`` and return metadata + body for each skill.

    Results are cached for up to 2 seconds to avoid repeated filesystem
    reads within the same request cycle.
    """
    if _use_cache:
        cached = _SkillCache.get(workspace_root)
        if cached is not None:
            return cached

    skills_dir = workspace_root / "skills"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta, body = parse_skill_frontmatter(text)
            name = str(meta.get("name") or skill_md.parent.name).strip() or skill_md.parent.name
            description = str(meta.get("description") or "").strip()
            enabled = bool(meta.get("enabled", True))

            strategy = str(meta.get("strategy") or "normal").strip().lower()
            if strategy not in ("constant", "normal"):
                strategy = "normal"

            raw_kw = meta.get("keywords")
            keywords: list[str] = []
            if isinstance(raw_kw, list):
                keywords = [str(k).strip() for k in raw_kw if isinstance(k, str) and str(k).strip()]
            elif isinstance(raw_kw, str) and raw_kw.strip():
                keywords = [raw_kw.strip()]

            order = 0
            if isinstance(meta.get("order"), (int, float)):
                order = int(meta["order"])
            priority = 0
            if isinstance(meta.get("priority"), (int, float)):
                priority = int(meta["priority"])
            scan_depth = 0
            if isinstance(meta.get("scan_depth"), (int, float)):
                scan_depth = max(0, int(meta["scan_depth"]))

            raw_node_ids = meta.get("node_ids")
            node_ids: list[str] = []
            if isinstance(raw_node_ids, list):
                node_ids = [str(n).strip() for n in raw_node_ids if isinstance(n, str) and str(n).strip()]
            elif isinstance(raw_node_ids, str) and raw_node_ids.strip():
                node_ids = [raw_node_ids.strip()]

            items.append({
                "name": name,
                "description": description,
                "enabled": enabled,
                "path": skill_md.relative_to(workspace_root).as_posix(),
                "strategy": strategy,
                "keywords": keywords,
                "compiled_keywords": [compile_keyword(k) for k in keywords],
                "order": order,
                "priority": priority,
                "scan_depth": scan_depth,
                "body": body.strip(),
                "node_ids": node_ids,
            })
        except Exception:
            continue
    _SkillCache.put(workspace_root, items)
    return items


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

def build_skill_messages(
    workspace_root: Path,
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    max_budget_chars: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build system messages for skill injection through the unified pipeline."""
    # Why: skill filtering, matching, budgeting, and rendering now live in the
    # engine-level knowledge injector so skills can share a global budget with
    # memories. How: normalize only skill catalog entries and disable memory in
    # the shared helper. Purpose: keep this legacy public function working while
    # preventing a second copy of the injection algorithm from drifting.
    from engine.builtin.knowledge_inject import build_knowledge_messages, normalize_skill_entries

    skill_static, skill_dynamic, _memory_static, _memory_dynamic = build_knowledge_messages(
        workspace_root,
        normalize_skill_entries(load_skill_catalog(workspace_root)),
        node_id=node_id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=skill_mode,
        skill_allow=skill_allow,
        memory_mode="none",
        memory_allow=None,
        skill_max_budget_chars=max_budget_chars,
        memory_max_budget_chars=0,
        knowledge_max_budget_chars=0,
    )
    return skill_static, skill_dynamic
