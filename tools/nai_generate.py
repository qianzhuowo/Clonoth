from __future__ import annotations
import shutil

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '使用 NovelAI (NAI) API 生成 anime 图片。支持 nai-diffusion-3/4/4.5 模型，返回生成图片的本地路径。',
 'input_schema': {'properties': {'filename': {'description': '输出文件名（不含路径），默认自动生成',
                                              'type': 'string'},
                                 'height': {'description': '图片高度，默认 1216', 'type': 'integer'},
                                 'model': {'description': '模型选择。可选：nai-diffusion-4-5-full(默认,v4.5完整版), nai-diffusion-4-5-curated(v4.5策展版,SFW), nai-diffusion-4-full(v4完整版), nai-diffusion-4-curated(v4策展版), nai-diffusion-3(v3旧版)',
                                           'type': 'string'},
                                 'negative_prompt': {'description': "负面提示词，默认 'lowres, bad "
                                                                    'anatomy, bad hands, text, '
                                                                    'error, missing fingers, extra '
                                                                    'digit, fewer digits, cropped, '
                                                                    "worst quality, low quality'",
                                                     'type': 'string'},
                                 'prompt': {'description': "正面提示词（Danbooru tag 或自然语言均可，v4+支持自然语言），如 '1girl, white "
                                                           'hair, blue eyes, masterpiece, best '
                                                           "quality'",
                                            'type': 'string'},
                                 'sampler': {'description': '采样器，v3默认k_euler，v4/v4.5默认k_euler_ancestral。可选：k_euler, '
                                                            'k_euler_ancestral, k_dpmpp_2s_ancestral, k_dpmpp_2m_sde, '
                                                            'k_dpmpp_2m, k_dpmpp_sde',
                                             'type': 'string'},
                                 'noise_schedule': {'description': '噪声调度，默认karras。可选：karras, exponential, polyexponential（仅v4+有效）',
                                                    'type': 'string'},
                                 'scale': {'description': 'CFG scale，默认 5.0', 'type': 'number'},
                                 'seed': {'description': '随机种子，不填则随机', 'type': 'integer'},
                                 'steps': {'description': '采样步数，v3默认28，v4/v4.5默认23', 'type': 'integer'},
                                 'width': {'description': '图片宽度，默认 832。常用：832x1216(竖版), '
                                                          '1216x832(横版), 1024x1024(方形)',
                                           'type': 'integer'},
                                 'vibe_image_paths': {'description': 'Vibe Transfer 参考图路径列表（相对于工作区根目录），仅 v4+ 模型支持',
                                                      'type': 'array',
                                                      'items': {'type': 'string'}},
                                 'vibe_strength': {'description': 'Vibe Transfer 影响权重，0-1，默认 0.6',
                                                   'type': 'number'},
                                 'vibe_info_extracted': {'description': 'Vibe Transfer 信息提取级别，0-1，默认 1.0',
                                                         'type': 'number'}},
                  'required': ['prompt'],
                  'type': 'object'},
 'name': 'nai_generate'}

