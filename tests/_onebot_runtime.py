"""Shared loader for tests that exercise the full ``adapters.onebot`` package.

Ported from the legacy single-file ``platform/onebot`` tests: every test gets a
freshly imported adapter package (module-level state, idempotency store and
config are re-created under ``CLONOTH_WORKSPACE=tmp_path``).

Isolation rules:
* nonebot is replaced by light-weight stubs (same as the legacy tests) so
  ``Message`` / ``MessageSegment`` stay plain list/dict values that the
  assertions can inspect.
* every ``adapters.onebot*`` entry already in ``sys.modules`` (e.g. the bare
  package registered by ``test_onebot_base_modules.py``) is removed through
  ``monkeypatch`` and restored afterwards; modules imported by the fresh load
  are dropped again on teardown so they never leak into other tests.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_PKG = "adapters.onebot"
SUBMODULES = (
    "access", "anon", "approval", "attachment_policy", "callbacks", "commands",
    "config", "custom_faces", "emoji_handler", "forward_bridge", "history",
    "inbound_attachments", "message_parse", "profiles", "routing", "send",
    "send_contract", "state", "voice",
)


def install_nonebot_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    class Matcher:
        def handle(self, *args: Any, **kwargs: Any):
            return lambda func: func

        def append_handler(self, *args: Any, **kwargs: Any):
            return lambda func: func

    class Driver:
        def on_startup(self, func=None):
            return (lambda value: value) if func is None else func

        def on_shutdown(self, func=None):
            return (lambda value: value) if func is None else func

    nonebot = types.ModuleType("nonebot")
    nonebot.get_bot = lambda *a, **k: None
    nonebot.get_driver = lambda: Driver()
    nonebot.on_message = lambda *a, **k: Matcher()
    nonebot.on_notice = lambda *a, **k: Matcher()

    class Segment(dict):
        @classmethod
        def image(cls, file: str):
            return cls(type="image", data={"file": file})

        @classmethod
        def text(cls, text: str):
            return cls(type="text", data={"text": text})

        @classmethod
        def reply(cls, message_id: Any):
            return cls(type="reply", data={"id": message_id})

        @classmethod
        def at(cls, user_id: Any):
            return cls(type="at", data={"qq": user_id})

        @property
        def type(self) -> str:
            return self["type"]

        @property
        def data(self) -> dict:
            return self["data"]

        def __add__(self, other: Any):
            return Message([self]) + other

    class Message(list):
        def __init__(self, value: Any = None):
            if value is None:
                value = []
            elif isinstance(value, (str, dict)):
                value = [value]
            super().__init__(value)

        def __add__(self, other: Any):
            return Message(list(self) + (list(other) if isinstance(other, list) else [other]))

    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    for name, value in {
        "Bot": object, "Event": object, "GroupMessageEvent": object,
        "GroupUploadNoticeEvent": object, "PrivateMessageEvent": object,
        "Message": Message, "MessageSegment": Segment,
    }.items():
        setattr(v11, name, value)
    exception = types.ModuleType("nonebot.adapters.onebot.v11.exception")
    exception.ActionFailed = type("ActionFailed", (RuntimeError,), {})
    rule = types.ModuleType("nonebot.rule")
    rule.Rule = lambda *a, **k: object()
    rule.to_me = lambda *a, **k: object()

    modules = {
        "nonebot": nonebot,
        "nonebot.adapters": types.ModuleType("nonebot.adapters"),
        "nonebot.adapters.onebot": types.ModuleType("nonebot.adapters.onebot"),
        "nonebot.adapters.onebot.v11": v11,
        "nonebot.adapters.onebot.v11.exception": exception,
        "nonebot.rule": rule,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _is_pkg_key(name: str) -> bool:
    return name == _PKG or name.startswith(_PKG + ".")


def load_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Import a fresh ``adapters.onebot`` and return its submodules.

    The returned namespace has ``pkg`` (the package ``__init__``) plus one
    attribute per submodule (``state``, ``send``, ``callbacks``, ...).
    Monkeypatch targets must be the module that *defines* the symbol.
    """
    install_nonebot_stubs(monkeypatch)
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    # The package __init__ prepends CLONOTH_WORKSPACE to sys.path.
    monkeypatch.setattr(sys, "path", list(sys.path))

    for name in [key for key in list(sys.modules) if _is_pkg_key(key)]:
        monkeypatch.delitem(sys.modules, name)
    adapters_pkg = importlib.import_module("adapters")
    monkeypatch.setattr(adapters_pkg, "onebot", None, raising=False)

    pkg = importlib.import_module(_PKG)
    modules = {name: importlib.import_module(f"{_PKG}.{name}") for name in SUBMODULES}

    # Register every freshly imported module through monkeypatch so teardown
    # removes it (and then restores whatever was there before).
    for name in [key for key in list(sys.modules) if _is_pkg_key(key)]:
        module = sys.modules.pop(name)
        monkeypatch.setitem(sys.modules, name, module)

    return SimpleNamespace(pkg=pkg, **modules)
