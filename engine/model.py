from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

import clonoth_channels as chn

from .node import Node

_current_sticky_sid_var: ContextVar[str] = ContextVar("_current_sticky_sid_var", default="")


def parse_sticky_session_config(raw: Any) -> dict[str, Any] | None:
    """Normalize sticky_session config from bool or dict.

    Returns:
      - None if not configured
      - {"enabled": False, ...} if explicitly disabled
      - {"enabled": True, "header": str, "ip_prefix": str} if enabled
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return {
            "enabled": raw,
            "header": "X-Forwarded-For",
            "ip_prefix": "10",
        }
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled", True))
        header = str(raw.get("header") or "X-Forwarded-For").strip() or "X-Forwarded-For"
        ip_prefix = str(raw.get("ip_prefix") or "10").strip().strip(".") or "10"
        return {
            "enabled": enabled,
            "header": header,
            "ip_prefix": ip_prefix,
        }
    return None


def compute_sticky_ip(session_key: str, ip_prefix: str = "10") -> str:
    """Deterministically map a session_key into an IPv4 string under ip_prefix."""
    clean_prefix = str(ip_prefix or "10").strip().strip(".")
    parts = [p.strip() for p in clean_prefix.split(".") if p.strip()]
    octets: list[int] = []
    for p in parts[:4]:
        try:
            val = int(p)
            if 0 <= val <= 255:
                octets.append(val)
        except ValueError:
            pass
    if not octets:
        octets = [10]
    if len(octets) >= 4:
        return ".".join(str(x) for x in octets[:4])

    digest = hashlib.md5(str(session_key or "default").encode("utf-8")).digest()
    needed = 4 - len(octets)
    for i in range(needed):
        b = digest[i]
        if i == needed - 1:
            # Avoid .0 and .255 on final host octet
            octets.append(1 + (b % 254))
        else:
            octets.append(b)
    return ".".join(str(x) for x in octets)


class StickyHttpProxy:
    """Lightweight httpx.AsyncClient proxy that injects a sticky session header
    into post/get/request/stream/build_request/send calls while sharing the
    underlying connection pool.
    """

    def __init__(self, client: Any, header_name: str, header_value: str):
        self._client = client
        self._header_name = header_name
        self._header_value = header_value

    def _merge_headers(self, headers: Any) -> dict[str, Any]:
        merged: dict[str, Any] = dict(headers) if headers else {}
        lower_keys = {str(k).lower() for k in merged.keys()}
        if self._header_name.lower() not in lower_keys:
            merged[self._header_name] = self._header_value
        return merged

    async def post(self, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.post(url, **kwargs)

    async def get(self, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.get(url, **kwargs)

    async def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.request(method, url, **kwargs)

    def stream(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._client.stream(method, url, **kwargs)

    def build_request(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._client.build_request(method, url, **kwargs)

    async def send(self, request: Any, **kwargs: Any) -> Any:
        if hasattr(request, "headers") and self._header_name not in request.headers:
            request.headers[self._header_name] = self._header_value
        return await self._client.send(request, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def wrap_http_with_sticky_session(
    http: Any,
    sticky_cfg_raw: Any,
    session_key: str,
) -> Any:
    """Wrap an httpx.AsyncClient with StickyHttpProxy if sticky_cfg_raw is enabled."""
    if http is None or not session_key:
        return http
    parsed = parse_sticky_session_config(sticky_cfg_raw)
    if not parsed or not parsed.get("enabled"):
        return http
    base_client = getattr(http, "_client", http)
    sticky_ip = compute_sticky_ip(session_key, parsed["ip_prefix"])
    return StickyHttpProxy(base_client, parsed["header"], sticky_ip)


@dataclass
class ResolvedProvider:
    model: str
    # provider_type 是 ProviderRegistry 的 key（渠道方案的 type）。
    provider_type: str
    api_key: str = ""
    base_url: str = ""
    provider_options: dict[str, Any] = field(default_factory=dict)
    channel_id: str = ""


def resolve_provider(
    workspace_root: Path,
    node: Node,
    session_override: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> ResolvedProvider:
    """节点要用的渠道：系统槽位 / node_models / 当前方案，再叠加会话覆盖（clonoth_channels）。

    Raises clonoth_channels.ChannelError when no scheme is configured.
    """
    cfg = cfg if cfg is not None else chn.load_config(workspace_root)
    ch = chn.node_channel(cfg, node.id, root=workspace_root)
    override = session_override if isinstance(session_override, dict) else {}
    ch = chn.apply_session_override(cfg, ch, override, root=workspace_root)

    po: dict[str, Any] = {k: v for k, v in ch.options.items() if k != "sticky_session"}
    if "sticky_session" in ch.options:
        po["_provider_sticky_session"] = ch.options["sticky_session"]
    raw_override_po = override.get("provider_options")
    if isinstance(raw_override_po, dict):
        po.update(raw_override_po)
    for key in ("reasoning_field", "reasoning_effort", "max_tokens", "max_completion_tokens", "thought_summary"):
        if override.get(key) is not None:
            po[key] = override[key]
    raw_override_tc = override.get("thinking_config")
    if isinstance(raw_override_tc, dict):
        existing_tc = po.get("thinking_config")
        po["thinking_config"] = {**existing_tc, **raw_override_tc} if isinstance(existing_tc, dict) else dict(raw_override_tc)

    cur_sid = _current_sticky_sid_var.get()
    if cur_sid and getattr(node, "persistent", False) and not cur_sid.endswith(f":{node.id}"):
        po.setdefault("_sticky_session_id", f"{cur_sid}:{node.id}")

    return ResolvedProvider(model=ch.model, provider_type=ch.type, api_key=ch.api_key,
                            base_url=ch.base_url, provider_options=po, channel_id=ch.id)
