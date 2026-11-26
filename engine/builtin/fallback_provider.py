"""Fallback Provider Plugin — auto-switch to backup API on primary failure.

Why: when the primary LLM provider exhausts retries (429/5xx), the task fails
with no recourse. How: hook into after_llm_call, detect non-OK responses for
retryable status codes, and replay the same request against fallback providers
listed in data/config.yaml. Purpose: improve availability without modifying
core llm_call.py or ai_step.py.
"""
from __future__ import annotations

import inspect
import logging
import time
from pathlib import Path
from typing import Any

from .result import hook_result
import clonoth_channels as chn
from engine.protocol import TaskAction
from engine.vision import model_has_vision


logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

PLUGIN_META = {
    "handler_class": "FallbackProviderHandler",
    "hook_points": [
        ("after_llm_call", "handle"),
    ],
    "priority": -20,  # run after error_snapshot (-10)
    "name": "fallback_provider",
    "description": "Primary API failure auto-fallback to backup providers",
}


def _is_retryable(status_code: int | None, error: str | None = None) -> bool:
    """Check if the error is worth retrying on a different provider.

    Covers both HTTP-level errors (429/5xx) and upstream provider errors
    that arrive as ok=False with HTTP 200 (e.g. content_filter, safety
    blocks, rate limits, empty responses via SSE error objects).
    """
    # Any response marked ok=False that reaches us is worth retrying,
    # unless it's a definitive client error (4xx other than 429).
    if status_code is None:
        return True  # unknown / connection-level error
    if status_code in _RETRYABLE_STATUS_CODES:
        return True
    # HTTP 200 but ok=False → upstream signaled error through body/SSE
    # (content_filter, safety block, quota via SSE error object, etc.)
    if status_code == 200 and error:
        return True
    # 4xx (except 429 and certain content moderation 403s) are client errors — don't retry
    # 403 can be either a true auth rejection or a content moderation refusal (safeguards).
    # Content moderation refusals are worth retrying on a different provider (e.g., Claude
    # cyber safeguard → fallback to DeepSeek); auth rejections are not.
    _CONTENT_MODERATION_KEYWORDS = {
        'safeguard', 'content_filter', 'refused', 'safety', 'cyber',
        'not allowed', 'community guidelines', 'moderation',
    }
    # Each fallback is its own channel (address + key + model): a rejected key,
    # exhausted balance or a model this gateway route lacks says nothing about the next one.
    if status_code in (401, 402, 404):
        return True
    if status_code == 403 and error:
        err_lower = error.lower()
        if any(kw in err_lower for kw in _CONTENT_MODERATION_KEYWORDS):
            return True  # content moderation → retryable on different provider
        return False  # auth/permissions rejection → don't retry
    if 400 <= status_code < 500:
        return False
    return True  # anything else (1xx, 3xx, unknown) → try fallback


def _messages_have_images(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for msg in messages or []
        if isinstance(msg.get("content"), list)
        for part in msg["content"]
    )


def _create_fallback_provider(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    http: Any,
    provider_options: dict[str, Any] | None = None,
) -> Any | None:
    """Instantiate a provider by type string using ProviderRegistry.

    Supports any provider registered in providers/__init__.py — no hardcoding.
    """
    try:
        from providers import registry
        provider_cls = registry.get(provider_type)
        if provider_cls is None:
            logger.warning("fallback_provider: unknown provider type '%s' (available: %s)",
                           provider_type, registry.list())
            return None
        # Build kwargs — different providers accept different params
        # Share the engine-managed HTTP client; do not allocate an unclosed
        # client for every replacement. Provider constructors differ slightly.
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,
            "http": http,
            "provider_options": provider_options or {},
        }
        params = inspect.signature(provider_cls).parameters
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        return provider_cls(**kwargs)
    except Exception as exc:
        logger.warning("fallback_provider: failed to create %s provider: %s", provider_type, exc)
        return None


