from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from clonoth_runtime import get_int
from engine import memory as memory_runtime
from engine.knowledge_match import build_scan_text, match_keywords
from engine.node import Node
from toolbox import skills_runtime

# Why: engine.builtin handlers must not depend on the hook package after relocation.
# How: return a local HookResult-compatible shape instead. Purpose: avoid
# cycles while keeping the existing hook registry duck-typed.
from .result import hook_result


# Why: skill and memory injection now share one before_prompt_build handler.
# How: declare one metadata entry with the higher previous priority. Purpose:
# preserve hook discovery while removing the old two-step injector chain.
PLUGIN_META = {
    "handler_class": "KnowledgeInjector",
    "hook_points": [
        ("before_prompt_build", "handle"),
    ],
    "priority": 50,
}


def _short_text(s: str, max_chars: int = 240) -> str:
    """Return the same short skill description used by the legacy index."""
    # Why: skill INDEX rendering moved into this module. How: keep the old
    # truncation helper byte-compatible. Purpose: avoid changing INDEX text.
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


def _as_string_list(value: Any) -> list[str]:
    """Normalize loose catalog values into a list of non-empty strings."""
    # Why: catalogs are loaded from YAML/frontmatter and may contain either a
    # scalar or a list. How: mirror the existing loader coercion rules. Purpose:
    # let the unified pipeline accept both raw and already-normalized catalogs.
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_int(value: Any, default: int = 0) -> int:
    """Return integer metadata while preserving legacy numeric-only coercion."""
    # Why: priority, order, and scan_depth were accepted only when YAML parsed
    # them as numbers. How: keep that rule here instead of parsing arbitrary
    # strings. Purpose: prevent a subtle behavior change during normalization.
    if isinstance(value, (int, float)):
        return int(value)
    return default


