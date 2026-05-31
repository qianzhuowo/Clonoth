from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '使用 gpt-image-2 模型生成图片。支持自定义分辨率（宽高需被16整除）、quality '
                '参数。异步执行，适合耗时较长的高分辨率生图。返回生成图片的本地路径。',
 'input_schema': {'properties': {'filename': {'description': '输出文件名（不含路径），默认自动生成',
                                              'type': 'string'},
                                 'prompt': {'description': '图片描述文本（支持中英文）', 'type': 'string'},
                                 'quality': {'default': 'standard',
                                             'description': '生成质量：low, medium, high, auto',
                                             'type': 'string'},
                                 'size': {'default': '1024x1024',
                                          'description': '分辨率，格式 WxH（宽高均需被16整除）。默认 '
                                                         '1024x1024。常用值：1024x1536（竖版）、1920x1088（横版）、3840x2160（4K横）、2160x3840（4K竖）',
                                          'type': 'string'},
                                 'image_paths': {'type': 'array',
                                                 'items': {'type': 'string'},
                                                 'description': '可选，本地图片路径列表（相对于工作区根目录），作为参考垫图传入模型'}},
                  'required': ['prompt'],
                  'type': 'object'},
 'name': 'gpt_image_2',
 'async_mode': True}

TIMEOUT_SEC = 540.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        # [AutoC 2026-05-31] Why: image API failures should still provide readable
        # history via data.result. How: emit the unified ok=false wrapper before
        # exiting with an error status. Purpose: preserve detailed failures through
        # the external-tool registry.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import base64, json, urllib.request, os, time, hashlib
    
    prompt = args.get('prompt', '')
    size = args.get('size', '1024x1024')
    quality = args.get('quality', 'auto')
    filename = args.get('filename', '')
    raw_image_paths = args.get('image_paths') or []
    if isinstance(raw_image_paths, str):
        raw_image_paths = [raw_image_paths]
    
    if not prompt:
        fail('prompt is required')
    
    # Validate size divisible by 16
    try:
        w, h = size.split('x')
        w, h = int(w), int(h)
        if w % 16 != 0 or h % 16 != 0:
            fail(f'Width ({w}) and height ({h}) must both be divisible by 16')
    except ValueError:
        fail(f'Invalid size format: {size}. Use WxH, e.g. 1024x1536')
    
    url = 'https://zoaholic.zhenxia.top/v1/chat/completions'
    api_key = os.environ.get('ZOAHOLIC_API_KEY', 'sk-KJr51AvQsCfphE11zPtZInappmQS64ZYecrk1uKnlncy7Qjv')
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    # Build message content with optional image references
    content_parts = []
    from pathlib import Path
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
    
    # Use simple string content if no images, multimodal array otherwise
    msg_content = content_parts if len(content_parts) > 1 else prompt
    
    payload = {
        'model': 'gpt-image-2',
        'messages': [{'role': 'user', 'content': msg_content}],
        'size': size,
        'quality': quality
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
    
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
    
    msg = res_data.get('choices', [{}])[0].get('message', {})
    images = msg.get('images', [])
    
    img_data = None
    if images:
        raw = images[0]
        img_data = base64.b64decode(raw.split(',')[-1] if ',' in raw else raw)
    else:
        content = msg.get('content', '')
        if content:
            import re
            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', str(content))
            if m:
                img_data = base64.b64decode(m.group(1))
    
    if not img_data:
        fail('No image data in response: ' + json.dumps(res_data)[:500])
    
    if not filename:
        ts = str(time.time()).encode()
        h = hashlib.md5(ts + prompt[:50].encode()).hexdigest()[:10]
        filename = f'gpt_image_{h}.png'
    
    out_path = os.path.join('data/attachments', filename)
    with open(out_path, 'wb') as f:
        f.write(img_data)
    
    try:
        from PIL import Image
        img = Image.open(out_path)
        actual_size = f'{img.size[0]}x{img.size[1]}'
    except:
        actual_size = size
    
    # [AutoC 2026-05-31] Why: generated image paths must be exposed both as
    # structured data and as attachments. How: place path and metadata under data,
    # use data.result for the readable message, and mirror attachments at top level
    # for migration compatibility. Purpose: keep image delivery and history display
    # working under the unified ok/data/error schema.
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
