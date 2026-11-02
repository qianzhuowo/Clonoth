"""Reverse WS listener hardening: non-loopback binds require an access token."""
from __future__ import annotations

import pytest

pytest.importorskip("nonebot")

from adapters.onebot.app import resolve_listen_config


def test_defaults_to_loopback_without_token():
    assert resolve_listen_config({}) == ("127.0.0.1", 8080, "")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_hosts_allowed_without_token(host):
    assert resolve_listen_config({"ONEBOT_HOST": host})[0] == host


@pytest.mark.parametrize("host", ["0.0.0.0", "172.17.0.1", "::", "example.internal"])
def test_non_loopback_without_token_is_refused(host):
    with pytest.raises(ValueError, match="ONEBOT_ACCESS_TOKEN"):
        resolve_listen_config({"ONEBOT_HOST": host, "ONEBOT_PORT": "8080"})


def test_non_loopback_with_token_is_allowed():
    env = {"ONEBOT_HOST": "172.17.0.1", "ONEBOT_PORT": "9001", "ONEBOT_ACCESS_TOKEN": " t0ken "}
    assert resolve_listen_config(env) == ("172.17.0.1", 9001, "t0ken")


def test_v11_alias_token_is_accepted():
    env = {"ONEBOT_HOST": "0.0.0.0", "ONEBOT_V11_ACCESS_TOKEN": "abc"}
    assert resolve_listen_config(env)[2] == "abc"


def test_conflicting_token_aliases_are_rejected():
    env = {"ONEBOT_ACCESS_TOKEN": "a", "ONEBOT_V11_ACCESS_TOKEN": "b"}
    with pytest.raises(ValueError, match="differ"):
        resolve_listen_config(env)


@pytest.mark.parametrize("flag", ["1", "true", "YES"])
def test_explicit_opt_out_allows_tokenless_bridge_bind(flag):
    env = {"ONEBOT_HOST": "172.17.0.1", "ONEBOT_ALLOW_NO_TOKEN": flag}
    assert resolve_listen_config(env) == ("172.17.0.1", 8080, "")


def test_opt_out_must_be_truthy():
    with pytest.raises(ValueError):
        resolve_listen_config({"ONEBOT_HOST": "0.0.0.0", "ONEBOT_ALLOW_NO_TOKEN": "0"})
