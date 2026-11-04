"""Model image capability and image hand-off for models that cannot see.

Single place that decides whether a request may carry image blocks and, when it
may not, replaces them with descriptions produced by the configured vision model.

Configuration (data/config.yaml):

    model_capabilities:          # keyed by model name; unlisted models can see images
      deepseek-flash: {vision: false}
    system_models:
      vision: {model: ..., base_url: ..., api_key: ...}   # used by read_media

Decision for one provider request:
  * primary call: node ``strip_images`` (alias ``no_vision``) when declared, otherwise
    the vision capability of the model being called;
  * replacement call (fallback chain): the vision capability of the replacement model.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict

_logger = logging.getLogger(__name__)

_FILE_SCHEME = "file://"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
# Only images in the most recent user turns get new descriptions; older images use
# a cached description when one exists, otherwise only their path annotation stays.
_DESCRIBE_RECENT_USER_TURNS = 2
_DESCRIBE_MAX_IMAGES = 6
_DESCRIBE_TIMEOUT_SEC = 180.0

_caps_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_read_media_fn: Any = None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return None


def load_model_capabilities(workspace_root: Path) -> dict[str, Any]:
    path = Path(workspace_root) / "data" / "config.yaml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = str(path)
    cached = _caps_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    raw = (load_yaml_dict(path) or {}).get("model_capabilities")
    caps = raw if isinstance(raw, dict) else {}
    _caps_cache[key] = (mtime, caps)
    return caps


def vision_from_capabilities(caps: Any, model: str) -> bool:
    """Vision flag of ``model`` in a ``model_capabilities`` mapping; unlisted models can see."""
    entry = caps.get((model or "").strip()) if isinstance(caps, dict) else None
    if isinstance(entry, dict):
        flag = _as_bool(entry.get("vision"))
        if flag is not None:
            return flag
    return True


def model_has_vision(workspace_root: Path, model: str) -> bool:
    return vision_from_capabilities(load_model_capabilities(workspace_root), model)


def node_strip_images_setting(node: Any) -> bool | None:
    """Node YAML ``strip_images`` / ``no_vision`` (in ``Node.extra``); None when undeclared."""
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict):
        return None
    for key in ("strip_images", "no_vision"):
        if key in extra:
            flag = _as_bool(extra.get(key))
            if flag is not None:
                return flag
    return None


def images_allowed(node: Any, model: str, workspace_root: Path, *, replacement_call: bool = False) -> bool:
    if not replacement_call:
        declared = node_strip_images_setting(node)
        if declared is not None:
            return not declared
    return model_has_vision(workspace_root, model)


# ---------------------------------------------------------------------------
#  Image hand-off to the vision model
# ---------------------------------------------------------------------------

def _image_paths(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
            url = str(part["image_url"].get("url", ""))
            if url.startswith(_FILE_SCHEME):
                out.append(url[len(_FILE_SCHEME):])
    return out


def _cache_file(workspace_root: Path, image: Path) -> Path | None:
    try:
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
    except OSError:
        return None
    return Path(workspace_root) / "data" / "cache" / "vision" / f"{digest}.txt"


def _read_cached(workspace_root: Path, rel_path: str) -> str:
    cache = _cache_file(workspace_root, Path(workspace_root) / rel_path)
    if cache and cache.is_file():
        try:
            return cache.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _load_read_media():
    global _read_media_fn
    if _read_media_fn is None:
        tool = Path(__file__).resolve().parent.parent / "tools" / "read_media.py"
        spec = importlib.util.spec_from_file_location("clonoth_tool_read_media", tool)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _read_media_fn = module.read_media
    return _read_media_fn


def _describe_sync(workspace_root: Path, rel_path: str) -> str:
    result = _load_read_media()({"path": rel_path}, Path(workspace_root))
    desc = str(((result or {}).get("data") or {}).get("description") or "").strip()
    if not desc:
        raise ValueError("empty description")
    cache = _cache_file(workspace_root, Path(workspace_root) / rel_path)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(desc, encoding="utf-8")
    return desc


async def _describe(workspace_root: Path, rel_path: str) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_describe_sync, workspace_root, rel_path), _DESCRIBE_TIMEOUT_SEC,
        )
    except Exception as exc:  # reported to the model as a failed hand-off
        _logger.warning("vision hand-off failed for %s: %s", rel_path, exc)
        return f"__error__:{exc}"


def _describe_text(rel_path: str, desc: str) -> str:
    if desc.startswith("__error__:"):
        reason = desc[len("__error__:"):][:200]
        return f"[图片 {rel_path}：识图模型识别失败（{reason}），可调用 read_media 重试]"
    return f"[图片 {rel_path} 的识图结果（由识图模型生成）]\n{desc}"


async def strip_and_describe_images(messages: list[dict[str, Any]], workspace_root: Path) -> list[dict[str, Any]]:
    """Replace image blocks with vision-model descriptions for a model that cannot see.

    Operates on provider-formatted messages whose images are still ``file://`` refs.
    Recent images are described (and cached); older images reuse cached descriptions.
    Every image keeps its ``[Attached images: path]`` annotation so the model can still
    call read_media for a focused look.
    """
    recent: list[str] = []
    turns = 0
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        paths = _image_paths(msg.get("content"))
        if paths:
            recent.extend(p for p in paths if p not in recent)
            turns += 1
            if turns >= _DESCRIBE_RECENT_USER_TURNS:
                break

    descriptions: dict[str, str] = {}
    for rel in {p for m in messages for p in _image_paths(m.get("content"))}:
        cached = _read_cached(workspace_root, rel)
        if cached:
            descriptions[rel] = cached
    pending = [p for p in recent if p not in descriptions][:_DESCRIBE_MAX_IMAGES]
    if pending:
        results = await asyncio.gather(*(_describe(workspace_root, p) for p in pending))
        descriptions.update(zip(pending, results))

    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list) or not any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        ):
            out.append(msg)
            continue
        new_content: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = str((part.get("image_url") or {}).get("url", ""))
                rel = url[len(_FILE_SCHEME):] if url.startswith(_FILE_SCHEME) else ""
                if rel and rel in descriptions:
                    new_content.append({"type": "text", "text": _describe_text(rel, descriptions[rel])})
                continue
            new_content.append(part)
        if not new_content:
            new_msg = {**msg, "content": "[image content removed]"}
        elif len(new_content) == 1 and new_content[0].get("type") == "text":
            new_msg = {**msg, "content": new_content[0].get("text", "")}
        else:
            new_msg = {**msg, "content": new_content}
        out.append(new_msg)
    return out
