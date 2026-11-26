from __future__ import annotations

"""Gemini native image generation tool (Nano Banana).

Uses the Gemini generativeLanguage REST API with responseModalities=["TEXT","IMAGE"]
to generate images. The generated image is saved under data/attachments/ and returned
as an attachment path compatible with Clonoth's multimodal pipeline.

发图机制（参考 NovelAI 插件）：
  - async_mode=False：同步执行，出图后直接返回 attachments，引擎据此发图，
    finish_guard 也能正确记录“工具成功”。
  - 图片发送统一交给 supervisor 的 dispatch_attachment 路由（子节点任务完成时
    自动发送），工具本身不再 POST intermediate_reply，避免双发同一张图。

API 渠道：data/config.yaml system_models.image 引用的渠道方案（设置 → 系统模型）。
"""

SPEC = {
    "name": "gemini_image",
    "async_mode": False,
    "description": (
        "Generate an image using Gemini (Nano Banana). "
        "Provide a text prompt describing the desired image. "
        "Optionally specify aspect_ratio (1:1, 3:4, 4:3, 9:16, 16:9) and "
        "model (gemini-3-pro-image-preview, gemini-2.5-flash-image, gemini-3.1-flash-image-preview). "
        "The generated image is sent to the user automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the image to generate.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Aspect ratio: 1:1, 3:4, 4:3, 9:16, 16:9. Default: 1:1",
                "enum": ["1:1", "3:4", "4:3", "9:16", "16:9"],
            },
            "model": {
                "type": "string",
                "description": "Gemini image model to use. Default: gemini-3-pro-image-preview",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of paths to input images (relative to workspace root) to use as references.",
            },
        },
        "required": ["prompt"],
    },
}

# [2026-07-19] 超时/重试对齐：单次请求 120s，重试 2 次，退避 5s；
# 内部最坏耗时 ≈ 2×120+5 = 245s < 工具总超时 300s，两个超时对齐。
TIMEOUT_SEC = 300

# API 请求重试参数（参考 NovelAI：429/5xx/超时/网络错误自动重试）
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SEC = (5.0,)
_REQUEST_TIMEOUT = 120

