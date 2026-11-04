"""Standalone NoneBot2 launcher for the Clonoth OneBot adapter.

This file starts a NoneBot2 reverse WebSocket server, registers the OneBot V11
adapter, and loads the existing plugin code from adapters/onebot/__init__.py.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path
from typing import Mapping

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


REPO_ROOT = Path(__file__).resolve().parents[2]


def _set_default_environment() -> None:
    """Set safe local defaults before the plugin reads its configuration."""
    os.environ.setdefault("CLONOTH_WORKSPACE", str(REPO_ROOT))
    os.environ.setdefault("CLONOTH_BASE_URL", os.environ.get("CLONOTH_URL", "http://127.0.0.1:8765"))
    os.environ.setdefault("CLONOTH_ENTRY_NODE", "qq.orchestrator")

    # NoneBot / OneBot reverse WebSocket defaults.
    # NapCat or another OneBot implementation should connect to:
    # ws://127.0.0.1:8080/onebot/v11/ws
    os.environ.setdefault("ONEBOT_HOST", "127.0.0.1")
    os.environ.setdefault("ONEBOT_PORT", "8080")


_TRUTHY = {"1", "true", "yes", "on"}


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_listen_config(env: Mapping[str, str]) -> tuple[str, int, str]:
    """Resolve (host, port, access_token) for the reverse WebSocket server.

    Anyone who can reach the listener can inject forged OneBot events (including
    messages that look like they come from an admin), so a non-loopback bind
    (e.g. the docker bridge gateway or 0.0.0.0) requires ONEBOT_ACCESS_TOKEN.
    ONEBOT_ALLOW_NO_TOKEN=1 is an explicit, temporary opt-out for migrations
    where the NapCat side has no token yet.
    """
    host = str(env.get("ONEBOT_HOST") or "127.0.0.1").strip()
    port = int(str(env.get("ONEBOT_PORT") or "8080").strip())
    primary = str(env.get("ONEBOT_ACCESS_TOKEN") or "").strip()
    alias = str(env.get("ONEBOT_V11_ACCESS_TOKEN") or "").strip()
    if primary and alias and primary != alias:
        raise ValueError("ONEBOT_ACCESS_TOKEN and ONEBOT_V11_ACCESS_TOKEN differ")
    token = primary or alias
    allow_no_token = str(env.get("ONEBOT_ALLOW_NO_TOKEN") or "").strip().lower() in _TRUTHY
    if not token and not _is_loopback(host) and not allow_no_token:
        raise ValueError(
            f"Refusing to listen on non-loopback {host}:{port} without ONEBOT_ACCESS_TOKEN "
            "(set the same token in NapCat's reverse WS config, or ONEBOT_ALLOW_NO_TOKEN=1 temporarily)"
        )
    return host, port, token


def main() -> None:
    _set_default_environment()
    sys.path.insert(0, str(REPO_ROOT))

    host, port, token = resolve_listen_config(os.environ)

    # Explicit so the token applies even when NoneBot does not pick it up from env.
    nonebot.init(driver="~fastapi", host=host, port=port, onebot_access_token=token or None)
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)

    nonebot.load_plugin("adapters.onebot")

    nonebot.run(host=host, port=port)


if __name__ == "__main__":
    main()
