"""Tool visibility registration surface.

Why: some tools and delegate targets are only usable when external
configuration exists (e.g. an API channel). Hiding them used to be hard-coded
inside the inference loop. How: plugins register a visibility filter; the tool
list composer asks every filter which real tools and delegate targets to hide
for a node, and drops them before the model-visible list and the authorized
tool set are derived. Purpose: one generic, declarative point for runtime tool
availability, so the loop carries no feature-specific gating logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolVisibilityContext:
    """Data available to visibility filters when a node's tool list is composed."""

    node: Any = None
    workspace_root: Path | None = None


# filter(ctx) -> None | {"tools": iterable[str], "delegate_targets": iterable[str]}
# Returned names are hidden for this composition; None means "hide nothing".
VisibilityFilter = Callable[[ToolVisibilityContext], "dict[str, Any] | None"]


class ToolVisibilityRegistry:
    """Registry of filters that hide real tools / delegate targets per node."""

    def __init__(self) -> None:
        self._filters: dict[str, VisibilityFilter] = {}

    def register_filter(self, name: str, fn: VisibilityFilter) -> Callable[[], None]:
        """Register one visibility filter and return its disposer.

        Same-name re-registration replaces; the disposer removes only this
        exact registration, and is archived in the shared plugin ledger when
        one is attached (same contract as prompt sections).
        """
        clean = (name or "").strip()
        if not clean:
            raise ValueError("visibility filter name is required")
        if not callable(fn):
            raise TypeError(f"visibility filter is not callable: {clean}")
        self._filters[clean] = fn

        def _dispose() -> None:
            if self._filters.get(clean) is fn:
                self._filters.pop(clean, None)

        ledger = getattr(self, "_disposal_ledger", None)
        if ledger is not None:
            ledger.record(_dispose, key=("tool_visibility", self, clean))
        return _dispose

    def set_disposal_ledger(self, ledger: Any) -> None:
        """Attach the shared disposal ledger (see engine/registry_core.py)."""
        self._disposal_ledger = ledger

    def list_filters(self) -> list[str]:
        return sorted(self._filters)

    def hidden(self, node: Any, workspace_root: Path | str | None) -> tuple[set[str], set[str]]:
        """Return (hidden real tool names, hidden delegate target ids) for a node.

        Failures are isolated per filter: a broken filter hides nothing.
        """
        ctx = ToolVisibilityContext(
            node=node,
            workspace_root=Path(workspace_root) if workspace_root is not None else None,
        )
        tools: set[str] = set()
        targets: set[str] = set()
        for name in sorted(self._filters):
            try:
                result = self._filters[name](ctx)
            except Exception as exc:
                logger.warning("Tool visibility filter %r failed: %s", name, exc)
                continue
            if not isinstance(result, dict):
                continue
            tools.update(str(t) for t in (result.get("tools") or ()) if str(t).strip())
            targets.update(str(t) for t in (result.get("delegate_targets") or ()) if str(t).strip())
        return tools, targets


# Engine-process singleton, same pattern as prompt_section_registry.
tool_visibility_registry = ToolVisibilityRegistry()