if __name__ == "__main__":
    import json
    import sys
    import os
    import time
    import base64
    import uuid
    from pathlib import Path
    from urllib import request as urllib_request
    from urllib.error import HTTPError, URLError

    _input = json.loads(sys.stdin.read())

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        # [AutoC 2026-05-31] Why: image generation errors must be visible through
        # data.result as well as error. How: emit the unified failure wrapper before
        # exiting non-zero. Purpose: let the registry preserve detailed API errors.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    def _slot_channel(slot):
        """Settings → 系统模型 data/config.yaml system_models.<slot>; None when unset."""
        import os as _os
        from pathlib import Path as _Path
        code_root = _Path(__file__).resolve().parent.parent
        root = _Path(_os.environ.get("CLONOTH_WORKSPACE") or code_root)
        if str(code_root) not in sys.path:
            sys.path.insert(0, str(code_root))
        import clonoth_channels as chn
        try:
            return chn.slot_channel(chn.load_config(root), slot, root=root)
        except chn.ChannelError as error:
            fail(f"生图渠道配置错误：{error}")

    # [2026-07-19] 方案 X：工具不再自己推图，发图统一交给 supervisor 的
    # dispatch_attachment 路由（和 NovelAI 一致），避免与它双发同一张图。

    args = _input

    # ---- 参数 ----
    prompt_text = str(args.get("prompt") or "").strip()
    if not prompt_text:
        fail("prompt is required")

    aspect_ratio = str(args.get("aspect_ratio") or "1:1").strip()
    if aspect_ratio not in {"1:1", "3:4", "4:3", "9:16", "16:9"}:
        aspect_ratio = "1:1"

    raw_image_paths = args.get("image_paths") or args.get("image_path") or []
    if isinstance(raw_image_paths, str):
        image_paths = [raw_image_paths]
    elif isinstance(raw_image_paths, list):
        image_paths = raw_image_paths
    else:
        image_paths = []

    ch = _slot_channel("image")
    if ch is None:
        fail("Gemini 生图未配置：在 设置 → 系统模型 → 生图(Gemini) 选择渠道方案")
    model = str(args.get("model") or ch.model or "gemini-3-pro-image-preview").strip()
    api_key = ch.api_key
    base_url = ch.base_url.rstrip("/")
    if not api_key:
        fail(f"渠道方案「{ch.name}」没有 API Key")

    # ---- 构建请求 ----
    for suffix in ("/v1beta", "/v1"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break
    if not base_url:
        base_url = "https://generativelanguage.googleapis.com"
    url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    parts = []
    for img_path in image_paths:
        img_path = str(img_path).strip()
        if not img_path:
            continue

        img_file = Path.cwd() / img_path
        if not img_file.exists():
            fail(f"Input image not found: {img_path}")

        mime_type = "image/png"
        ext = img_file.suffix.lower()
        if ext in [".jpg", ".jpeg"]:
            mime_type = "image/jpeg"
        elif ext == ".webp":
            mime_type = "image/webp"
        elif ext == ".gif":
            mime_type = "image/gif"

        try:
            b64_data = base64.b64encode(img_file.read_bytes()).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": mime_type,
                    "data": b64_data
                }
            })
        except Exception as e:
            fail(f"Failed to read input image {img_path}: {e}")

    parts.append({"text": prompt_text})

    body = {
        "contents": [
            {
                "role": "user",
                "parts": parts
            }
        ],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
            },
        },
    }

    req_data = json.dumps(body).encode("utf-8")

    # ---- 发送请求（带自动重试） ----
    def _retryable_http(code: int) -> bool:
        return code in (408, 409, 425, 429, 500, 502, 503, 504)

    resp_data = None
    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        req = urllib_request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                pass
            last_error = f"Gemini API HTTP {e.code}: {error_body}"
            retryable = _retryable_http(e.code)
        except URLError as e:
            last_error = f"Gemini API connection error: {e.reason}"
            retryable = True
        except Exception as e:
            last_error = f"Gemini API request failed: {e}"
            retryable = True
        else:
            retryable = False
        if resp_data is not None:
            break
        if attempt < _MAX_ATTEMPTS and retryable:
            time.sleep(_RETRY_BACKOFF_SEC[min(attempt - 1, len(_RETRY_BACKOFF_SEC) - 1)])
            continue
        fail(last_error)

    # ---- 解析响应 ----
    candidates = resp_data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        fail(f"No candidates in response: {json.dumps(resp_data)[:500]}")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list):
        fail("No parts in response")

    text_parts = []
    image_saved = []

    # 确定保存目录
    # 工具运行时 cwd 是 workspace_root
    workspace_root = Path.cwd()
    attachments_dir = workspace_root / "data" / "attachments" / "gemini_image"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    for part in parts:
        if not isinstance(part, dict):
            continue

        # 文本部分
        if "text" in part and isinstance(part["text"], str):
            text_parts.append(part["text"])

        # 图片部分
        inline_data = part.get("inlineData")
        if isinstance(inline_data, dict):
            b64_data = inline_data.get("data", "")
            mime_type = str(inline_data.get("mimeType") or "image/png")

            if not b64_data:
                continue

            # 确定扩展名
            ext = ".png"
            if "jpeg" in mime_type or "jpg" in mime_type:
                ext = ".jpg"
            elif "webp" in mime_type:
                ext = ".webp"
            elif "gif" in mime_type:
                ext = ".gif"

            filename = f"{uuid.uuid4().hex}{ext}"
            file_path = attachments_dir / filename

            try:
                img_bytes = base64.b64decode(b64_data)
                file_path.write_bytes(img_bytes)
            except Exception as e:
                fail(f"Failed to decode/save image: {e}")

            rel_path = file_path.relative_to(workspace_root).as_posix()
            image_saved.append({
                "type": "image",
                "path": rel_path,
                "mime_type": mime_type,
                "name": filename,
            })

    if not image_saved:
        fail(f"Gemini did not return any image. Text response: {' '.join(text_parts)[:500]}")

    text = "\n".join(text_parts).strip()

    # === 只返回 attachments，发图交给 supervisor 的 dispatch_attachment 统一处理 ===
    # [AutoC 2026-05-31] Why: generated image tools now expose their primary text
    # and attachment metadata under data, but legacy attachment collection still
    # reads the top-level field. How: store text, attachments, and image metadata in
    # data and mirror attachments at the top level. Purpose: migrate schema without
    # breaking final image delivery.
    output({
        "ok": True,
        "data": {
            "result": (
                "Image generated and will be sent to the user automatically. "
                "Do NOT resend it via reply/finish."
            ),
            "text": text,
            "attachments": image_saved,
            "image_path": image_saved[0]["path"] if image_saved else "",
            "image_count": len(image_saved),
        },
        "attachments": image_saved,
    })