class FallbackProviderHandler:
    """After-LLM-call hook: replay failed requests on fallback providers."""

    name = "fallback_provider"
    priority = -20

    async def handle(self, ctx: Any) -> Any | None:
        resp = ctx.response
        if resp is None or getattr(resp, "ok", True):
            return None  # success or no response, don't intervene

        status_code = getattr(resp, "status_code", None)
        original_error = getattr(resp, "error", None) or ""
        if not _is_retryable(status_code, original_error):
            logger.debug(
                "fallback_provider: skipping non-retryable error (status=%s)",
                status_code,
            )
            return None

        # Get workspace root from rctx
        rctx = getattr(ctx, "rctx", None)
        workspace_root = getattr(rctx, "workspace_root", None) if rctx else None
        if not workspace_root:
            return None

        fallbacks_raw = chn.fallback_channels(chn.load_config(workspace_root), root=workspace_root)
        if not fallbacks_raw:
            return None

        original_error = getattr(resp, "error", "unknown")
        messages = ctx.messages
        original_provider = ctx.provider
        original_model = getattr(original_provider, "model", "unknown")

        call_provider = ctx.extra.get("call_provider")
        if not callable(call_provider):
            logger.error("fallback_provider: call_provider capability unavailable")
            return None
        # Safe audit data only. Raw errors and endpoint addresses stay out of
        # delivery metadata; the plugin owns the meaning, the host only transports it.
        requested = {"provider": getattr(original_provider, "name", "unknown"), "model": original_model}
        attempts = []

        logger.warning(
            "fallback_provider: primary failed (status=%s error=%s model=%s), "
            "trying %d fallback(s)",
            status_code, original_error, original_model, len(fallbacks_raw),
        )

        # [fix 2026-05-28] Collect per-fallback error details so that when all
        # fallbacks fail, the user sees what was attempted and why each failed,
        # instead of only seeing the original provider's error message.
        fallback_errors: list[str] = []

        # Try each fallback in chain order
        for i, fb_ch in enumerate(fallbacks_raw):
            fb_provider_type = fb_ch.type
            fb_base_url = fb_ch.base_url
            fb_api_key = fb_ch.api_key
            fb_model = fb_ch.model or original_model
            fb_options = {k: v for k, v in fb_ch.options.items() if k != "sticky_session"}

            _same_provider = fb_ch.id == getattr(original_provider, "channel_id", "")
            if (
                len(fallbacks_raw) > 1
                and _same_provider
                and fb_model == original_model
            ):
                logger.info(
                    "fallback_provider: skipping fallback[%d] (%s/%s) — identical to failed primary",
                    i, fb_provider_type, fb_model,
                )
                continue

            if not fb_base_url or not fb_api_key:
                logger.warning(
                    "fallback_provider: skipping fallback[%d] (%s) — no base_url/api_key after resolve",
                    i, fb_provider_type,
                )
                continue

            try:
                from engine.model import wrap_http_with_sticky_session
                _fb_sticky_sid = (
                    getattr(rctx, "sticky_session_id", "")
                    or getattr(rctx, "parent_session_id", "")
                    or getattr(rctx, "session_id", "")
                )
                _fb_http = wrap_http_with_sticky_session(
                    rctx.llm_http,
                    fb_ch.options.get("sticky_session"),
                    _fb_sticky_sid,
                )
                fb_provider = _create_fallback_provider(
                    provider_type=fb_provider_type,
                    base_url=fb_base_url,
                    api_key=fb_api_key,
                    model=fb_model,
                    http=_fb_http,
                    provider_options=fb_options,
                )
                if fb_provider is not None:
                    fb_provider.channel_id = fb_ch.id
                if fb_provider is None:
                    logger.warning(
                        "fallback_provider: skipping fallback[%d] — unsupported provider type '%s'",
                        i, fb_provider_type,
                    )
                    continue

                logger.info(
                    "fallback_provider: trying fallback[%d] base_url=%s model=%s",
                    i, fb_base_url[:40], fb_model,
                )

                # Formatting, request hooks, IDs, cancellation and the image policy
                # (engine.vision, decided by the replacement model's capability) all
                # run inside the public call capability.
                images_removed = (
                    _messages_have_images(messages)
                    and not model_has_vision(workspace_root, fb_model)
                )
                attempt = {"provider": fb_provider_type, "channel": fb_ch.id, "model": fb_model, "index": i}
                t0 = time.monotonic()
                new_resp = await call_provider(fb_provider)
                if isinstance(new_resp, TaskAction):
                    return hook_result(action=new_resp)
                if new_resp is None:
                    return hook_result(skip_step=True)
                elapsed = round((time.monotonic() - t0) * 1000, 1)
                attempt.update(success=bool(new_resp.ok), status=getattr(new_resp, "status_code", None))
                attempts.append(attempt)
                await rctx.emit_event("llm_fallback", {
                    "node_id": ctx.node.id, "task_id": rctx.task_id,
                    "requested": requested, "original_status": status_code,
                    "attempt": attempt, "elapsed_ms": elapsed, "images_removed": images_removed,
                })

                if new_resp.ok:
                    logger.warning(
                        "fallback_provider: fallback[%d] succeeded in %.0fms "
                        "(base_url=%s model=%s)",
                        i, elapsed, fb_base_url[:40], fb_model,
                    )
                    new_resp.provider_meta["_fallback_provider"] = fb_provider_type
                    new_resp.provider_meta["_fallback_channel"] = fb_ch.id
                    new_resp.provider_meta["_fallback_model"] = fb_model
                    ctx.response = new_resp
                    ctx.provider = fb_provider  # This hook response only, not the configured provider.
                    text = f"原渠道 {original_model} 请求失败，本次改用 {fb_model}（{fb_provider_type}）。"
                    if images_removed:
                        text += "该模型不支持识图，图片已交由识图模型转写为文字。"
                    return hook_result(modified=True, channels={"response_notices": [{
                        "code": "fallback_provider.used", "label": "备用模型", "text": text,
                        "requested": requested, "original_status": status_code,
                        "attempts": attempts, "images_removed": images_removed,
                    }]})
                else:
                    _fb_err = getattr(new_resp, 'error', 'unknown')
                    _fb_st = getattr(new_resp, 'status_code', '?')
                    logger.warning(
                        "fallback_provider: fallback[%d] also failed "
                        "(status=%s error=%s)",
                        i, _fb_st, _fb_err,
                    )
                    # [fix 2026-05-28] Record this fallback's failure for later
                    # aggregation into the user-facing error message.
                    fallback_errors.append(
                        f"[Fallback {fb_provider_type} failed: {str(_fb_err)[:150]}]"
                    )

            except Exception as exc:
                await rctx.emit_event("llm_fallback", {
                    "node_id": ctx.node.id, "task_id": rctx.task_id, "requested": requested,
                    "original_status": status_code, "attempt": {
                        "provider": fb_provider_type, "model": fb_model, "index": i,
                        "success": False, "error_type": type(exc).__name__,
                    },
                })
                logger.error(
                    "fallback_provider: fallback[%d] exception: %s",
                    i, exc, exc_info=True,
                )
                # [fix 2026-05-28] Record exception-type failures too.
                fallback_errors.append(
                    f"[Fallback {fb_ch.id} exception: {str(exc)[:150]}]"
                )

        # [fix 2026-05-28] Why: when all fallbacks fail, the user only sees the
        # original provider's error and has no idea fallbacks were even attempted.
        # How: append each fallback's failure reason to ctx.response.error.
        # Purpose: give the user full visibility into what was tried.
        if fallback_errors and hasattr(ctx.response, "error"):
            fb_summary = " | ".join(fallback_errors)
            ctx.response.error = f"{original_error} | {fb_summary}"

        # All fallbacks failed, emit signal and let original error flow
        logger.error(
            "fallback_provider: all %d fallback(s) failed, "
            "original error stands (status=%s)",
            len(fallbacks_raw), status_code,
        )
        await rctx.emit_event("llm_fallback", {
            "node_id": ctx.node.id, "task_id": rctx.task_id, "requested": requested,
            "original_status": status_code, "success": False, "exhausted": True,
            "attempts": attempts,
        })

        return None