TIMEOUT_SEC = 65.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        # [AutoC 2026-05-31] Why: NAI generation failures should be preserved as
        # structured ok=false results even when the script exits non-zero. How: add
        # data.result with an ERROR prefix beside the error field. Purpose: keep API
        # and validation failures readable in tool history.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import json, os, subprocess, zipfile, random, hashlib, time
    
    VALID_MODELS = [
        'nai-diffusion-4-5-full',
        'nai-diffusion-4-5-curated',
        'nai-diffusion-4-full',
        'nai-diffusion-4-curated',
        'nai-diffusion-3',
    ]
    
    model = args.get('model', 'nai-diffusion-4-5-full')
    if model not in VALID_MODELS:
        fail(f'Invalid model: {model}. Valid: {VALID_MODELS}')
    
    is_v3 = model == 'nai-diffusion-3'
    is_v4_plus = not is_v3
    
    prompt = args.get('prompt', '')
    neg = args.get('negative_prompt', 'lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality')
    width = args.get('width', 832)
    height = args.get('height', 1216)
    scale = args.get('scale', 5.0)
    steps = args.get('steps', 23 if is_v4_plus else 28)
    sampler = args.get('sampler', 'k_euler_ancestral' if is_v4_plus else 'k_euler')
    noise_schedule = args.get('noise_schedule', 'karras')
    seed = args.get('seed', random.randint(0, 2**32-1))
    filename = args.get('filename', f'nai_{hashlib.md5(prompt.encode()).hexdigest()[:10]}.png')
    vibe_paths = args.get('vibe_image_paths', [])
    vibe_strength = args.get('vibe_strength', 0.6)
    vibe_info = args.get('vibe_info_extracted', 1.0)
    
    api_key = 'pst-Zm9Kgu0pe3UVw3YUxDVnB4K5hLUDgF7WPNbeQ6Z0jE0ysX7y8dYG0k5qsJ9zQ2r2'
    
    if is_v3:
        # V3 payload format (original)
        params = {
            'width': width,
            'height': height,
            'scale': scale,
            'sampler': sampler,
            'steps': steps,
            'seed': seed,
            'n_samples': 1,
            'negative_prompt': neg,
            'qualityToggle': True,
            'ucPreset': 0
        }
    else:
        # V4/V4.5 payload format — requires v4_prompt/v4_negative_prompt structures
        v4_prompt = {
            'caption': {
                'base_caption': prompt,
                'char_captions': []
            },
            'use_coords': False,
            'use_order': True
        }
        v4_negative_prompt = {
            'caption': {
                'base_caption': neg,
                'char_captions': []
            },
            'legacy_uc': False
        }
        params = {
            'params_version': 3,
            'width': width,
            'height': height,
            'scale': scale,
            'sampler': sampler,
            'steps': steps,
            'seed': seed,
            'n_samples': 1,
            'noise_schedule': noise_schedule,
            'prompt': prompt,
            'negative_prompt': neg,
            'v4_prompt': v4_prompt,
            'v4_negative_prompt': v4_negative_prompt,
            'qualityToggle': True,
            'ucPreset': 0,
            'cfg_rescale': 0,
            'legacy': False,
            'legacy_v3_extend': False,
            'deliberate_euler_ancestral_bug': False,
            'prefer_brownian': True,
            'autoSmea': False,
            'sm': False,
            'sm_dyn': False,
            'add_original_image': False,
            'dynamic_thresholding': False,
            'legacy_uc': False,
            'use_coords': False,
            'normalize_reference_strength_multiple': False,
            'characterPrompts': [],
            'skip_cfg_above_sigma': None,
        }
        
        # Vibe Transfer (v4+ requires pre-encoding via /ai/encode-vibe)
        # Cache dir: /tmp/nai_vibe_cache/ keyed by md5(file_content + model + info_extracted)
        if vibe_paths:
            import base64
            cache_dir = '/tmp/nai_vibe_cache'
            os.makedirs(cache_dir, exist_ok=True)
            encoded_vibes = []
            for vp in vibe_paths:
                img_path = vp if os.path.isabs(vp) else os.path.join('/www/wwwroot/Clonoth', vp)
                with open(img_path, 'rb') as f:
                    img_bytes = f.read()
                
                # Cache key: md5(image_bytes + model + info_extracted)
                cache_key = hashlib.md5(img_bytes + model.encode() + str(vibe_info).encode()).hexdigest()
                cache_file = os.path.join(cache_dir, f'{cache_key}.bin')
                
                if os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
                    # Cache hit — reuse encoded vibe (0 Anlas)
                    with open(cache_file, 'rb') as cf:
                        encoded_vibes.append(base64.b64encode(cf.read()).decode('utf-8'))
                else:
                    # Cache miss — call encode-vibe API (2 Anlas)
                    b64_img = base64.b64encode(img_bytes).decode('utf-8')
                    encode_data = json.dumps({
                        'image': b64_img,
                        'model': model,
                        'informationExtracted': vibe_info
                    })
                    encode_tmp = f'/tmp/nai_encode_{seed}.json'
                    encode_out = f'/tmp/nai_encoded_{seed}.bin'
                    with open(encode_tmp, 'w') as ef:
                        ef.write(encode_data)
                    
                    enc_result = subprocess.run(
                        ['curl', '-s', '-m', '60',
                         'https://image.novelai.net/ai/encode-vibe',
                         '-H', f'Authorization: Bearer {api_key}',
                         '-H', 'Content-Type: application/json',
                         '-d', f'@{encode_tmp}',
                         '-o', encode_out,
                         '-w', '%{http_code}'],
                        capture_output=True, text=True, timeout=65
                    )
                    enc_code = enc_result.stdout.strip()
                    if enc_code != '200':
                        try: os.remove(encode_tmp)
                        except: pass
                        fail(f'Vibe encode failed HTTP {enc_code}')
                    
                    # Save to cache
                    shutil.copy2(encode_out, cache_file)
                    
                    with open(encode_out, 'rb') as ef:
                        encoded_vibes.append(base64.b64encode(ef.read()).decode('utf-8'))
                    
                    try:
                        os.remove(encode_tmp)
                        os.remove(encode_out)
                    except: pass
            
            params['reference_image_multiple'] = encoded_vibes
            params['reference_strength_multiple'] = [vibe_strength] * len(encoded_vibes)
            params['reference_information_extracted_multiple'] = [vibe_info] * len(encoded_vibes)
            params['normalize_reference_strength_multiple'] = True
    
    payload = json.dumps({
        'input': prompt,
        'model': model,
        'action': 'generate',
        'parameters': params
    })
    
    tmp_zip = f'/tmp/nai_{seed}.zip'
    tmp_dir = f'/tmp/nai_{seed}_out'
    tmp_payload = f'/tmp/nai_{seed}_payload.json'
    out_dir = 'data/temp'
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    
    # Write payload to temp file to avoid OS argument length limit (esp. with vibe base64)
    with open(tmp_payload, 'w') as pf:
        pf.write(payload)
    
    result = subprocess.run(
        ['curl', '-s', '-m', '120',
         'https://image.novelai.net/ai/generate-image',
         '-H', f'Authorization: Bearer {api_key}',
         '-H', 'Content-Type: application/json',
         '-d', f'@{tmp_payload}',
         '-o', tmp_zip,
         '-w', '%{http_code}'],
        capture_output=True, text=True, timeout=130
    )
    
    http_code = result.stdout.strip()
    if http_code != '200':
        try:
            err_body = open(tmp_zip).read()
        except:
            err_body = 'unknown'
        fail(f'NAI API returned HTTP {http_code}: {err_body}')
    
    try:
        with zipfile.ZipFile(tmp_zip, 'r') as zf:
            zf.extractall(tmp_dir)
    except Exception as e:
        fail(f'Failed to extract zip: {e}')
    
    images = [f for f in os.listdir(tmp_dir) if f.endswith('.png')]
    if not images:
        fail('No PNG found in response')
    
    src = os.path.join(tmp_dir, images[0])
    dst = os.path.join(out_dir, filename)
    import shutil
    shutil.move(src, dst)
    
    # Cleanup
    try:
        os.remove(tmp_zip)
        os.remove(tmp_payload)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except:
        pass
    
    # [AutoC 2026-05-31] Why: NAI output is a generated image and should follow
    # the same nested data plus mirrored attachments contract as other media tools.
    # How: keep seed and generation parameters under data, put a readable path line
    # in data.result, and mirror attachments at the top level. Purpose: preserve
    # final attachment delivery and structured generation metadata.
    output({
        'ok': True,
        'data': {
            'result': f'Image generated: {dst}',
            'path': dst,
            'attachments': [dst],
            'seed': seed,
            'width': width,
            'height': height,
            'steps': steps,
            'sampler': sampler,
            'scale': scale,
            'model': model
        },
        'attachments': [dst]
    })
