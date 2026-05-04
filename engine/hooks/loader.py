from __future__ import annotations

"""Loader for user-provided hook plugins.

[2026-05-03] Why: the hook system has built-in handlers, but users need a
stable extension point outside engine source files. How: scan one directory for
enabled Python files and call their register(hook_registry) function. Purpose:
allow local custom handlers to be installed at startup without changing the
engine package.
"""

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

from .registry import HookRegistry

logger = logging.getLogger(__name__)


def _default_plugin_meta(py_file: Path) -> dict:
    """Build default metadata for a plugin file."""
    # Why: existing plugins predate PLUGIN_META and must keep loading unchanged.
    # How: derive a stable name from the file stem and fill every documented
    # metadata field. Purpose: make list_plugins() complete for legacy plugins.
    return {
        "name": py_file.stem,
        "version": "unknown",
        "description": "",
        "author": "",
        "hooks": [],
    }


def _normalize_plugin_meta(py_file: Path, raw_meta: object) -> dict:
    """Merge optional PLUGIN_META fields with loader-owned defaults."""
    # Why: plugin authors may omit PLUGIN_META or only provide some fields.
    # How: copy defaults first, then overlay a declared dict and repair required
    # display fields when they are empty. Purpose: keep name and version present
    # while preserving any extra metadata keys a plugin chooses to publish.
    meta = _default_plugin_meta(py_file)
    if isinstance(raw_meta, dict):
        meta.update(raw_meta)
    elif raw_meta is not None:
        logger.warning("Plugin %s has non-dict PLUGIN_META, using defaults", py_file.name)
    if not meta.get("name"):
        meta["name"] = py_file.stem
    if not meta.get("version"):
        meta["version"] = "unknown"
    if meta.get("description") is None:
        meta["description"] = ""
    if meta.get("author") is None:
        meta["author"] = ""
    if meta.get("hooks") is None:
        meta["hooks"] = []
    return meta


def _is_enabled_python_plugin(path: Path) -> bool:
    """Return whether one filesystem entry should be imported as a plugin."""
    # Why: plugin directories may contain __init__.py, private helpers, examples,
    # and disabled files. How: accept only normal .py files that are not private
    # and do not end with .disabled. Purpose: avoid executing files that users did
    # not explicitly enable.
    if not path.is_file():
        return False
    if path.name.startswith("_"):
        return False
    if path.name.endswith(".disabled"):
        return False
    return path.suffix == ".py"


def _load_module_from_path(py_file: Path) -> ModuleType:
    """Import a plugin module from an arbitrary file path."""
    # Why: external plugins live in the workspace plugins/ directory, not in an
    # installed package. How: build an importlib spec directly from the file path.
    # Purpose: support simple drop-in plugin files while keeping import failures
    # isolated to the loader's try-except block.
    module_name = f"clonoth_external_plugin_{py_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, py_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {py_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_external_plugins(hook_registry: HookRegistry, plugins_dir: Path) -> int:
    """Load enabled external hook plugins from plugins_dir.

    Args:
        hook_registry: Registry that each plugin's register() function receives.
        plugins_dir: Directory containing drop-in plugin .py files.

    Returns:
        Number of plugin files whose register() function completed successfully.
    """
    count = 0
    plugins_dir = Path(plugins_dir)
    if not plugins_dir.is_dir():
        return count

    for py_file in sorted(plugins_dir.iterdir()):
        if not _is_enabled_python_plugin(py_file):
            continue
        try:
            module = _load_module_from_path(py_file)
            meta = _normalize_plugin_meta(py_file, getattr(module, "PLUGIN_META", {}))
            register = getattr(module, "register", None)
            if not callable(register):
                logger.warning(
                    "Plugin %s %s (%s) has no register() function, skipped",
                    meta["name"],
                    meta["version"],
                    py_file.name,
                )
                continue
            # Why: register_builtins() is already idempotent through handler-name
            # replacement. How: external plugins use the same HookRegistry API and
            # then publish normalized metadata through HookRegistry. Purpose:
            # repeated task startup can call this loader safely while list_plugins()
            # stays accurate.
            register(hook_registry)
            hook_registry.register_plugin_meta(meta)
            count += 1
            logger.info(
                "Loaded external plugin: %s %s (%s)",
                meta["name"],
                meta["version"],
                py_file.name,
            )
        except Exception as exc:
            # Why: user plugins are optional extensions and must not prevent the
            # engine from starting. How: log the failure and continue scanning.
            # Purpose: one bad plugin cannot disable all inference tasks.
            logger.error("Failed to load plugin %s: %s", py_file.name, exc, exc_info=True)
    return count
