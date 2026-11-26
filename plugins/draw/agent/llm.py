"""场景 Agent（规划模型）的接口预设。

预设存在 tools/drawtools/settings.yaml 的 agent 段，这里负责：
- 规范化预设与整段设置（缺失时由旧的 planner.tool_protocol / planner.timeout_sec 生成一套“默认”预设）；
- 按预设创建模型调用对象（engine 进程规划时用，管理面板“测试连接”时也用）；
- 判定“整次规划失败后要不要重试、等多久”。

预设引用一个渠道方案（data/config.yaml channels）；没选方案时不接管，规划用节点自身的渠道（设置 → 节点模型）。
不在模块顶层 import providers：管理面板所在的 supervisor 进程只在真正需要时才加载它。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import clonoth_channels as chn

logger = logging.getLogger(__name__)

DEFAULT_PRESET_ID = "default"
DEFAULT_PRESET_NAME = "默认"
DEFAULT_TIMEOUT_SEC = 180
MAX_TIMEOUT_SEC = 3600
MAX_PRESETS = 100
MAX_NAME_LENGTH = 64

DEFAULT_PLANNER_MAX_RETRIES = 3
MAX_PLANNER_MAX_RETRIES = 10
RETRY_BASE_DELAY_SEC = 2.0
RETRY_MAX_DELAY_SEC = 30.0

TOOL_MODES = ("native", "text")
REASONING_MODES = ("inherit", "on", "off")
REASONING_EFFORTS = ("", "minimal", "low", "medium", "high", "xhigh", "max")

# 采样参数：key -> (默认值, 下限, 上限, 是否整数)。未启用的项不进请求。
SAMPLING_SPECS: dict[str, tuple[float, float | None, float | None, bool]] = {
    "temperature": (1.0, 0.0, 2.0, False),
    "top_p": (1.0, 0.0, 1.0, False),
    "top_k": (40, 1, None, True),
    "frequency_penalty": (0.0, -2.0, 2.0, False),
    "presence_penalty": (0.0, -2.0, 2.0, False),
    "repetition_penalty": (1.0, 0.0, None, False),
}

_GEMINI_BASE = "https://generativelanguage.googleapis.com"
_GEMINI_SAMPLING = ("temperature", "top_p", "top_k")
_GEMINI_REASONING_NOTE = "“关闭”与“跟随模型默认”相同：都不发送思考强度。"
# family 决定请求体怎么带采样参数、模型列表怎么鉴权。
PROVIDER_INFO: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI 兼容", "family": "openai", "default_base_url": "https://api.openai.com/v1",
        "sampling": tuple(SAMPLING_SPECS),
        "reasoning_note": "“关闭”会发送 reasoning_effort=none；接口不认这个值时会报错，改回“跟随模型默认”即可。",
    },
    "openai-responses": {
        "label": "OpenAI Responses", "family": "openai", "default_base_url": "https://api.openai.com/v1",
        "sampling": ("temperature", "top_p"),
        "reasoning_note": "“关闭”会发送 reasoning.effort=none。",
    },
    "anthropic": {
        "label": "Anthropic", "family": "anthropic", "default_base_url": "https://api.anthropic.com",
        "sampling": ("temperature", "top_p", "top_k"),
        "sampling_note": "只在 Reasoning 为“关闭”时发送：开着思考时接口不接受这些参数。",
        "reasoning_note": "“关闭”会发送 thinking=disabled；“跟随模型默认”为自适应思考。",
    },
    "gemini-aistudio": {
        "label": "Google AI Studio", "family": "gemini", "default_base_url": _GEMINI_BASE,
        "sampling": _GEMINI_SAMPLING, "reasoning_note": _GEMINI_REASONING_NOTE,
    },
    "gemini-vertex": {
        "label": "Gemini（Vertex 语义）", "family": "gemini", "default_base_url": _GEMINI_BASE,
        "sampling": _GEMINI_SAMPLING, "reasoning_note": _GEMINI_REASONING_NOTE,
    },
    "gemini": {
        "label": "Gemini（按地址判断后端）", "family": "gemini", "default_base_url": _GEMINI_BASE,
        "sampling": _GEMINI_SAMPLING, "reasoning_note": _GEMINI_REASONING_NOTE,
    },
    "deepseek": {
        "label": "DeepSeek", "family": "openai", "default_base_url": "https://api.deepseek.com",
        "sampling": ("temperature", "top_p", "frequency_penalty", "presence_penalty"),
        "reasoning_note": "“关闭”会发送 thinking=disabled；“跟随模型默认”为开启思考。",
    },
}
PROVIDER_ORDER = ("openai", "openai-responses", "anthropic", "gemini-aistudio", "gemini-vertex", "gemini", "deepseek")

# 重试也不会变好的失败：取消、配置 / 输入 / 提示词错误、我们自己的合同错误、自定义工具次数超限。
NON_RETRYABLE_CODES = frozenset({
    "REQUEST_ABORTED", "AGENT_PRESET_INVALID", "MODEL_MISSING", "PROVIDER_MISSING",
    "EMPTY_MESSAGE", "IMAGE_LIMIT_EXCEEDED", "PROMPT_EXPANSION_FAILED", "PREPARED_INPUT_INVALID",
    "TOOL_CONTRACT_INVALID", "CUSTOM_TOOL_LIMIT_EXCEEDED",
})
# 这些状态说明请求本身有问题（key、模型名、参数），重试只会白等。
NON_RETRYABLE_HTTP_STATUS = frozenset({400, 401, 403, 404, 405, 413, 422})


# ---- 规范化 ----

def _number(value: Any, default: float, low: float | None, high: float | None, integer: bool) -> float | int:
    number: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number):
        number = float(default)
    if low is not None:
        number = max(float(low), number)
    if high is not None:
        number = min(float(high), number)
    return int(number) if integer else number


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def provider_info(name: str) -> dict[str, Any]:
    """已知供应商的说明；未知的（第三方插件注册的）按 OpenAI 兼容处理，但不注入采样参数。"""
    key = str(name or "").strip().lower()
    info = PROVIDER_INFO.get(key)
    if info is not None:
        return info
    return {"label": key or "未选择", "family": "openai", "default_base_url": "", "sampling": ()}


def normalize_preset(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    sampling_src = src.get("sampling") if isinstance(src.get("sampling"), Mapping) else {}
    sampling: dict[str, dict[str, Any]] = {}
    for key, (default, low, high, integer) in SAMPLING_SPECS.items():
        item = sampling_src.get(key) if isinstance(sampling_src.get(key), Mapping) else {}
        sampling[key] = {"enabled": item.get("enabled") is True,
                         "value": _number(item.get("value"), default, low, high, integer)}
    reasoning_src = src.get("reasoning") if isinstance(src.get("reasoning"), Mapping) else {}
    preset_id = str(src.get("id") or "").strip()[:MAX_NAME_LENGTH] or f"agent-{uuid4().hex[:8]}"
    return {
        "id": preset_id,
        "name": str(src.get("name") or "").strip()[:MAX_NAME_LENGTH] or DEFAULT_PRESET_NAME,
        # 渠道方案 id（data/config.yaml channels）；空 = 跟随节点 draw.novelai_planner 的渠道。
        "channel": str(src.get("channel") or "").strip(),
        # 空 = 用方案自己的模型。
        "model": str(src.get("model") or "").strip(),
        # 0 = 不发送，由接口决定。
        "max_tokens": _number(src.get("max_tokens"), 0, 0, 1_000_000, True),
        # 0 = 不限时。
        "timeout_sec": _number(src.get("timeout_sec"), DEFAULT_TIMEOUT_SEC, 0, MAX_TIMEOUT_SEC, True),
        "tool_mode": _choice(src.get("tool_mode"), TOOL_MODES, "native"),
        "sampling": sampling,
        "reasoning": {
            "mode": _choice(reasoning_src.get("mode"), REASONING_MODES, "inherit"),
            "effort": _choice(reasoning_src.get("effort"), REASONING_EFFORTS, ""),
        },
    }


def normalize_planner_max_retries(value: Any, default: int = DEFAULT_PLANNER_MAX_RETRIES) -> int:
    """只认数字或非空数字串；None / '' / 布尔 / 负数都算“没填”，回落到默认值而不是 0。"""
    number: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number) or number < 0:
        return default
    return min(MAX_PLANNER_MAX_RETRIES, int(number))


def normalize_agent_settings(raw: Any, planner: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """settings.yaml 的 agent 段 → 完整结构；没有预设时由旧的 planner 设置生成一套“默认”。"""
    src = raw if isinstance(raw, Mapping) else {}
    presets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in src.get("presets") if isinstance(src.get("presets"), list) else []:
        if not isinstance(item, Mapping):
            continue
        preset = normalize_preset(item)
        if preset["id"] in seen:
            preset["id"] = f"agent-{uuid4().hex[:8]}"
        seen.add(preset["id"])
        presets.append(preset)
    if not presets:
        legacy = planner if isinstance(planner, Mapping) else {}
        presets = [normalize_preset({
            "id": DEFAULT_PRESET_ID, "name": DEFAULT_PRESET_NAME,
            "tool_mode": legacy.get("tool_protocol"), "timeout_sec": legacy.get("timeout_sec"),
        })]
    current = str(src.get("current_preset_id") or "").strip()
    if current not in {p["id"] for p in presets}:
        current = presets[0]["id"]
    return {
        "current_preset_id": current,
        "presets": presets,
        "planner_max_retries": normalize_planner_max_retries(src.get("planner_max_retries")),
    }


def current_preset(agent: Mapping[str, Any] | None) -> dict[str, Any]:
    presets = (agent or {}).get("presets") if isinstance((agent or {}).get("presets"), list) else []
    presets = [p for p in presets if isinstance(p, Mapping)]
    if not presets:
        return normalize_preset({"id": DEFAULT_PRESET_ID})
    current = str((agent or {}).get("current_preset_id") or "")
    return dict(next((p for p in presets if p.get("id") == current), presets[0]))


def preset_is_configured(preset: Mapping[str, Any] | None) -> bool:
    """选了渠道方案才接管规划；否则用节点自身的渠道（设置 → 节点模型）。"""
    return bool(str((preset or {}).get("channel") or "").strip())


def resolve_preset(preset: Mapping[str, Any], workspace_root: Path | str) -> dict[str, Any]:
    """预设 + 所选渠道方案 → 带 provider/base_url/api_key/model 的完整调用参数。"""
    cfg = chn.load_config(workspace_root)
    try:
        ch = chn.get_channel(cfg, str(preset.get("channel") or ""), model=str(preset.get("model") or ""),
                             root=workspace_root)
    except chn.ChannelError as error:
        raise ValueError(str(error)) from error
    return {**dict(preset), "provider": ch.type, "base_url": ch.base_url, "api_key": ch.api_key,
            "model": ch.model, "channel_name": ch.name}


# ---- 供应商 ----

def provider_names() -> list[str]:
    from providers import registry

    return list(registry.list())


def list_providers() -> list[dict[str, Any]]:
    """面板下拉用：已注册的供应商，常用的在前。"""
    names = provider_names()
    ordered = [n for n in PROVIDER_ORDER if n in names] + [n for n in names if n not in PROVIDER_ORDER]
    out: list[dict[str, Any]] = []
    for name in ordered:
        info = provider_info(name)
        out.append({
            "id": name, "label": info["label"], "default_base_url": info.get("default_base_url", ""),
            "sampling": list(info.get("sampling") or ()),
            "sampling_note": info.get("sampling_note", ""),
            "reasoning_note": info.get("reasoning_note", ""),
        })
    return out


def enabled_sampling(preset: Mapping[str, Any]) -> dict[str, Any]:
    """当前供应商支持、且已启用的采样参数。"""
    supported = provider_info(str(preset.get("provider") or "")).get("sampling") or ()
    sampling = preset.get("sampling") if isinstance(preset.get("sampling"), Mapping) else {}
    out: dict[str, Any] = {}
    for key in supported:
        item = sampling.get(key)
        if isinstance(item, Mapping) and item.get("enabled") is True:
            out[key] = item.get("value")
    return out


_MAX_COMPLETION_TOKENS_MODEL = re.compile(r"^(?:[\w.-]+/)?(?:o[1-9]|gpt-5)", re.IGNORECASE)


def build_provider_options(preset: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(preset.get("provider") or "")
    family = provider_info(provider)["family"]
    options: dict[str, Any] = {}
    max_tokens = int(preset.get("max_tokens") or 0)
    if max_tokens > 0:
        # OpenAI 的 o 系列 / gpt-5 只认 max_completion_tokens。
        key = "max_completion_tokens" if provider == "openai" and _MAX_COMPLETION_TOKENS_MODEL.match(
            str(preset.get("model") or "")) else "max_tokens"
        options[key] = max_tokens
    reasoning = preset.get("reasoning") if isinstance(preset.get("reasoning"), Mapping) else {}
    mode = str(reasoning.get("mode") or "inherit")
    effort = str(reasoning.get("effort") or "")
    if mode == "off":
        options["reasoning_effort"] = "none"
    elif mode == "on" and effort:
        options["reasoning_effort"] = effort
    if family == "gemini":
        # Gemini 供应商类自己从 provider_options 读这三项。
        options.update(enabled_sampling(preset))
    return options


def payload_extras(preset: Mapping[str, Any]) -> dict[str, Any]:
    """要并入请求体的采样参数（Gemini 走 provider_options，不在这里）。"""
    if provider_info(str(preset.get("provider") or ""))["family"] == "gemini":
        return {}
    return enabled_sampling(preset)


def _with_payload_extras(provider_cls: type, extras: Mapping[str, Any], *, only_without_thinking: bool) -> type:
    """供应商类本身不发采样参数：派生一个只在构造请求体时并入这些字段的子类。

    派生类不进 ProviderRegistry（注册只扫描 providers/ 目录），不影响其它节点。
    """
    if not extras:
        return provider_cls
    builder_name = "_build_payload"
    builder = getattr(provider_cls, builder_name, None)
    if not callable(builder):
        logger.warning("draw agent: %s 没有 %s，采样参数不会发送", provider_cls.__name__, builder_name)
        return provider_cls
    fields = dict(extras)

    def build(self: Any, *args: Any, **kwargs: Any) -> Any:
        payload = builder(self, *args, **kwargs)
        if not isinstance(payload, dict):
            return payload
        if only_without_thinking:
            thinking = payload.get("thinking")
            if not (isinstance(thinking, Mapping) and thinking.get("type") == "disabled"):
                return payload
        payload.update(fields)
        return payload

    return type(f"DrawPlanner{provider_cls.__name__}", (provider_cls,), {builder_name: build, "__module__": __name__})


def create_provider(preset: Mapping[str, Any], *, http: Any) -> Any:
    """按预设创建模型调用对象；预设不完整或供应商未注册时抛 ValueError。"""
    from providers import registry

    name = str(preset.get("provider") or "").strip().lower()
    provider_cls = registry.get(name)
    if provider_cls is None:
        raise ValueError(f"未知的 Provider：{name or '（空）'}（可用：{', '.join(registry.list())}）")
    model = str(preset.get("model") or "").strip()
    api_key = str(preset.get("api_key") or "").strip()
    if not model:
        raise ValueError("渠道方案没有模型")
    if not api_key:
        raise ValueError("没有填写 API Key")
    family = provider_info(name)["family"]
    cls = _with_payload_extras(provider_cls, payload_extras(preset), only_without_thinking=family == "anthropic")
    kwargs: dict[str, Any] = {
        "http": http, "api_key": api_key, "base_url": str(preset.get("base_url") or "").strip() or None,
        "model": model, "provider_options": build_provider_options(preset),
    }
    params = inspect.signature(cls).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return cls(**kwargs)


# ---- 拉取模型 ----

# ---- 测试连接 ----

CONNECTION_TEST_TIMEOUT_SEC = 60.0
CONNECTION_TEST_MESSAGES = [{"role": "user", "content": "只回复两个字母：OK"}]


async def check_connection(preset: Mapping[str, Any], *, http: Any = None) -> dict[str, Any]:
    """用预设发一次最小请求。预设本身不能用时抛 ValueError；请求失败返回 ok=False 与原因。"""
    import httpx

    owns_client = http is None
    if owns_client:
        http = httpx.AsyncClient(timeout=httpx.Timeout(CONNECTION_TEST_TIMEOUT_SEC, connect=10.0), trust_env=False)
    try:
        provider = create_provider(preset, http=http)
        configured = float(preset.get("timeout_sec") or 0)
        limit = min(configured, CONNECTION_TEST_TIMEOUT_SEC) if configured > 0 else CONNECTION_TEST_TIMEOUT_SEC
        base = {"provider": getattr(provider, "name", ""), "model": getattr(provider, "model", "")}
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(provider.chat(messages=list(CONNECTION_TEST_MESSAGES), tools=None), timeout=limit)
        except asyncio.TimeoutError:
            return {**base, "ok": False, "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "error": f"{limit:.0f} 秒内没有响应"}
        except Exception as error:  # noqa: BLE001
            return {**base, "ok": False, "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "error": f"{type(error).__name__}: {error}"}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not getattr(response, "ok", False):
            return {**base, "ok": False, "elapsed_ms": elapsed_ms,
                    "status_code": getattr(response, "status_code", None),
                    "error": str(getattr(response, "error", "") or "未知错误")[:500]}
        return {**base, "ok": True, "elapsed_ms": elapsed_ms,
                "reply": str(getattr(response, "text", "") or "").strip()[:120],
                "usage": dict(getattr(response, "usage", None) or {})}
    finally:
        if owns_client:
            await http.aclose()


# ---- 失败重试 ----

def is_retryable_planner_error(error: BaseException | None) -> bool:
    """超时、供应商 / 网络错误、工具调用最终仍不合格都值得整次重试；取消和配置类错误不重试。"""
    if error is None:
        return False
    if str(getattr(error, "code", "") or "").upper() in NON_RETRYABLE_CODES:
        return False
    details = getattr(error, "details", None)
    status = details.get("status_code") if isinstance(details, Mapping) else None
    return not (isinstance(status, int) and not isinstance(status, bool) and status in NON_RETRYABLE_HTTP_STATUS)


def planner_retry_delay(retry_number: int, rand: Callable[[], float] = random.random) -> float:
    """第 n 次重试前等待的秒数：2 × 2^(n-1)，封顶 30，再乘 0.8–1.2 的抖动。"""
    exponent = max(0, int(retry_number or 1) - 1)
    raw = min(RETRY_MAX_DELAY_SEC, RETRY_BASE_DELAY_SEC * (2 ** exponent))
    jitter = 0.8 + 0.4 * min(1.0, max(0.0, float(rand())))
    return raw * jitter
