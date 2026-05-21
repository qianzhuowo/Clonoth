from __future__ import annotations

"""Auto-discovery loader for built-in hook handlers."""

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from toolbox.registry import ToolRegistry

logger = logging.getLogger(__name__)

_SKIP_MODULES = {"__init__", "loader", "result"}


def auto_discover_and_register(
    registry: Any,
    *,
    package: str = "engine.builtin",
    directory: Path | None = None,
    tool_registry: "ToolRegistry | None" = None,
) -> dict[str, Any]:
    """Scan built-in handler modules and register PLUGIN_META declarations.

    Why: built-in engine and supervisor hook handlers should not be wired through
    hard-coded registration functions. How: import each module under engine.builtin,
    read PLUGIN_META, instantiate its handler class, and register the declared
    methods on the provided registry. Purpose: keep handler placement, metadata,
    and registration in one file per handler.
    """
    base_dir = Path(directory) if directory is not None else Path(__file__).parent
    handlers: dict[str, Any] = {}
    if not base_dir.is_dir():
        return handlers

    for py_file in sorted(base_dir.glob("*.py")):
        if _should_skip(py_file):
            continue
        module_name = f"{package}.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
            meta = getattr(module, "PLUGIN_META", None)
            if not isinstance(meta, dict):
                continue
            instance = _instantiate_handler(module, meta)
            handler_name = str(getattr(instance, "name", "") or py_file.stem)
            handlers[handler_name] = instance
            priority = meta.get("priority", getattr(instance, "priority", None))
            for hook_point, method_name in _iter_hook_points(meta):
                method = getattr(instance, method_name)
                registry.register(str(hook_point), method, priority=priority)
            _register_declared_tools(module_name, meta, tool_registry)
            _register_meta(registry, py_file, meta, handler_name)
        except Exception as exc:
            logger.error("Failed to load built-in hook %s: %s", module_name, exc, exc_info=True)
    return handlers


def _should_skip(py_file: Path) -> bool:
    """Return whether a Python file should be ignored by built-in discovery."""
    # Why: helper modules and private files are not hook handlers. How: skip
    # reserved stems and underscore-prefixed files. Purpose: let result.py and the
    # loader itself coexist with discoverable handler modules.
    if not py_file.is_file() or py_file.suffix != ".py":
        return True
    if py_file.stem in _SKIP_MODULES:
        return True
    return py_file.name.startswith("_")


def _instantiate_handler(module: Any, meta: dict[str, Any]) -> Any:
    """Instantiate the handler class named by PLUGIN_META."""
    # Why: each handler module owns its class name through metadata. How: look up
    # handler_class and call it without arguments. Purpose: keep registration
    # declarative while preserving handler-owned state such as timers and cursors.
    class_name = str(meta.get("handler_class") or "").strip()
    if not class_name:
        raise ValueError("PLUGIN_META.handler_class is required")
    cls = getattr(module, class_name)
    return cls()


def _register_declared_tools(module_name: str, meta: dict[str, Any], tool_registry: "ToolRegistry | None") -> None:
    """Register PLUGIN_META.tools declarations into the provided ToolRegistry."""
    # Why: built-in plugins can now own their tool implementations and schemas.
    # How: when a ToolRegistry is provided, validate each metadata declaration and
    # pass it through register_builtin_tool(). Purpose: remove hard-coded knowledge
    # tools from toolbox.registry.py while keeping loader failures localized.
    raw_tools = meta.get("tools")
    if raw_tools is None:
        return
    if tool_registry is None:
        return
    if not isinstance(raw_tools, list):
        raise ValueError(f"{module_name} PLUGIN_META.tools must be a list")

    register_builtin_tool = getattr(tool_registry, "register_builtin_tool", None)
    if not callable(register_builtin_tool):
        raise TypeError("tool_registry must provide register_builtin_tool")

    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise ValueError(f"{module_name} has invalid tool declaration: {tool!r}")
        name = str(tool.get("name") or "").strip()
        description = str(tool.get("description") or "")
        input_schema = tool.get("input_schema")
        func = tool.get("func")
        if not name:
            raise ValueError(f"{module_name} tool declaration missing name")
        if not isinstance(input_schema, dict):
            raise ValueError(f"{module_name}.{name} input_schema must be a dict")
        if not callable(func):
            raise ValueError(f"{module_name}.{name} func must be callable")
        register_builtin_tool(name, description, input_schema, func)


def _iter_hook_points(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Validate and return hook point declarations from PLUGIN_META."""
    # Why: malformed metadata should fail one module clearly instead of registering
    # a partial handler. How: require a list of two-item declarations. Purpose:
    # keep auto-discovery predictable and easy to diagnose.
    raw_points = meta.get("hook_points")
    if not isinstance(raw_points, list):
        raise ValueError("PLUGIN_META.hook_points must be a list")
    points: list[tuple[str, str]] = []
    for item in raw_points:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"invalid hook point declaration: {item!r}")
        hook_point = str(item[0]).strip()
        method_name = str(item[1]).strip()
        if not hook_point or not method_name:
            raise ValueError(f"invalid hook point declaration: {item!r}")
        points.append((hook_point, method_name))
    return points


def _register_meta(registry: Any, py_file: Path, meta: dict[str, Any], handler_name: str) -> None:
    """Publish normalized built-in plugin metadata when the registry supports it."""
    # Why: HookRegistry can expose loaded plugin metadata to diagnostics. How:
    # register a copied display record after successful handler registration.
    # Purpose: make built-in auto-discovery observable without coupling the loader
    # to one concrete registry implementation.
    register_plugin_meta = getattr(registry, "register_plugin_meta", None)
    if not callable(register_plugin_meta):
        return
    display_meta = dict(meta)
    if isinstance(display_meta.get("tools"), list):
        # Why: tool declarations contain live callables that diagnostic metadata
        # should not expose. How: keep only small serializable tool records.
        # Purpose: HookRegistry.list_plugins() remains safe to inspect and encode.
        display_meta["tools"] = [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
            }
            for tool in display_meta["tools"]
            if isinstance(tool, dict)
        ]
    display_meta.setdefault("name", handler_name)
    display_meta.setdefault("version", "builtin")
    display_meta.setdefault("description", "")
    display_meta.setdefault("author", "core")
    display_meta.setdefault("hooks", [str(point[0]) for point in display_meta.get("hook_points", [])])
    display_meta.setdefault("module", py_file.stem)
    register_plugin_meta(display_meta)
