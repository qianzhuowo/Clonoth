from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {
    "name": "exa_search",
    "description": (
        "使用 Exa API 进行联网搜索，适合通用网页搜索、资料检索和带来源引用的研究任务。"
        "返回搜索结果摘要、URL 引用和结构化结果列表。需要 EXA_API_KEY 环境变量或 .env 配置，"
        "也可在参数 api_key 中显式传入。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询。可以使用自然语言描述要查找的信息。",
            },
            "api_key": {
                "type": "string",
                "description": "Exa API key。不传则使用环境变量或 .env 中的 EXA_API_KEY。",
            },
            "base_url": {
                "type": "string",
                "description": "Exa API base URL。不传则使用环境变量或 .env 中的 EXA_BASE_URL，默认 https://api.exa.ai。",
            },
            "num_results": {
                "type": "integer",
                "default": 5,
                "description": "返回结果数量，默认 5，最大 20。",
            },
            "search_type": {
                "type": "string",
                "default": "auto",
                "enum": ["auto", "neural", "keyword"],
                "description": "搜索类型。auto=自动选择，neural=语义搜索，keyword=关键词搜索。默认 auto。",
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，仅搜索这些域名，例如 [\"example.com\"]。",
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，排除这些域名。",
            },
            "start_published_date": {
                "type": "string",
                "description": "可选，限定发布日期起点，ISO 日期字符串，例如 2024-01-01T00:00:00.000Z。",
            },
            "end_published_date": {
                "type": "string",
                "description": "可选，限定发布日期终点，ISO 日期字符串。",
            },
            "use_autoprompt": {
                "type": "boolean",
                "default": True,
                "description": "是否让 Exa 自动优化搜索查询，默认 true。",
            },
            "text_max_characters": {
                "type": "integer",
                "default": 1000,
                "description": "每条结果最多返回的正文字符数，默认 1000，最大 5000。设为 0 可不请求正文。",
            },
        },
        "required": ["query"],
    },
}

TIMEOUT_SEC = 75.0


if __name__ == "__main__":
    import json
    import os
    import sys
    import urllib.error
    import urllib.request

    _input = json.loads((sys.stdin.read() or "{}").lstrip("\ufeff"))

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    def as_clean_string_list(value):
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def clamp_int(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def load_dotenv():
        env_path = os.path.join(os.getcwd(), ".env")
        values = {}
        if not os.path.isfile(env_path):
            return values
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f.read().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("'\"")
        except Exception:
            return {}
        return values

    args = _input if isinstance(_input, dict) else {}

    query = str(args.get("query") or "").strip()
    if not query:
        fail("query is required")

    api_key = str(args.get("api_key") or os.environ.get("EXA_API_KEY") or "").strip()
    base_url = str(args.get("base_url") or os.environ.get("EXA_BASE_URL") or "").strip().rstrip("/")

    if not api_key or not base_url:
        dotenv = load_dotenv()
        if not api_key:
            api_key = str(dotenv.get("EXA_API_KEY") or "").strip()
        if not base_url:
            base_url = str(dotenv.get("EXA_BASE_URL") or "").strip().rstrip("/")

    if not api_key:
        fail("No Exa API key available. Set EXA_API_KEY in the environment or .env, or pass api_key.")
    if not base_url:
        base_url = "https://api.exa.ai"
    num_results = clamp_int(args.get("num_results", 5), 5, 1, 20)
    search_type = str(args.get("search_type") or "auto").strip().lower()
    if search_type not in {"auto", "neural", "keyword"}:
        search_type = "auto"
    text_max_characters = clamp_int(args.get("text_max_characters", 1000), 1000, 0, 5000)

    payload = {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "useAutoprompt": bool(args.get("use_autoprompt", True)),
    }

    include_domains = as_clean_string_list(args.get("include_domains"))
    exclude_domains = as_clean_string_list(args.get("exclude_domains"))
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains

    start_published_date = str(args.get("start_published_date") or "").strip()
    end_published_date = str(args.get("end_published_date") or "").strip()
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    if end_published_date:
        payload["endPublishedDate"] = end_published_date

    if text_max_characters > 0:
        payload["contents"] = {
            "text": {"maxCharacters": text_max_characters},
            "highlights": {"numSentences": 2, "highlightsPerUrl": 2},
        }

    req = urllib.request.Request(
        f"{base_url}/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=65) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            body = ""
        fail(f"Exa API request failed: HTTP {e.code} {e.reason}. {body}")
    except Exception as e:
        fail(f"Exa API request failed: {e}")

    raw_results = response_data.get("results", [])
    if not isinstance(raw_results, list):
        raw_results = []

    formatted_results = []
    citations = []
    lines = [f"Exa 搜索结果：{query}"]

    for index, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        published_date = str(item.get("publishedDate") or "").strip()
        author = str(item.get("author") or "").strip()
        text = str(item.get("text") or "").strip()
        highlights = item.get("highlights") if isinstance(item.get("highlights"), list) else []

        if url:
            citations.append(url)

        snippet = text
        if not snippet and highlights:
            snippet = "\n".join(str(h).strip() for h in highlights if str(h).strip())
        snippet = snippet[:1200].strip()

        lines.append("")
        lines.append(f"[{index}] {title}")
        if url:
            lines.append(f"URL: {url}")
        meta = "；".join(part for part in [f"发布时间: {published_date}" if published_date else "", f"作者: {author}" if author else ""] if part)
        if meta:
            lines.append(meta)
        if snippet:
            lines.append(f"摘要: {snippet}")

        formatted_results.append({
            "title": title,
            "url": url,
            "published_date": published_date,
            "author": author,
            "score": item.get("score"),
            "text": text,
            "highlights": highlights,
            "summary": item.get("summary"),
            "id": item.get("id"),
        })

    result_text = "\n".join(lines).strip()
    if not formatted_results:
        result_text = f"Exa 搜索没有返回结果：{query}"

    output({
        "ok": True,
        "data": {
            "result": result_text,
            "query": query,
            "citations": citations,
            "results": formatted_results,
            "autoprompt_string": response_data.get("autopromptString"),
            "request_id": response_data.get("requestId"),
        },
    })
