"""Regression tests for global provider_options configuration.

[AutoC 2026-06-01] These tests are written before the implementation because the
runtime already supports node-level provider_options, but provider-wide defaults
were not passed through. The tests pin the intended merge behavior so future
provider adapters can rely on one combined options dictionary.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Why: the project is tested directly from a source checkout. How: put the
# repository root on sys.path before importing local modules. Purpose: exercise
# the edited runtime and runner modules without requiring package installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clonoth_runtime import DEFAULT_RUNTIME_CONFIG  # noqa: E402
from engine.runner import _merge_provider_options  # noqa: E402


def test_default_openai_provider_declares_options_dict() -> None:
    """The default runtime config should expose a provider-wide options dict."""
    # Why: downstream runner code reads providers.openai.options unconditionally
    # when building the provider_options payload. How: assert the default schema
    # contains an empty dict. Purpose: old configs remain valid and new configs
    # have a documented place to put provider-wide options.
    assert DEFAULT_RUNTIME_CONFIG["providers"]["openai"]["options"] == {}


def test_provider_options_merge_keeps_global_defaults_and_node_overrides() -> None:
    """Node provider_options should override matching global provider options."""
    global_options = {
        "reasoning": {"effort": "medium", "summary": "auto"},
        "text": {"verbosity": "low"},
        "shared": "global",
    }
    node_options = {
        "reasoning": {"effort": "high"},
        "shared": "node",
        "node_only": True,
    }

    merged = _merge_provider_options(global_options, node_options)

    # Why: global provider options provide defaults, while node YAML must still be
    # able to specialize a single nested value. How: merge dictionaries recursively
    # and let node values win on conflicts. Purpose: avoid losing sibling options
    # such as reasoning.summary when a node changes only reasoning.effort.
    assert merged == {
        "reasoning": {"effort": "high", "summary": "auto"},
        "text": {"verbosity": "low"},
        "shared": "node",
        "node_only": True,
    }
    assert global_options["reasoning"]["effort"] == "medium"
    assert node_options["reasoning"] == {"effort": "high"}
