from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '搜索 X/Twitter 上的帖子。通过 xAI Grok API 的 Responses API + x_search '
                '工具实现，支持语义搜索和关键词搜索，返回帖子内容和链接引用。',
 'input_schema': {'properties': {'api_key': {'description': 'xAI API key，不传则从 data/xai_key.txt 读取',
                                             'type': 'string'},
                                 'max_tokens': {'default': 64000,
                                                'description': '最大输出 token 数，默认 64000',
                                                'type': 'integer'},
                                 'model': {'default': 'grok-4.3',
                                           'description': '使用的模型，默认 grok-4.3',
                                           'type': 'string'},
                                 'query': {'description': '搜索查询，用自然语言描述想搜什么', 'type': 'string'}},
                  'required': ['query'],
                  'type': 'object'},
 'name': 'x_search'}

TIMEOUT_SEC = 130.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        # [AutoC 2026-05-31] Why: x_search failures should remain readable after
        # the engine prefers data.result. How: emit ok=false with data.result before
        # exiting non-zero. Purpose: preserve specific API errors in tool history.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import urllib.request
    import json
    import os
    
    query = args.get('query', '')
    model = args.get('model', 'grok-4.3')
    max_tokens = args.get('max_tokens', 64000)
    api_key = args.get('api_key', '')
    
    if not api_key:
        api_key = os.environ.get('ZOAHOLIC_API_KEY', 'sk-KJr51AvQsCfphE11zPtZInappmQS64ZYecrk1uKnlncy7Qjv')
    
    if not api_key:
        fail('No API key available')
    
    payload = json.dumps({
        'model': model,
        'input': query,
        'tools': [{'type': 'x_search'}],
        'max_output_tokens': max_tokens
    }).encode('utf-8')
    
    req = urllib.request.Request(
        'https://zoaholic.zhenxia.top/v1/responses',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        fail(f'API request failed: {e}')
    
    final_text = ''
    annotations = []
    reasoning_summary = ''
    
    for item in data.get('output', []):
        if item.get('type') == 'message':
            for content in item.get('content', []):
                if content.get('type') == 'output_text':
                    final_text = content.get('text', '')
                    for ann in content.get('annotations', []):
                        if ann.get('type') == 'url_citation':
                            annotations.append(ann.get('url', ''))
        elif item.get('type') == 'reasoning':
            for s in item.get('summary', []):
                if s.get('text'):
                    reasoning_summary += s['text'] + '\n'
    
    usage = data.get('usage', {})
    
    usage_info = {
        'input_tokens': usage.get('input_tokens', 0),
        'output_tokens': usage.get('output_tokens', 0),
        'total_tokens': usage.get('total_tokens', 0),
        'x_search_calls': usage.get('server_side_tool_usage_details', {}).get('x_search_calls', 0),
        'cost_usd': usage.get('cost_in_usd_ticks', 0) / 1_000_000_000
    }
    # [AutoC 2026-05-31] Why: x_search used to return primary text at the top
    # level, but all tools now expose human-readable output as data.result. How:
    # keep text, citations, model, and usage under data, with result matching the
    # final answer text. Purpose: preserve structured search metadata while making
    # engine rendering uniform.
    result = {
        'ok': True,
        'data': {
            'result': final_text,
            'text': final_text,
            'citations': list(set(annotations)),
            'model': data.get('model', model),
            'usage': usage_info
        }
    }
    
    output(result)
