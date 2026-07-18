from __future__ import annotations

"""
GPT Image 2 生图工具 (Clonoth external tool)

运行方式：通过 stdin 接收 JSON 参数，stdout 输出 JSON 结果

API 渠道配置（与 read_image / system_models 一致）：
  优先读 data/config.yaml 的 system_models.image_gpt（model/base_url/api_key），
  其次 slot 专属环境变量 CLONOTH_IMAGE_GPT_*，留空则回退主渠道
  （OPENAI_BASE_URL / OPENAI_API_KEY，model 默认 gpt-image-2）。
"""

SPEC = {
    'name': 'gpt_image_2',
    'description': '使用 gpt-image-2 模型生成图片。支持自定义分辨率（宽高需被16整除）、quality 参数。',
    'async_mode': True,
    'input_schema': {
        'type': 'object',
        'required': ['prompt'],
        'properties': {
            'prompt': {
                'type': 'string',
                'description': '图片描述文本（支持中英文）'
            },
            'size': {
                'type': 'string',
                'default': '1024x1024',
                'description': '分辨率，格式 WxH（宽高均需被16整除）。常用值：1024x1536（竖版）、1920x1088（横版）'
            },
            'quality': {
                'type': 'string',
                'default': 'low',
                'description': '生成质量：low, medium, high, auto（注意：部分网关对 high/auto 有约90秒超时，推荐用 low）'
            },
            'filename': {
                'type': 'string',
                'description': '输出文件名（不含路径），默认自动生成'
            },
            'image_paths': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '可选，本地图片路径列表，作为参考垫图传入模型'
            }
        }
    }
}

TIMEOUT_SEC = 540.0


if __name__ == "__main__":
    import json, sys
    # Fix Windows GBK stdin encoding - must read as UTF-8
    _input = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import base64, json, urllib.request, os, time, hashlib
    from pathlib import Path

    prompt = args.get('prompt', '')
    size = args.get('size', '1024x1024')
    quality = args.get('quality', 'auto')
    filename = args.get('filename', '')
    raw_image_paths = args.get('image_paths') or []
    if isinstance(raw_image_paths, str):
        raw_image_paths = [raw_image_paths]

    if not prompt:
        fail('prompt is required')

    # === 分辨率校验 ===
    try:
        w, h = size.split('x')
        w, h = int(w), int(h)
        if w % 16 != 0 or h % 16 != 0:
            fail(f'Width ({w}) and height ({h}) must both be divisible by 16')
    except ValueError:
        fail(f'Invalid size format: {size}. Use WxH, e.g. 1024x1536')

    # ============================================================
    # API 渠道配置解析
    # 优先级：config.yaml system_models.image_gpt > 环境变量 CLONOTH_IMAGE_GPT_*
    #        > 主渠道 OPENAI_BASE_URL / OPENAI_API_KEY（model 默认 gpt-image-2）
    # 本工具是独立子进程，无法导入 clonoth_runtime，故本地实现一份轻量解析
    # （与 clonoth_runtime.resolve_system_model / read_image 策略一致）。
    # ============================================================
    def _load_dotenv():
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
        blk = sm.get("image_gpt") if isinstance(sm, dict) else None
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
        v = _env(f"CLONOTH_IMAGE_GPT_{env_suffix}")
        if v:
            return v
        for fb in fallbacks:
            if fb and fb.strip():
                return fb.strip()
        return ""

    # api_key: image_gpt 专属 > OPENAI_API_KEY（主渠道）
    api_key = _pick("api_key", "API_KEY", _env("OPENAI_API_KEY"))
    # base_url: image_gpt 专属 > OPENAI_BASE_URL（主渠道）
    base_url = _pick("base_url", "BASE_URL", _env("OPENAI_BASE_URL")).rstrip("/")
    # model: image_gpt 专属 > CLONOTH_IMAGE_GPT_MODEL > 默认 gpt-image-2
    model_name = _pick("model", "MODEL", "gpt-image-2")

    if not api_key:
        fail('No API key found in config.yaml / env / .env file')
    if not base_url:
        fail('No base_url configured (system_models.image_gpt / OPENAI_BASE_URL)')

    # 拼接 chat/completions 端点（base_url 若已含完整路径则直接使用）
    if base_url.endswith("/chat/completions"):
        url = base_url
    else:
        url = base_url.rstrip("/") + "/chat/completions"

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }

    # === 构建消息体（支持垫图） ===
    content_parts = []
    for img_path in raw_image_paths:
        img_path = str(img_path).strip()
        if not img_path:
            continue
        img_file = Path.cwd() / img_path
        if not img_file.exists():
            fail(f'Input image not found: {img_path}')
        mime = 'image/png'
        ext = img_file.suffix.lower()
        if ext in ['.jpg', '.jpeg']:
            mime = 'image/jpeg'
        elif ext == '.webp':
            mime = 'image/webp'
        elif ext == '.gif':
            mime = 'image/gif'
        b64 = base64.b64encode(img_file.read_bytes()).decode('utf-8')
        content_parts.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
    content_parts.append({'type': 'text', 'text': prompt})

    # 没有垫图时用纯文本，有垫图时用多模态数组
    msg_content = content_parts if len(content_parts) > 1 else prompt

    payload = {
        'model': model_name,
        'messages': [{'role': 'user', 'content': msg_content}],
        'size': size,
        'quality': quality
    }

    payload_json = json.dumps(payload)
    req = urllib.request.Request(url, data=payload_json.encode('utf-8'), headers=headers, method='POST')

    try:
        resp = urllib.request.urlopen(req, timeout=480)
        res_data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        err_body = ''
        if hasattr(e, 'read'):
            try:
                err_body = e.read().decode()[:500]
            except:
                pass
        fail(f'API request failed: {e} {err_body}')

    # === 从响应中提取图片数据 ===
    msg = res_data.get('choices', [{}])[0].get('message', {})
    images = msg.get('images', [])

    img_data = None
    if images:
        # 方式1: message.images 数组（部分反代返回这种格式）
        raw = images[0]
        img_data = base64.b64decode(raw.split(',')[-1] if ',' in raw else raw)
    else:
        # 方式2: 从 content 中提取 base64 data URI
        content = msg.get('content', '')
        if content:
            import re
            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', str(content))
            if m:
                img_data = base64.b64decode(m.group(1))

    if not img_data:
        fail('No image data in response: ' + json.dumps(res_data)[:500])

    # === 保存图片 ===
    if not filename:
        ts = str(time.time()).encode()
        h = hashlib.md5(ts + prompt[:50].encode()).hexdigest()[:10]
        filename = f'gpt_image_{h}.png'

    out_dir = os.path.join('data', 'attachments')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, 'wb') as f:
        f.write(img_data)

    # 尝试读取实际分辨率
    try:
        from PIL import Image
        img = Image.open(out_path)
        actual_size = f'{img.size[0]}x{img.size[1]}'
    except:
        actual_size = size

    output({
        'ok': True,
        'data': {
            'result': f'Image generated: {out_path}',
            'path': out_path,
            'attachments': [out_path],
            'size': actual_size,
            'quality': quality,
            'bytes': len(img_data)
        },
        'attachments': [out_path]
    })
