"""Treat a gateway error page delivered as a normal reply as a failed call.

Some reverse proxies (oai-reverse-proxy style) answer a failed upstream call with
HTTP 200 and a Markdown error page as the assistant text, e.g.

    ### **Proxy error (HTTP 400 Bad Request)**
    ...
    <!-- oai-proxy-error -->

Sent on, that page lands in the group chat. Here it becomes ``ok=False``. The
upstream status is kept when it is transient (429/5xx) so the normal retry runs;
otherwise the status stays 200 with an error, which the inference loop does not
retry but fallback_provider does try on the next model.
"""
from __future__ import annotations

import json
import re
from typing import Any

_PAGE_RE = re.compile(r"^\s*(?:#{1,6}\s*)?\*\*Proxy error \(HTTP (\d{3})[^)\n]*\)\*\*", re.IGNORECASE)
_MARKER = "<!-- oai-proxy-error -->"
_TRANSIENT = frozenset({429, 500, 502, 503, 504})


def _detail(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            err = json.loads(m.group(1)).get("error") or {}
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
        except ValueError:
            pass
    m = re.search(r"^\*(.+)\*\s*$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def mark_gateway_error_page(resp: Any) -> bool:
    """Turn ``resp`` into a failure if its text is a gateway error page. Returns True if marked."""
    if not getattr(resp, "ok", False) or getattr(resp, "tool_calls", None):
        return False
    text = getattr(resp, "text", None) or ""
    page = _PAGE_RE.match(text)
    if not page and not text.rstrip().endswith(_MARKER):
        return False
    upstream = int(page.group(1)) if page else 0
    detail = _detail(text)
    resp.ok = False
    resp.text = ""
    resp.status_code = upstream if upstream in _TRANSIENT else 200
    resp.error = f"gateway error page (HTTP {upstream or '?'})" + (f": {detail}" if detail else "")
    return True
