from __future__ import annotations

"""Gemini native image generation tool (Nano Banana).

Uses the Gemini generativeLanguage REST API with responseModalities=["TEXT","IMAGE"]
to generate images. The generated image is saved under data/attachments/ and returned
as an attachment path compatible with Clonoth's multimodal pipeline.

API 渠道配置（与 read_image / system_models 一致）：
  优先读 data/config.yaml 的 system_models.image_gemini（model/base_url/api_key），
  其次 slot 专属环境变量 CLONOTH_IMAGE_GEMINI_*，留空则回退主渠道
  （api_key: GEMINI_API_KEY > OPENAI_API_KEY；base_url: OPENAI_BASE_URL；
   model 默认 gemini-3-pro-image-preview）。
"""

SPEC = {
    "name": "gemini_image",
    "async_mode": True,
    "description": (
        "Generate an image using Gemini (Nano Banana). "
        "Provide a text prompt describing the desired image. "
        "Optionally specify aspect_ratio (1:1, 3:4, 4:3, 9:16, 16:9) and "
        "model (gemini-3-pro-image-preview, gemini-2.5-flash-image, gemini-3.1-flash-image-preview). "
        "Returns the generated image path under data/attachments/."
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

TIMEOUT_SEC = 240

if __name__ == "__main__":
    import json
    import sys
    import os
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

    # ============================================================
    # API 渠道配置解析
    # 优先级：config.yaml system_models.image_gemini
    #        > 环境变量 CLONOTH_IMAGE_GEMINI_* > 主渠道回退
    # 本工具是独立子进程，无法导入 clonoth_runtime，故本地实现一份轻量解析
    # （与 clonoth_runtime.resolve_system_model / read_image 策略一致）。
    # ============================================================
    def _load_dotenv():
        """Fallback: read .env file if env vars missing."""
        env_path = Path.cwd() / ".env"
        kv = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip("'\"")
        return kv

    dotenv = _load_dotenv()

    def _env(key: str) -> str:
        return (os.environ.get(key, "") or dotenv.get(key, "")).strip()

    def _load_config_slot():
        try:
            import yaml  # type: ignore
        except Exception:
            return {}
        cfg_path = Path.cwd() / "data" / "config.yaml"
        if not cfg_path.exists():
            return {}
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        sm = data.get("system_models") if isinstance(data, dict) else None
        blk = sm.get("image_gemini") if isinstance(sm, dict) else None
        return blk if isinstance(blk, dict) else {}

    def _resolve_ref(v: str) -> str:
        s = (v or "").strip()
        if s.startswith("${") and s.endswith("}") and len(s) > 3:
            return _env(s[2:-1].strip())
        if s.startswith("$ENV{") and s.endswith("}") and len(s) > 6:
            return _env(s[5:-1].strip())
        return s

    _slot_cfg = _load_config_slot()

    def _pick(cfg_field: str, env_suffix: str, *fallbacks: str) -> str:
        v = _resolve_ref(str(_slot_cfg.get(cfg_field) or ""))
        if v:
            return v
        v = _env(f"CLONOTH_IMAGE_GEMINI_{env_suffix}")
        if v:
            return v
        for fb in fallbacks:
            if fb and fb.strip():
                return fb.strip()
        return ""

    # model: 优先 args > slot 配置 > CLONOTH_IMAGE_GEMINI_MODEL > 默认
    model = str(args.get("model") or "").strip()
    if not model:
        model = _pick("model", "MODEL", "gemini-3-pro-image-preview")

    # api_key: image_gemini 专属 > GEMINI_API_KEY > OPENAI_API_KEY
    api_key = _pick("api_key", "API_KEY", _env("GEMINI_API_KEY"), _env("OPENAI_API_KEY"))
    # base_url: image_gemini 专属 > GEMINI_BASE_URL > OPENAI_BASE_URL
    base_url = _pick("base_url", "BASE_URL", _env("GEMINI_BASE_URL"), _env("OPENAI_BASE_URL")).rstrip("/")

    if not api_key:
        fail("No API key found in config.yaml / env / .env file")

    # ---- 构建请求 ----
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
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
    req = urllib_request.Request(
        url,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # ---- 发送请求 ----
    try:
        with urllib_request.urlopen(req, timeout=180) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        fail(f"Gemini API HTTP {e.code}: {error_body}")
    except URLError as e:
        fail(f"Gemini API connection error: {e.reason}")
    except Exception as e:
        fail(f"Gemini API request failed: {e}")

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
    # [AutoC 2026-05-31] Why: generated image tools now expose their primary text
    # and attachment metadata under data, but legacy attachment collection still
    # reads the top-level field. How: store text, attachments, and image metadata in
    # data and mirror attachments at the top level. Purpose: migrate schema without
    # breaking final image delivery.
    output({
        "ok": True,
        "data": {
            "result": text,
            "text": text,
            "attachments": image_saved,
            "image_path": image_saved[0]["path"] if image_saved else "",
            "image_count": len(image_saved),
        },
        "attachments": image_saved,
    })
