from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .dynamic_context import _load_dynamic_context_vars
from ..conversation_store import MessageType, is_dynamic_context
from ..compact import INHERITABLE_INJECTED_HASH_PREFIXES
from engine.faces.prompt_sections import PromptSectionContext, content_sha1, prompt_section_registry
from ..attachments import build_multimodal_content
from ..node import Node
from ..prompt import assemble_prompt
from clonoth_runtime import get_int

_VOLATILE_PROMPT_DYNAMIC_LINE_RE = re.compile(
    r"^\s*(?:当前时间|Current\s+time|上下文使用率|Context\s+utilization|上下文占用|Context\s+usage)\s*[：:].*$",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_prompt_dynamic_for_hash(text: str) -> str:
    return "\n".join(line.rstrip() for line in _VOLATILE_PROMPT_DYNAMIC_LINE_RE.sub("", text).splitlines() if line.strip()).strip()


def collect_active_injected_hashes(messages: list[dict[str, Any]] | None) -> dict[str, str]:
    """Read live injected units from history, the sole source of deduplication state."""
    active: dict[str, str] = {}
    for message in messages or []:
        if is_dynamic_context(message):
            meta = message.get("_meta") or message.get("meta") or {}
            active.update(meta.get("injected_hashes", {}))
    return active


def collect_summarized_injected_hashes(messages: list[dict[str, Any]] | None) -> dict[str, str]:
    """Read skill/memory units inherited by compact summaries, not live bodies."""
    active: dict[str, str] = {}
    for message in messages or []:
        meta = message.get("_meta") or message.get("meta") or {}
        if message.get("message_type") == MessageType.SUMMARY or meta.get("message_type") == MessageType.SUMMARY or meta.get("source_task_id") == "compact_summary":
            active.update({key: value for key, value in meta.get("injected_hashes", {}).items() if key.startswith(INHERITABLE_INJECTED_HASH_PREFIXES)})
    return active


def has_current_task_anchor(messages: list[dict[str, Any]] | None, task_id: str) -> bool:
    """A surviving task segment already contains its dynamic context."""
    if not task_id:
        return False
    for message in messages or []:
        meta = message.get("_meta") or message.get("meta") or {}
        if (meta.get("source_task_id") or message.get("source_task_id")) == task_id:
            if message.get("role") in ("user", "assistant", "tool") or is_dynamic_context(message):
                return True
    return False


def _build_dynamic_message(parts: list[str], injected_hashes: dict[str, str], *, is_block_mode: bool) -> dict[str, Any]:
    prefix = (
        "Below is this turn's dynamic context; it may change between turns.\n\n"
        if is_block_mode
        else "Below is this turn's dynamic context information; it may change between turns. If it is irrelevant to the current task, ignore it and continue with your previous work.\n\n"
    )
    meta: dict[str, Any] = {"_dynamic": True, "message_type": MessageType.DYNAMIC_CONTEXT, "injected_hashes": injected_hashes}
    return {"role": "user", "content": prefix + "\n\n".join(parts), "_dynamic": True, "message_type": MessageType.DYNAMIC_CONTEXT, "_meta": meta}


def _collect_incremental_dynamic_parts(
    *, system_prompt: list[dict[str, Any]], is_block_mode: bool,
    skill_dynamic: list[dict[str, Any]], memory_dynamic: list[dict[str, Any]],
    section_entries: list[tuple[str, dict[str, str]]], active_injected: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    parts: list[str] = []
    new_hashes: dict[str, str] = {}
    if not is_block_mode and len(system_prompt) >= 2 and system_prompt[1].get("content"):
        text = system_prompt[1]["content"]
        normalized = _normalize_prompt_dynamic_for_hash(text)
        if normalized and active_injected.get("prompt_dynamic") != content_sha1(normalized):
            parts.append(text)
            new_hashes["prompt_dynamic"] = content_sha1(normalized)
    for scope, units in (("skill_block", skill_dynamic), ("memory_block", memory_dynamic)):
        for idx, unit in enumerate(units):
            text = str(unit.get("content") or "").strip()
            if text:
                hashes = unit.get("_injected_hashes") or {f"{scope}:{idx}": content_sha1(text)}
                if any(active_injected.get(key) != value for key, value in hashes.items()):
                    parts.append(text)
                    new_hashes.update(hashes)
    for text, hashes in section_entries:
        changed = {key: value for key, value in hashes.items() if active_injected.get(key) != value}
        if text and changed:
            parts.append(text)
            new_hashes.update(changed)
    return parts, new_hashes


def conversational_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude injected content, tools and rejection scaffolding from keyword scans."""
    result: list[dict[str, Any]] = []
    for message in history:
        if is_dynamic_context(message):
            continue
        meta = message.get("_meta") or message.get("meta") or {}
        if (message.get("message_type") or meta.get("message_type")) in ("tool_result", "retry_hint", "output_rejected") or meta.get("rejected_draft") or meta.get("is_rejection"):
            continue
        if message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            result.append(message)
        elif isinstance(content, list) and any(part.get("type") == "text" and part.get("text", "").strip() for part in content):
            result.append(message)
    return result


def assemble_messages_with_injections(
    *, workspace_root: Path, system_prompt: list[dict[str, Any]], history: list[dict[str, Any]], instruction: str,
    attachments: list[dict[str, Any]] | None = None, skill_static: list[dict[str, Any]] | None = None,
    skill_dynamic: list[dict[str, Any]] | None = None, memory_static: list[dict[str, Any]] | None = None,
    memory_dynamic: list[dict[str, Any]] | None = None, section_context: PromptSectionContext | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Build one canonical layout: stable prefix, history, User_N, Dynamic_N."""
    is_block_mode = any(message.get("role") == "history" for message in system_prompt)
    section_static = prompt_section_registry.render_messages("static", section_context) if section_context is not None else []
    section_entries = prompt_section_registry.render_dynamic_entries(section_context) if section_context is not None else []
    parts, hashes = _collect_incremental_dynamic_parts(
        system_prompt=system_prompt, is_block_mode=is_block_mode, skill_dynamic=skill_dynamic or [],
        memory_dynamic=memory_dynamic or [], section_entries=section_entries,
        active_injected=collect_active_injected_hashes(history),
    )
    messages: list[dict[str, Any]] = []
    post_blocks: list[dict[str, Any]] = []
    depth_blocks: list[dict[str, Any]] = []
    if is_block_mode:
        found_marker = False
        for block in system_prompt:
            if block.get("role") == "history":
                found_marker = True
            elif not found_marker:
                messages.append(block)
            elif "depth" in block:
                depth_blocks.append(block)
            else:
                post_blocks.append(block)
    elif system_prompt:
        messages.append(system_prompt[0])
    messages.extend(skill_static or [])
    messages.extend(memory_static or [])
    messages.extend(section_static)
    messages.extend(history)
    last = history[-1] if history else None
    if last is not None and last.get("role") == "user" and not is_dynamic_context(last) and isinstance(last.get("content"), str) and last["content"].strip() == instruction.strip():
        messages.pop()
    if attachments:
        messages.append({"role": "user", "content": build_multimodal_content(instruction, attachments, workspace_root=workspace_root)})
    elif instruction:
        messages.append({"role": "user", "content": instruction})
    if parts:
        messages.append(_build_dynamic_message(parts, hashes, is_block_mode=is_block_mode))
    messages.extend(post_blocks)
    for block in sorted(depth_blocks, key=lambda item: item["depth"], reverse=True):
        messages.insert(max(0, len(messages) - int(block["depth"])), {key: value for key, value in block.items() if key != "depth"})
    return messages, is_block_mode


def _estimate_history_prompt_tokens(history: list[dict[str, Any]]) -> int:
    from engine.builtin.compact import _seed_prompt_tokens
    from ..compact import estimate_message_tokens
    seeded = _seed_prompt_tokens(history)
    return max(0, int(seeded)) if seeded is not None else sum(estimate_message_tokens(message) for message in history)


def assemble_initial_messages(
    *, workspace_root: Path, runtime_cfg: dict[str, Any], node: Node, instruction: str,
    history: list[dict[str, Any]], task_context: dict[str, Any] | None = None, session_id: str = "",
    attachments: list[dict[str, Any]] | None = None, workspace_name: str = "",
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    prompt_vars = {"node_id": node.id, "node_name": node.name, "instruction": instruction}
    prompt_vars.update(_load_dynamic_context_vars(
        workspace_root, task_context=task_context, session_id=session_id, node_id=node.id,
        prompt_tokens=_estimate_history_prompt_tokens(history),
        compact_threshold=get_int(runtime_cfg, "engine.compact.threshold_tokens", 100_000, min_value=0),
    ))
    system_prompt = assemble_prompt(workspace_root, node, variables=prompt_vars)
    section_context = PromptSectionContext(
        workspace_root=workspace_root, node=node, session_id=session_id, history=history, instruction=instruction,
        task_context=task_context or {}, runtime_cfg=runtime_cfg, workspace_name=workspace_name,
    )
    messages, is_block_mode = assemble_messages_with_injections(
        workspace_root=workspace_root, system_prompt=system_prompt, history=history,
        instruction=instruction, attachments=attachments, section_context=section_context,
    )
    return messages, is_block_mode, system_prompt


def rebuild_dynamic_context(
    messages: list[dict[str, Any]], *, workspace_root: Path, node: Node, runtime_cfg: dict[str, Any],
    instruction: str, is_block_mode: bool, system_prompt: list[dict[str, Any]],
    session_id: str = "", workspace_name: str = "", task_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append only new units after resume/preempt, leaving the live prefix intact."""
    section_context = PromptSectionContext(
        workspace_root=workspace_root, node=node, session_id=session_id, history=messages, instruction=instruction,
        task_context=task_context or {}, runtime_cfg=runtime_cfg, workspace_name=workspace_name,
    )
    parts, hashes = _collect_incremental_dynamic_parts(
        system_prompt=system_prompt, is_block_mode=is_block_mode, skill_dynamic=[], memory_dynamic=[],
        section_entries=prompt_section_registry.render_dynamic_entries(section_context),
        active_injected=collect_active_injected_hashes(messages),
    )
    if not parts:
        return None
    message = _build_dynamic_message(parts, hashes, is_block_mode=is_block_mode)
    messages.append(message)
    return message
