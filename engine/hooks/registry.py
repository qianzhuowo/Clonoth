from __future__ import annotations

import logging

from .types import Handler, HookContext, HookResult

logger = logging.getLogger(__name__)


def _copy_plugin_meta(meta: dict) -> dict:
    """Copy plugin metadata without assuming every extra value is deep-copyable."""
    # Why: PLUGIN_META has simple documented fields, but plugins may publish extra
    # values. How: copy the dict and clone the hooks list, which is the only
    # mutable documented field. Purpose: protect registry state without making
    # unusual extra values break plugin loading.
    copied = dict(meta)
    if isinstance(copied.get("hooks"), list):
        copied["hooks"] = list(copied["hooks"])
    return copied


class HookRegistry:
    """Registry for hook handlers grouped by hook point.

    Why: engine policy checks should be installable without more ai_step.py
    branches. How: store handlers by hook point and run them in priority order.
    Purpose: provide a small plugin surface for internal engine behavior.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[Handler]] = {}
        # Why: external plugin files can now declare module-level metadata that
        # should be visible after startup. How: keep one normalized dict per
        # plugin name. Purpose: expose loaded plugin information without changing
        # existing hook handler registration behavior.
        self._loaded_plugins: list[dict] = []

    def register(self, hook_point: str, handler: Handler) -> None:
        """Register one handler to a hook point.

        Why: run_ai_node calls built-in registration every task, so duplicate
        registrations would repeat side effects. How: remove an existing handler
        with the same name before appending the new instance. Purpose: make
        built-in registration idempotent while still allowing replacement.
        """
        handlers = self._hooks.setdefault(hook_point, [])
        handlers[:] = [h for h in handlers if h.name != handler.name]
        handlers.append(handler)
        handlers.sort(key=lambda h: h.priority, reverse=True)

    def unregister(self, hook_point: str, handler_name: str) -> bool:
        """Remove a handler by name and report whether anything changed."""
        handlers = self._hooks.get(hook_point)
        if not handlers:
            return False
        before = len(handlers)
        handlers[:] = [h for h in handlers if h.name != handler_name]
        if not handlers:
            self._hooks.pop(hook_point, None)
        return len(handlers) != before

    def register_plugin_meta(self, meta: dict) -> None:
        """Register or replace metadata for one loaded plugin."""
        # Why: run_ai_node can scan plugins repeatedly in a long-lived process.
        # How: replace a previous record with the same name instead of appending a
        # duplicate, while storing a copy of the caller's dict. Purpose: make
        # metadata registration idempotent like handler registration.
        stored = _copy_plugin_meta(meta)
        name = stored.get("name") or "unknown"
        stored["name"] = name
        for index, plugin in enumerate(self._loaded_plugins):
            if plugin.get("name") == name:
                self._loaded_plugins[index] = stored
                return
        self._loaded_plugins.append(stored)

    def list_plugins(self) -> list[dict]:
        """Return metadata for plugins that loaded successfully."""
        # Why: callers should be able to inspect plugin state without mutating the
        # registry internals. How: return copied metadata dicts and copied hook
        # lists. Purpose: keep the registry state owned by HookRegistry.
        return [_copy_plugin_meta(plugin) for plugin in self._loaded_plugins]

    async def fire(self, hook_point: str, ctx: HookContext) -> HookResult:
        """Run handlers for one hook point.

        Why: one failing handler should not break inference. How: catch and log
        handler exceptions, and stop only for explicit block/skip/action results.
        Purpose: keep hooks extensible while preserving the engine loop.
        """
        handlers = self._hooks.get(hook_point, [])
        modified = False
        for handler in handlers:
            try:
                result = await handler.handle(ctx)
                if result is None:
                    continue
                # Why: some handlers mutate HookContext but intentionally allow
                # the chain to continue. How: aggregate their modified flag until
                # a blocking/action result appears. Purpose: callers can still
                # observe non-terminal context edits after all handlers ran.
                modified = modified or bool(result.modified)
                if result.block or result.skip_step or result.action is not None:
                    if modified and not result.modified:
                        result.modified = True
                    return result
            except Exception as exc:
                logger.warning("Hook %s.%s failed: %s", hook_point, handler.name, exc)
        return HookResult(modified=modified)

    def list_hooks(self) -> dict[str, list[str]]:
        """Return registered hook points and handler names."""
        return {
            hook_point: [handler.name for handler in handlers]
            for hook_point, handlers in self._hooks.items()
        }
