"""Model list fetching shared by the channel settings page and the draw panel.

The backend calls the upstream on behalf of the browser so keys never reach it.
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

LIST_AUTHS = ("auto", "bearer", "x-api-key", "x-goog-api-key")
_DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
_DEFAULT_BASE_BY_TYPE = {"deepseek": "https://api.deepseek.com"}
_LABEL = {"openai": "OpenAI 兼容", "anthropic": "Anthropic", "gemini": "Gemini"}


class ModelListError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def family(kind: str) -> str:
    k = str(kind or "").strip().lower()
    if k == "anthropic":
        return "anthropic"
    if k.startswith("gemini"):
        return "gemini"
    return "openai"


def default_base_url(kind: str) -> str:
    k = str(kind or "").strip().lower()
    return _DEFAULT_BASE_BY_TYPE.get(k) or _DEFAULT_BASE[family(k)]


def filter_models(models: list[Any]) -> list[str]:
    """去重，保持上游顺序（生图 / 语音等模型也要能选，不按名字剔除）。"""
    unique: list[str] = []
    for item in models:
        text = str(item or "").strip()
        if text and text not in unique:
            unique.append(text)
    return unique


def extract_model_ids(data: Any) -> list[str]:
    """兼容 {data:[{id}]}（OpenAI / Anthropic）、{models:[{name:'models/x'}]}（Google）和纯数组。"""
    if isinstance(data, Mapping):
        items = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
    else:
        items = data
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            value = item
        elif isinstance(item, Mapping):
            value = str(item.get("id") or item.get("name") or "")
        else:
            continue
        value = value.strip()
        if value.startswith("models/"):
            value = value[len("models/"):]
        if value:
            out.append(value)
    return out


_API_VERSION_SUFFIX = re.compile(r"/v\d+[a-z0-9]*$", re.IGNORECASE)


def model_list_urls(base_url: str, prefix: str) -> list[str]:
    """地址已带版本段（/v1、/v1beta、/custom/v3）时只试 {base}/models；否则先试带前缀的再试不带的。"""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return []
    if _API_VERSION_SUFFIX.search(base):
        return [f"{base}/models"]
    return [f"{base}{prefix}/models", f"{base}/models"]


def resolve_list_auth(kind: str, base_url: str, choice: str = "auto") -> str:
    """模型列表的鉴权方式。auto：官方域名用原生头，其它地址（中转）用 Bearer。"""
    fam = family(kind)
    allowed = {"anthropic": ("bearer", "x-api-key"), "gemini": ("bearer", "x-goog-api-key")}.get(fam, ())
    if choice != "auto" and choice in allowed:
        return choice
    host = (urlsplit(base_url).hostname or "").lower()
    if fam == "anthropic" and (host == "anthropic.com" or host.endswith(".anthropic.com")):
        return "x-api-key"
    if fam == "gemini" and (host == "googleapis.com" or host.endswith(".googleapis.com")):
        return "x-goog-api-key"
    return "bearer"


def _error_snippet(response: Any) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        candidates = [error.get("message") if isinstance(error, Mapping) else None, payload.get("message"),
                      payload.get("detail"), error]
        for item in candidates:
            if isinstance(item, str) and item.strip():
                return item.strip()[:200]
    try:
        return str(response.text or "").strip()[:160]
    except Exception:  # noqa: BLE001
        return ""


async def pull_models(kind: str, base_url: str, api_key: str, *, auth: str = "auto",
                      client: Any = None, timeout: float = 20.0) -> list[str]:
    """向上游要模型列表。401 / 403 立刻停止，不换地址重试。"""
    import httpx

    fam = family(kind)
    base = (str(base_url or "").strip() or default_base_url(kind)).rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    api_key = str(api_key or "").strip()
    method = resolve_list_auth(kind, base, auth)
    headers = {"Accept": "application/json"}
    prefix = "/v1"
    if fam == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    if method == "x-goog-api-key" or (fam == "gemini" and base.lower().endswith("/v1beta")):
        prefix = "/v1beta"
    if api_key:
        if method == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers[method] = api_key

    owns_client = client is None
    if owns_client:
        # trust_env=False：与 engine 调模型时一致（不读环境里的代理设置），这里拉得到 = 调用时连得上。
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), trust_env=False)
    last: tuple[int, str, str] | None = None
    try:
        for url in model_list_urls(base, prefix):
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as error:
                last = (0, f"{type(error).__name__}: {error}".strip(": "), url)
                continue
            status = int(response.status_code)
            if status >= 400:
                last = (status, _error_snippet(response), url)
                if status in (401, 403):
                    break
                continue
            try:
                data = response.json()
            except ValueError:
                last = (status, "返回的不是 JSON", url)
                continue
            models = filter_models(extract_model_ids(data))
            if models:
                return models
            last = (status, "返回成功，但模型列表为空", url)
    finally:
        if owns_client:
            await client.aclose()
    label = _LABEL[fam]
    if last is None:
        raise ModelListError(f"{label} 拉取模型失败：未获取到模型列表。")
    status, detail, url = last
    raise ModelListError(f"{label} 拉取模型失败：{status or '网络错误'}" + (f"：{detail}" if detail else "") + f" ({url})", status)
