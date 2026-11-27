from __future__ import annotations

"""Media understanding tool. Gemini-format vision channel: images + videos; OpenAI format: images only."""

SPEC = {
    "pure": True,
    "name": "read_media",
    "async_mode": False,
    "description": (
        "[For non-multimodal models or video analysis; do NOT use on images if you can see images natively.] "
        "Analyze one or more local image or video files using the configured vision model (videos need a Gemini-format vision channel). "
        "Returns a comprehensive description covering OCR text, layout, visual style, objects, people, actions, "
        "and (for videos) scene timeline, motion, dialogue, and audio. "
        "Videos >60s automatically upgrade to background async execution and deliver results via preempt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of local image or video file paths (relative to workspace root or absolute).",
            },
            "path": {
                "type": "string",
                "description": "Single local image or video file path (convenience shorthand; also accepts legacy image_path / video_path).",
            },
            "image_path": {"type": "string", "description": "Deprecated alias for path."},
            "video_path": {"type": "string", "description": "Deprecated alias for path."},
            "prompt": {
                "type": "string",
                "description": (
                    "Optional instruction or focus prompt (also accepts legacy 'focus'). "
                    "Examples: 'OCR all text verbatim', 'describe character outfit', 'transcribe dialogue with timestamps'. "
                    "Default: comprehensive analysis."
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional multimodal model to use. Default: the configured vision model",
            },
        },
        "required": [],
    },
}

TIMEOUT_SEC = 300.0

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
import uuid


IMAGE_MIMES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp", ".avif": "image/avif",
}
VIDEO_MIMES = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".m4v": "video/mp4", ".3gp": "video/3gpp",
}


def _prepare_media(raw_path: str, root: Path) -> tuple[dict, bool]:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / raw_path).resolve()
        if not path.exists():
            path = (root / raw_path).resolve()
    if not path.is_file():
        raise ValueError(f"Media file not found: {raw_path}")
    ext = path.suffix.lower()
    mime = VIDEO_MIMES.get(ext) or IMAGE_MIMES.get(ext)
    if not mime:
        raise ValueError(f"Unsupported media type: {ext}")
    is_video = ext in VIDEO_MIMES
    temp_path = None
    try:
        if is_video and path.stat().st_size > 20 * 1024 * 1024:
            temp_dir = root / "data" / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"read_media_{uuid.uuid4().hex}.mp4"
            compressed = subprocess.run(
                ["ffmpeg", "-y", "-i", str(path), "-vf", "scale='min(854,iw)':-2",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "64k", str(temp_path)],
                capture_output=True, text=True, timeout=120,
            )
            if compressed.returncode or not temp_path.is_file() or temp_path.stat().st_size > 25 * 1024 * 1024:
                raise ValueError(f"Video exceeds 20MB and ffmpeg compression failed: {raw_path}")
            path, mime = temp_path, "video/mp4"
        if path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError(f"Media file too large (>25MB): {raw_path}")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"inlineData": {"mimeType": mime, "data": data}}, is_video
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _vision_channel(root: Path):
    """Settings → 识图模型 (the vision channel); missing or off = the main channel."""
    code_root = str(Path(__file__).resolve().parent.parent)
    if code_root not in sys.path:
        sys.path.insert(0, code_root)
    import clonoth_channels as chn
    cfg = chn.load_config(root)
    try:
        return chn.slot_channel(cfg, "vision", root=root) or chn.get_channel(cfg, root=root)
    except chn.ChannelError as error:
        raise ValueError(f"识图模型未配置：{error}") from error


