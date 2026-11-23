"""Memory write router for system nodes (dream + memory_extractor).

[AutoC 2026-08-28] Why: system.dream and system.memory_extractor organize/extract
memory entries for other nodes (ereuna_main, bootstrap.coder, etc.), but namespace
isolation causes their save_memory/delete_memory calls to write into their own
directories (data/memory/system.dream/, data/memory/system.memory_extractor/)
instead of the source node's directory.

How: register a before_tool_call hook that intercepts save_memory and delete_memory
when the calling node is a system node. Two routing modes:

1. Dream mode (_memory_route table): maps book name -> {ns, ws}. Dream's pipeline
   scans all node directories and builds this table at task creation time.

2. Extractor mode (_memory_route_ns): a single target namespace string. The
   memory_extract handler captures the source entry node_id when preparing the
   extraction and passes it through input_data.

Purpose: memory writes land in the correct node/workspace directory without
modifying save_memory or delete_memory tools themselves.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.hooks.types import Handler, HookContext, HookResult
from engine.inference.pseudo_tools import resolve_effective_action

log = logging.getLogger(__name__)

_SYSTEM_NODES = frozenset({"system.dream", "system.memory_extractor"})
_ROUTABLE_TOOLS = frozenset({"manage_memory", "save_memory", "delete_memory", "list_memories"})


class DreamMemoryRouter(Handler):
    """Redirect system node memory writes to the original source namespace."""

    name = "dream_memory_router"
    priority = 200  # Run before approval (100) so ctx is patched before execution

    async def handle(self, ctx: HookContext) -> HookResult | None:
        # Only intercept system node calls
        node = ctx.node
        if node is None:
            return None
        node_id = str(node.id or "").strip()
        if node_id not in _SYSTEM_NODES:
            return None

        # Only intercept save_memory / delete_memory
        tool_call = ctx.tool_call
        if tool_call is None:
            return None
        tool_name = str(tool_call.name or "").strip()
        if tool_name not in _ROUTABLE_TOOLS:
            return None

        # Get the tool_ctx reference from extra (added by ai_step.py)
        tool_ctx = ctx.extra.get("tool_ctx")
        tool_args = ctx.extra.get("tool_args") or {}
        if tool_ctx is None:
            return None

        rctx = ctx.rctx
        if rctx is None:
            return None
        task_context = rctx.task_context or {}

        # --- Route resolution ---
        target_ns = ""
        target_ws = ""

        # Mode 1: Dream route table (supports exact 'book/id' key and book key)
        _, eff_action = resolve_effective_action(tool_name, tool_args)
        memory_route: dict[str, Any] = task_context.get("_memory_route") or {}
        if memory_route:
            book = str(tool_args.get("book") or "default").strip()
            mid = str(tool_args.get("id") or "").strip()
            target = None
            if mid:
                target = memory_route.get(f"{book}/{mid}")
                if target is None and eff_action == "upsert":
                    target = memory_route.get(f"*/{mid}")
            if target is None:
                target = memory_route.get(book)
            if target is None:
                # Book not in route table (new book created by dream).
                # Fall back to the first route entry's namespace.
                _first = next((v for v in memory_route.values() if not v.get("ws")), None) or next(iter(memory_route.values()), None)
                if _first:
                    target = _first
            if target:
                target_ns = str(target.get("ns") or "").strip()
                if node_id == "system.dream" and eff_action == "upsert":
                    target_ws = ""
                else:
                    target_ws = str(target.get("ws") or "").strip()
        # Mode 2: Direct namespace from memory_extractor
        if not target_ns:
            _direct_ns = str(task_context.get("_memory_route_ns") or "").strip()
            if _direct_ns:
                target_ns = _direct_ns
                # [fix 2026-09-04] Dream preprocessing also carries the session's
                # workspace name; scope the write to the @ws directory when set.
                _direct_ws = str(task_context.get("_memory_route_ws") or "").strip()
                if _direct_ws:
                    target_ws = _direct_ws

        if not target_ns:
            return None

        # Rewrite ToolContext to redirect the write, preserving original caller node_id
        old_node_id = getattr(tool_ctx, "_node_id", "") or getattr(tool_ctx, "node_id", "")
        tool_ctx._caller_node_id = node_id  # type: ignore[attr-defined]
        tool_ctx._node_id = target_ns  # type: ignore[attr-defined]
        tool_ctx.node_id = target_ns

        # Clear _node_extra memory_book so _effective_memory_ns falls back to _node_id
        if hasattr(tool_ctx, "_node_extra"):
            tool_ctx._node_extra = {}  # type: ignore[attr-defined]

        # Dream consolidates writes into the source namespace, not a temporary workspace.
        if node_id == "system.dream" and eff_action == "upsert":
            target_ws = ""
        tool_ctx.workspace_name = target_ws.lstrip("@")

        log.info(
            "[dream_router] redirected %s book=%s from ns=%s to ns=%s ws=%s",
            tool_name,
            tool_args.get("book", "default"),
            old_node_id,
            target_ns,
            target_ws or "(node-level)",
        )
        return None  # Allow execution to proceed with patched context