def normalize_skill_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map skill catalog entries to the unified knowledge-entry dictionary."""
    # Why: skill and memory now share filtering, matching, budgeting, and
    # rendering orchestration. How: convert skill-specific fields into the
    # common Entry shape while preserving skill-only render metadata. Purpose:
    # make global skill/memory budget selection possible without merging storage.
    entries: list[dict[str, Any]] = []
    for raw in catalog or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue

        keywords = _as_string_list(raw.get("keywords"))
        raw_strategy = str(raw.get("strategy") or "normal").strip().lower()
        if raw_strategy == "constant":
            strategy = "constant"
        elif keywords:
            strategy = "keyword"
        else:
            strategy = "index"

        name = str(raw.get("name") or raw.get("id") or "").strip()
        if not name:
            continue

        entries.append({
            "id": name,
            "kind": "skill",
            "content": str(raw.get("body") or raw.get("content") or ""),
            "strategy": strategy,
            "keywords": keywords,
            "compiled_keywords": list(raw.get("compiled_keywords") or []),
            "priority": _as_int(raw.get("priority"), 0),
            "order": _as_int(raw.get("order"), 0),
            "scan_depth": max(0, _as_int(raw.get("scan_depth"), 0)),
            "node_ids": _as_string_list(raw.get("node_ids")),
            "description": str(raw.get("description") or ""),
            "path": str(raw.get("path") or ""),
            "book": "",
            "source": "",
            "created_at": "",
            "last_hit_at": "",
        })
    return entries


def normalize_memory_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map memory catalog entries to the unified knowledge-entry dictionary."""
    # Why: memory injection should use the same pipeline as skills, but memory
    # has no discovery INDEX. How: map constant memories to constant entries,
    # keyword memories to keyword entries, and skip keywordless non-constant
    # memories. Purpose: preserve existing memory visibility exactly.
    entries: list[dict[str, Any]] = []
    for raw in catalog or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue

        memory_id = str(raw.get("id") or "").strip()
        content = str(raw.get("content") or "").strip()
        if not memory_id or not content:
            continue

        keywords = _as_string_list(raw.get("keywords"))
        if bool(raw.get("constant", False)):
            strategy = "constant"
        elif keywords:
            strategy = "keyword"
        else:
            continue

        entries.append({
            "id": memory_id,
            "kind": "memory",
            "content": content,
            "strategy": strategy,
            "keywords": keywords,
            "compiled_keywords": list(raw.get("compiled_keywords") or []),
            "priority": _as_int(raw.get("priority"), 0),
            "order": 0,
            "scan_depth": max(0, _as_int(raw.get("scan_depth"), 0)),
            "node_ids": _as_string_list(raw.get("node_ids")),
            "description": "",
            "path": "",
            "book": str(raw.get("book") or ""),
            "source": str(raw.get("source") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "last_hit_at": str(raw.get("last_hit_at") or ""),
        })
    return entries


def _filter_entries(
    entries: list[dict[str, Any]],
    *,
    node_id: str = "",
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply node visibility and node-level skill/memory access rules."""
    # Why: both old builders performed the same node_id and allowlist filtering
    # in separate files. How: branch only on kind-specific access semantics.
    # Purpose: give the unified budget stage one already-authorized entry list.
    filtered: list[dict[str, Any]] = []
    skill_allow_set = set(skill_allow or []) if skill_allow is not None else None
    memory_allow_set = set(memory_allow or []) if memory_allow else None

    for entry in entries:
        node_ids = entry.get("node_ids") or []
        if node_id and node_ids and node_id not in node_ids:
            continue

        if entry.get("kind") == "skill":
            if skill_mode == "none":
                continue
            # Why: the legacy skill builder only skipped all skills for an empty
            # allowlist when allow was an explicit list, not None. How: preserve
            # that exact None-versus-empty distinction. Purpose: keep wrappers
            # and node config behavior backward compatible.
            if skill_mode == "allowlist" and skill_allow_set is not None and entry.get("id") not in skill_allow_set:
                continue
        elif entry.get("kind") == "memory":
            if memory_mode == "none":
                continue
            # Why: the legacy memory builder filtered allowlist only when the
            # allow collection was truthy. How: keep that condition here.
            # Purpose: knowledge.max_budget_chars=0 remains behavior-compatible.
            if memory_mode == "allowlist" and memory_allow_set is not None and entry.get("book") not in memory_allow_set:
                continue
        else:
            continue
        filtered.append(entry)
    return filtered


def _new_buckets() -> dict[str, list[dict[str, Any]]]:
    """Create render buckets shared by budget and rendering stages."""
    # Why: prompt layout still needs separate skill/static, skill/dynamic,
    # memory/static, and memory/dynamic messages. How: keep explicit buckets
    # after unified matching. Purpose: unify selection without changing labels.
    return {
        "skill_constant": [],
        "skill_active": [],
        "skill_index": [],
        "memory_constant": [],
        "memory_active": [],
    }


def _classify_and_match_entries(
    workspace_root: Path,
    entries: list[dict[str, Any]],
    *,
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify entries and run keyword activation for keyword entries."""
    # Why: the old builders both used constant/keyword/index phases. How: run
    # the same phases over unified entries and route results back to render
    # buckets by kind. Purpose: make later budgeting global while preserving
    # memory's lack of an INDEX block.
    buckets = _new_buckets()
    matched_memories: list[dict[str, Any]] = []

    for entry in entries:
        kind = entry.get("kind")
        strategy = entry.get("strategy")
        if strategy == "constant":
            if kind == "skill":
                buckets["skill_constant"].append(entry)
            elif kind == "memory":
                buckets["memory_constant"].append(entry)
            continue

        if strategy == "keyword":
            scan_text = build_scan_text(instruction_text, history, int(entry.get("scan_depth") or 0))
            if match_keywords(list(entry.get("compiled_keywords") or []), scan_text):
                if kind == "skill":
                    buckets["skill_active"].append(entry)
                elif kind == "memory":
                    buckets["memory_active"].append(entry)
                    matched_memories.append(entry)
            elif kind == "skill":
                buckets["skill_index"].append(entry)
            continue

        if strategy == "index" and kind == "skill":
            buckets["skill_index"].append(entry)

    if matched_memories:
        # Why: memory last_hit_at was updated immediately after keyword matches,
        # before budget truncation. How: keep a background daemon thread that
        # receives every matched memory entry. Purpose: preserve lifecycle data
        # even when a matched memory is later dropped by a budget limit.
        threading.Thread(
            target=memory_runtime._update_last_hit_bg,
            args=(workspace_root, matched_memories),
            daemon=True,
        ).start()

    return buckets


def _sort_render_buckets(buckets: dict[str, list[dict[str, Any]]]) -> None:
    """Sort buckets exactly as the legacy renderers did before budgeting."""
    # Why: prompt cache stability depends on deterministic ordering. How: sort
    # skills by (order, id) and memories by id, matching the old builders.
    # Purpose: keep render order stable after moving logic into one module.
    for key in ("skill_constant", "skill_active", "skill_index"):
        buckets[key].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    for key in ("memory_constant", "memory_active"):
        buckets[key].sort(key=lambda entry: str(entry.get("id") or ""))


def _apply_skill_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply the legacy skill-only budget to skill injectable buckets."""
    # Why: knowledge.max_budget_chars=0 must use the old independent skill
    # budget. How: select constant and active skills by priority and append
    # rejected skills to INDEX. Purpose: preserve both injected content and
    # discovery behavior for existing configs.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    for entry in buckets["skill_constant"]:
        all_injectable.append(("skill_constant", entry))
    for entry in buckets["skill_active"]:
        all_injectable.append(("skill_active", entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept_constant: list[dict[str, Any]] = []
    kept_active: list[dict[str, Any]] = []
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            if origin == "skill_constant":
                kept_constant.append(entry)
            else:
                kept_active.append(entry)
        else:
            buckets["skill_index"].append(entry)

    buckets["skill_constant"] = kept_constant
    buckets["skill_active"] = kept_active
    buckets["skill_constant"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["skill_active"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))


def _apply_memory_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply the legacy memory-only budget to memory injectable buckets."""
    # Why: the default path must keep memory's old independent budget. How:
    # select constant and active memories by priority and drop rejected memories
    # because memories never had an INDEX. Purpose: preserve existing memory
    # prompt output when no global knowledge budget is configured.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    for entry in buckets["memory_constant"]:
        all_injectable.append(("memory_constant", entry))
    for entry in buckets["memory_active"]:
        all_injectable.append(("memory_active", entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept_constant: list[dict[str, Any]] = []
    kept_active: list[dict[str, Any]] = []
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            if origin == "memory_constant":
                kept_constant.append(entry)
            else:
                kept_active.append(entry)

    buckets["memory_constant"] = kept_constant
    buckets["memory_active"] = kept_active
    buckets["memory_constant"].sort(key=lambda entry: str(entry.get("id") or ""))
    buckets["memory_active"].sort(key=lambda entry: str(entry.get("id") or ""))


def _apply_global_budget(buckets: dict[str, list[dict[str, Any]]], max_budget_chars: int) -> None:
    """Apply one priority-sorted budget pool across skills and memories."""
    # Why: Phase 3 requires high-priority memories and skills to compete in one
    # pool. How: rank all injectable entries by priority while remembering their
    # render bucket, then restore kept entries to their original labels. Purpose:
    # change budget selection without changing final tag names or prompt layout.
    if max_budget_chars <= 0:
        return

    all_injectable: list[tuple[str, dict[str, Any]]] = []
    # Why: equal-priority ties need deterministic behavior. How: start from the
    # prompt render order before the stable priority sort. Purpose: avoid random
    # output while still making priority the only cross-kind ranking key.
    for bucket_name in ("skill_constant", "memory_constant", "skill_active", "memory_active"):
        for entry in buckets[bucket_name]:
            all_injectable.append((bucket_name, entry))
    all_injectable.sort(key=lambda item: int(item[1].get("priority") or 0), reverse=True)

    kept: dict[str, list[dict[str, Any]]] = {
        "skill_constant": [],
        "skill_active": [],
        "memory_constant": [],
        "memory_active": [],
    }
    used = 0
    for origin, entry in all_injectable:
        body_len = len(str(entry.get("content") or ""))
        if used + body_len <= max_budget_chars:
            used += body_len
            kept[origin].append(entry)
        elif origin.startswith("skill_"):
            # Why: over-budget skill bodies used to remain discoverable through
            # SKILLS:INDEX. How: append rejected injectable skills to the index
            # bucket. Purpose: global budgeting does not hide skill metadata.
            buckets["skill_index"].append(entry)

    for bucket_name, entries in kept.items():
        buckets[bucket_name] = entries
    buckets["skill_constant"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["skill_active"].sort(key=lambda entry: (int(entry.get("order") or 0), str(entry.get("id") or "")))
    buckets["memory_constant"].sort(key=lambda entry: str(entry.get("id") or ""))
    buckets["memory_active"].sort(key=lambda entry: str(entry.get("id") or ""))


def _render_skill_messages(
    constant_skills: list[dict[str, Any]],
    dynamic_skills: list[dict[str, Any]],
    index_only_skills: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render skill buckets using the legacy SKILLS tags."""
    # Why: callers and tests depend on exact SKILLS tag names and section
    # structure. How: copy the old renderer's joins and header strings while
    # reading unified entry fields. Purpose: make the refactor output-compatible.
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    if constant_skills:
        parts: list[str] = ["[SKILLS:CONSTANT]"]
        for entry in constant_skills:
            parts.append(f"\n## Skill: {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/SKILLS:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    if dynamic_skills:
        dynamic_parts.append("[SKILLS:ACTIVE]")
        for entry in dynamic_skills:
            dynamic_parts.append(f"\n## Skill: {entry['id']}\n")
            dynamic_parts.append(str(entry.get("content") or ""))
        dynamic_parts.append("\n[/SKILLS:ACTIVE]")

    if index_only_skills:
        if dynamic_parts:
            dynamic_parts.append("")
        dynamic_parts.append("[SKILLS:INDEX]")
        dynamic_parts.append(
            "以下 skill 未被激活。如果当前任务需要，可通过 read_file 读取对应 path 的全文。"
        )
        for entry in index_only_skills:
            dynamic_parts.append(f"- name: {entry['id']}")
            dynamic_parts.append(f"  description: {_short_text(str(entry.get('description') or ''))}")
            dynamic_parts.append(f"  path: {entry.get('path') or ''}")
        dynamic_parts.append("[/SKILLS:INDEX]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs


def _render_memory_messages(
    constant_entries: list[dict[str, Any]],
    dynamic_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render memory buckets using the legacy MEMORY tags."""
    # Why: memories did not have an INDEX block and used different section
    # headers from skills. How: keep the old MEMORY renderer's exact tag names
    # and joins. Purpose: preserve prompt text for existing memory users.
    static_msgs: list[dict[str, str]] = []
    dynamic_msgs: list[dict[str, str]] = []

    if constant_entries:
        parts: list[str] = ["[MEMORY:CONSTANT]"]
        for entry in constant_entries:
            parts.append(f"\n## {entry['id']}\n")
            parts.append(str(entry.get("content") or ""))
        parts.append("\n[/MEMORY:CONSTANT]")
        static_msgs.append({"role": "system", "content": "\n".join(parts)})

    dynamic_parts: list[str] = []
    if dynamic_entries:
        dynamic_parts.append("[MEMORY:ACTIVE]")
        for entry in dynamic_entries:
            dynamic_parts.append(f"\n## {entry['id']}\n")
            dynamic_parts.append(str(entry.get("content") or ""))
        dynamic_parts.append("\n[/MEMORY:ACTIVE]")

    if dynamic_parts:
        dynamic_msgs.append({"role": "system", "content": "\n".join(dynamic_parts)})

    return static_msgs, dynamic_msgs


def build_knowledge_messages(
    workspace_root: Path,
    entries: list[dict[str, Any]],
    *,
    node_id: str = "",
    instruction_text: str = "",
    history: list[dict[str, Any]] | None = None,
    skill_mode: str = "all",
    skill_allow: list[str] | None = None,
    memory_mode: str = "all",
    memory_allow: list[str] | None = None,
    skill_max_budget_chars: int = 0,
    memory_max_budget_chars: int = 0,
    knowledge_max_budget_chars: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Build skill and memory messages from already-normalized entries."""
    # Why: build_knowledge_context and legacy wrappers need the same pipeline.
    # How: filter, classify, match, budget, and render unified entries in one
    # helper. Purpose: avoid reintroducing divergent skill and memory behavior.
    filtered_entries = _filter_entries(
        entries,
        node_id=node_id,
        skill_mode=skill_mode,
        skill_allow=skill_allow,
        memory_mode=memory_mode,
        memory_allow=memory_allow,
    )
    if not filtered_entries:
        return [], [], [], []

    buckets = _classify_and_match_entries(
        workspace_root,
        filtered_entries,
        instruction_text=instruction_text,
        history=history,
    )
    _sort_render_buckets(buckets)

    if knowledge_max_budget_chars > 0:
        _apply_global_budget(buckets, knowledge_max_budget_chars)
    else:
        _apply_skill_budget(buckets, skill_max_budget_chars)
        _apply_memory_budget(buckets, memory_max_budget_chars)

    skill_static, skill_dynamic = _render_skill_messages(
        buckets["skill_constant"],
        buckets["skill_active"],
        buckets["skill_index"],
    )
    memory_static, memory_dynamic = _render_memory_messages(
        buckets["memory_constant"],
        buckets["memory_active"],
    )
    return skill_static, skill_dynamic, memory_static, memory_dynamic


def build_knowledge_context(
    workspace_root: Path,
    node: Node,
    instruction_text: str,
    history: list[dict],
    runtime_cfg: dict,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return ``(skill_static, skill_dynamic, memory_static, memory_dynamic)``.

    Why: inference and preempt paths must not know how skill and memory builders
    are wired. How: load both catalogs, normalize them into one Entry shape, and
    run one filtering/matching/budget/render pipeline. Purpose: support global
    skill/memory budgeting while keeping the public context boundary stable.
    """
    safe_runtime_cfg = runtime_cfg or {}

    # Why: loaders still own their storage formats and caches. How: keep skill
    # and memory loading in their original modules, then normalize only the
    # in-memory records here. Purpose: unify injection without merging storage.
    entries = normalize_skill_entries(skills_runtime.load_skill_catalog(workspace_root))
    entries.extend(normalize_memory_entries(memory_runtime.load_memory_catalog(workspace_root)))

    return build_knowledge_messages(
        workspace_root,
        entries,
        node_id=node.id,
        instruction_text=instruction_text,
        history=history,
        skill_mode=node.skill_access.mode,
        skill_allow=node.skill_access.allow,
        memory_mode=node.memory_access.mode,
        memory_allow=node.memory_access.allow,
        skill_max_budget_chars=get_int(safe_runtime_cfg, "skills.max_budget_chars", 0, min_value=0),
        memory_max_budget_chars=get_int(safe_runtime_cfg, "memory.max_budget_chars", 0, min_value=0),
        knowledge_max_budget_chars=get_int(safe_runtime_cfg, "knowledge.max_budget_chars", 0, min_value=0),
    )


class KnowledgeInjector:
    """Glue handler for skill and memory prompt injection."""

    name = "knowledge_inject"
    priority = 50

    async def handle(self, ctx: Any) -> Any | None:
        """Build skill and memory messages, then optionally rebuild the prompt.

        Why: skill_inject.py and memory_inject.py duplicated prompt-hook glue and
        filtered conversation history separately. How: filter history once, call
        the unified knowledge builder with either independent or global budgets,
        and store the same ctx.extra keys as before. Purpose: unify ownership
        without changing prompt content, order, labels, or downstream contracts.
        """
        if ctx.node is None or ctx.rctx is None:
            return None

        from engine.inference.message_assembly import _conversational_history

        runtime_cfg = ctx.extra.get("runtime_cfg") or {}
        instruction_text = str(ctx.extra.get("instruction_text") or "")
        history = _conversational_history(ctx.extra.get("history") or [])

        # Why: this hook and the inference/preempt paths must share one builder
        # boundary. How: delegate to build_knowledge_context after filtering the
        # same conversation history as before. Purpose: remove duplicated builder
        # calls without changing ctx.extra keys or prompt layout.
        skill_static, skill_dynamic, memory_static, memory_dynamic = build_knowledge_context(
            ctx.rctx.workspace_root,
            ctx.node,
            instruction_text,
            history,
            runtime_cfg,
        )

        ctx.extra["skill_static_messages"] = skill_static
        ctx.extra["skill_dynamic_messages"] = skill_dynamic
        ctx.extra["memory_static_messages"] = memory_static
        ctx.extra["memory_dynamic_messages"] = memory_dynamic

        if ctx.extra.get("apply_injection"):
            _rebuild_prompt_messages(ctx)
            return hook_result(modified=True)
        return hook_result(modified=bool(skill_static or skill_dynamic or memory_static or memory_dynamic))


def _rebuild_prompt_messages(ctx: Any) -> None:
    """Rebuild ctx.messages with all prompt injections currently in ctx.extra.

    Why: the old SkillInjector helper was shared by MemoryInjector and therefore
    had to survive the merge. How: keep the same assemble_messages_with_injections
    call in the unified module and read the unchanged ctx.extra key names.
    Purpose: preserve the existing prompt layout while removing the old modules.
    """
    from engine.inference.message_assembly import assemble_messages_with_injections

    rebuilt, is_block_mode = assemble_messages_with_injections(
        workspace_root=ctx.rctx.workspace_root,
        system_prompt=list(ctx.extra.get("system_prompt") or []),
        history=list(ctx.extra.get("history") or []),
        instruction=str(ctx.extra.get("instruction_text") or ""),
        attachments=ctx.extra.get("attachments"),
        skill_static=list(ctx.extra.get("skill_static_messages") or []),
        skill_dynamic=list(ctx.extra.get("skill_dynamic_messages") or []),
        memory_static=list(ctx.extra.get("memory_static_messages") or []),
        memory_dynamic=list(ctx.extra.get("memory_dynamic_messages") or []),
    )
    ctx.messages[:] = rebuilt
    ctx.extra["is_block_mode"] = is_block_mode