def read_media(args: dict, root: Path) -> dict:
    paths = args.get("paths") or args.get("image_paths") or []
    paths = list(paths)
    single = args.get("path") or args.get("image_path") or args.get("video_path")
    if single:
        paths.append(single)
    paths = list(dict.fromkeys(str(path).strip() for path in paths if str(path).strip()))
    if not paths:
        raise ValueError("Provide path, image_path, video_path, or paths")

    ch = _vision_channel(root)
    if not ch.api_key:
        raise ValueError(f"渠道「{ch.name}」没有 API Key")
    model = str(args.get("model") or ch.model).strip()
    if not model:
        raise ValueError(f"渠道「{ch.name}」没有模型")

    with ThreadPoolExecutor(max_workers=min(len(paths), 8)) as pool:
        prepared = list(pool.map(lambda path: _prepare_media(path, root), paths))
    video_only = all(is_video for _, is_video in prepared)
    instruction = (
        "Analyze this video in comprehensive detail (scene timeline with timestamps, visible text, "
        "people/actions, audio/dialogue, camera motion, and overall summary)." if video_only else
        "Describe the provided media in comprehensive detail (verbatim OCR text, layout/composition, "
        "visual style, objects/people/actions, and overall context)."
    )
    focus = str(args.get("prompt") or args.get("focus") or "").strip()
    if focus:
        instruction += f"\n\nAdditionally, focus specifically on: {focus}"
    if ch.type.startswith("gemini"):
        description, tokens = _ask_gemini(ch, model, [part for part, _ in prepared], instruction)
    else:
        if any(is_video for _, is_video in prepared):
            raise ValueError(f"识别视频需要 Gemini 格式的识图渠道，当前「{ch.name}」是 {ch.type} 格式（只能识别图片）")
        description, tokens = _ask_openai(ch, model, [part for part, _ in prepared], instruction)
    if not description:
        raise ValueError("Empty response from model")
    return {"ok": True, "data": {
        "result": description, "description": description, "model": model, "files": paths, "tokens_used": tokens,
    }}


def _post_json(url: str, body: dict, headers: dict[str, str]) -> dict:
    request = urllib_request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib_request.urlopen(request, timeout=240) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        raise ValueError(f"API HTTP {error.code}: {detail}") from error


def _ask_gemini(ch, model: str, parts: list[dict], instruction: str) -> tuple[str, int | None]:
    base_url = re.sub(r"/v1(beta)?$", "", (ch.base_url or "https://generativelanguage.googleapis.com").rstrip("/"))
    # Explicit role: the official API tolerates its absence, but gateways often require user/model.
    body = {"contents": [{"role": "user", "parts": [*parts, {"text": instruction}]}],
            "generationConfig": {"temperature": 0.15, "maxOutputTokens": 8192}}
    result = _post_json(f"{base_url}/v1beta/models/{model}:generateContent?key={ch.api_key}", body, {})
    candidates = result.get("candidates") or []
    if not candidates:
        raise ValueError(f"No candidates in response: {json.dumps(result, ensure_ascii=False)[:500]}")
    response_parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in response_parts if not part.get("thought")).strip()
    return text, (result.get("usageMetadata") or {}).get("totalTokenCount")


def _ask_openai(ch, model: str, parts: list[dict], instruction: str) -> tuple[str, int | None]:
    """OpenAI Chat Completions: images as data-URL image_url parts."""
    base_url = (ch.base_url or "https://api.openai.com/v1").rstrip("/")
    content = [{"type": "image_url", "image_url": {"url": f"data:{p['inlineData']['mimeType']};base64,{p['inlineData']['data']}"}}
               for p in parts]
    content.append({"type": "text", "text": instruction})
    body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.15, "max_tokens": 8192}
    result = _post_json(f"{base_url}/chat/completions", body, {"Authorization": f"Bearer {ch.api_key}"})
    choices = result.get("choices") or []
    if not choices:
        raise ValueError(f"No choices in response: {json.dumps(result, ensure_ascii=False)[:500]}")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    if isinstance(text, list):
        text = "".join(str(part.get("text") or "") for part in text if isinstance(part, dict))
    return str(text).strip(), (result.get("usage") or {}).get("total_tokens")

if __name__ == "__main__":
    try:
        raw_input = json.loads(sys.stdin.read())
        args = raw_input.get("args", raw_input)
        root = Path(os.environ.get("CLONOTH_WORKSPACE") or Path(__file__).resolve().parent.parent)
        output = read_media(args, root)
    except (OSError, ValueError, URLError, subprocess.SubprocessError) as error:
        output = {"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}
    print(json.dumps(output, ensure_ascii=False))
