from __future__ import annotations

"""Unified web search and batch page fetching via TinyFish, Parallel and Exa."""

SPEC = {
    "pure": True,
    "name": "search_web",
    "description": (
        '统一网页搜索（action="search"，默认，传 query）与网页正文批量抓取'
        '（action="fetch"，传 urls，支持 1~10 个 URL；仅传 urls 时自动识别）。'
        'engine 支持 auto（默认，tinyfish -> parallel -> exa 自动回退）、'
        'tinyfish、parallel（免 Key）、exa。'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "fetch"], "default": "search", "description": "search=搜索网页（默认）；fetch=批量抓取网页正文"},
            "engine": {"type": "string", "enum": ["auto", "tinyfish", "parallel", "exa"], "default": "auto", "description": "搜索引擎：auto（默认，tinyfish->parallel->exa 回退）、tinyfish、parallel（免Key）、exa"},
            "query": {"type": "string", "description": "search 模式的搜索词或自然语言问题"},
            "urls": {"type": "array", "items": {"type": "string"}, "description": "fetch 模式要抓取的 URL 列表（1~10 个 http/https 链接）"},
            "count": {"type": "integer", "default": 10, "description": "search 模式返回结果条数（1~20，默认 10）"},
        },
        "required": [],
    },
}

TIMEOUT_SEC = 130.0

if __name__ == "__main__":
    import json
    import os
    import sys
    import uuid
    from pathlib import Path
    from urllib import request as urllib_request
    from urllib.error import HTTPError
    from urllib.parse import quote

    import yaml

    _input = json.loads(sys.stdin.read())
    args = _input.get("args", _input) if isinstance(_input, dict) else {}

    def output(payload: dict):
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(0)

    def fail(error: str):
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "data": {"result": f"ERROR: {error}"},
        }, ensure_ascii=False))
        sys.exit(1)

    ROOT = Path(__file__).resolve().parent.parent

    def load_dotenv() -> dict[str, str]:
        env_path = ROOT / ".env"
        kv: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip("\'\"")
        return kv

    def load_first_line(rel_path: str) -> str:
        p = ROOT / rel_path
        if not p.exists():
            return ""
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        return ""

    dotenv = load_dotenv()

    def load_key(name: str, rel_path: str) -> str:
        return (os.environ.get(name, "").strip() or dotenv.get(name, "").strip()
                or load_first_line(rel_path))

    def load_exa_key() -> str:
        key = load_key("EXA_API_KEY", "data/keys_exa.txt")
        if key:
            return key
        path = ROOT / "data" / "mcp_clients.yaml"
        if not path.exists():
            return ""
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str(config.get("clients", {}).get("exa", {}).get("headers", {}).get("x-api-key") or "").strip()

    def http_json(url: str, method: str = "GET", headers: dict | None = None, body: dict | None = None, timeout: int = 35):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = {"User-Agent": "Clonoth-SearchWeb/2.0", "Accept": "application/json, text/event-stream"}
        if headers:
            h.update(headers)
        if data is not None and "Content-Type" not in h:
            h["Content-Type"] = "application/json"
        req = urllib_request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                if raw.lstrip().startswith(("event:", "data:")) or "\ndata:" in raw:
                    for line in reversed(raw.splitlines()):
                        if line.startswith("data:"):
                            try:
                                return r.status, json.loads(line[5:].strip())
                            except Exception:
                                pass
                return r.status, json.loads(raw)
        except HTTPError as e:
            try:
                err_raw = e.read().decode("utf-8", errors="replace")
                err_payload = json.loads(err_raw)
            except Exception:
                err_payload = err_raw if "err_raw" in locals() else str(e)
            return e.code, err_payload
        except Exception as e:
            return 0, {"error": str(e)}

    def format_search_summary(engine_used: str, query_str: str, items: list[dict]) -> str:
        lines = [f"[{engine_used}] Search results for {query_str!r} ({len(items)} items):"]
        for idx, it in enumerate(items, 1):
            title = it.get("title") or "(no title)"
            u = it.get("url") or ""
            snip = (it.get("snippet") or "").strip()
            lines.append(f"{idx}. {title}\n   URL: {u}" + (f"\n   {snip}" if snip else ""))
        return "\n".join(lines)

    def format_fetch_summary(engine_used: str, items: list[dict]) -> str:
        lines = [f"[{engine_used}] Fetched {len(items)} page(s):"]
        for idx, it in enumerate(items, 1):
            title = it.get("title") or ""
            u = it.get("final_url") or it.get("url") or ""
            txt = (it.get("text") or "").strip()
            lines.append(f"--- [{idx}] {title} ({u}) ---\n{txt}")
        return "\n\n".join(lines)

    def search_tinyfish(q: str, count: int) -> list[dict] | None:
        tf_key = load_key("TINYFISH_API_KEY", "data/keys_tinyfish.txt")
        if not tf_key:
            return None
        url = f"https://api.search.tinyfish.ai?query={quote(q)}&count={count}"
        st, data = http_json(url, headers={"X-API-Key": tf_key}, timeout=25)
        if st != 200 or not isinstance(data, dict):
            return None
        results = data.get("results") or []
        return [
            {"title": r.get("title"), "url": r.get("url"), "snippet": (r.get("snippet") or "")[:500]}
            for r in results
        ]

    def _extract_mcp_texts(st: int, data: object) -> list[str] | None:
        if st != 200 or not isinstance(data, dict):
            return None
        if data.get("error"):
            return None
        res_obj = data.get("result")
        if not isinstance(res_obj, dict) or res_obj.get("isError"):
            return None
        content_list = res_obj.get("content") or []
        texts: list[str] = []
        for c in content_list:
            if not isinstance(c, dict):
                continue
            t = str(c.get("text") or "").strip()
            if not t:
                continue
            if t.startswith(("web_search_exa error", "web_fetch_exa error", "web_search error", "MCP error")):
                return None
            texts.append(t)
        return texts or None

    def search_exa(q: str, count: int) -> list[dict] | None:
        exa_key = load_exa_key()
        if not exa_key:
            return None
        st, data = http_json(
            f"https://mcp.exa.ai/mcp?exaApiKey={quote(exa_key, safe='')}",
            method="POST",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_search_exa",
                    "arguments": {"query": q, "objective": q, "numResults": count},
                },
            },
            timeout=35,
        )
        texts = _extract_mcp_texts(st, data)
        if not texts:
            return None
        return [{"title": "Exa Search Result", "url": "", "snippet": "\n\n".join(texts)[:6000]}]

    def search_parallel(q: str, count: int) -> list[dict] | None:
        clean_kw = " ".join(q.replace('"', " ").split()).strip() or q
        st, data = http_json(
            "https://search.parallel.ai/mcp",
            method="POST",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_search",
                    "arguments": {
                        "objective": q,
                        "search_queries": [clean_kw[:200]],
                        "session_id": uuid.uuid4().hex,
                    },
                },
            },
            timeout=35,
        )
        texts = _extract_mcp_texts(st, data)
        if not texts:
            return None
        try:
            parsed = json.loads(texts[0])
            if isinstance(parsed, dict) and isinstance(parsed.get('results'), list):
                rows = parsed['results']
                if not rows:
                    return None
                return [
                    {
                        'title': r.get('title') or r.get('url') or '(no title)',
                        'url': r.get('url') or '',
                        'snippet': ('\n'.join(r.get('excerpts') or []))[:800],
                    }
                    for r in rows[:count] if isinstance(r, dict)
                ]
        except Exception:
            pass
        return [{"title": "Parallel Search Result", "url": "", "snippet": "\n\n".join(texts)[:6000]}]

    def fetch_tinyfish(url_list: list[str]) -> list[dict] | None:
        tf_key = load_key("TINYFISH_API_KEY", "data/keys_tinyfish.txt")
        if not tf_key:
            return None
        st, data = http_json(
            "https://api.fetch.tinyfish.ai",
            method="POST",
            headers={"X-API-Key": tf_key},
            body={"urls": url_list[:10]},
            timeout=60,
        )
        if st != 200 or not data:
            return None
        items = data if isinstance(data, list) else data.get("results", [data])
        return [
            {
                "url": it.get("url"),
                "final_url": it.get("final_url"),
                "title": it.get("title"),
                "text": it.get("text"),
                "published_date": it.get("published_date"),
            }
            for it in items if isinstance(it, dict)
        ]

    def fetch_exa(url_list: list[str]) -> list[dict] | None:
        exa_key = load_exa_key()
        if not exa_key:
            return None
        st, data = http_json(
            f"https://mcp.exa.ai/mcp?exaApiKey={quote(exa_key, safe='')}",
            method="POST",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_fetch_exa",
                    "arguments": {"urls": url_list[:10], "maxCharacters": 8000},
                },
            },
            timeout=45,
        )
        texts = _extract_mcp_texts(st, data)
        if not texts:
            return None
        return [{"url": url_list[0] if url_list else "", "title": "Exa Fetch", "text": "\n\n".join(texts)}]

    def fetch_parallel(url_list: list[str]) -> list[dict] | None:
        st, data = http_json(
            "https://search.parallel.ai/mcp",
            method="POST",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_fetch",
                    "arguments": {
                        "urls": url_list[:10],
                        "session_id": uuid.uuid4().hex,
                    },
                },
            },
            timeout=45,
        )
        texts = _extract_mcp_texts(st, data)
        if not texts:
            return None
        try:
            parsed = json.loads(texts[0])
            if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
                rows = parsed["results"]
                if not rows:
                    return None
                out_rows = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    body_txt = r.get("full_content") or "\n\n".join(r.get("excerpts") or [])
                    if body_txt:
                        out_rows.append({
                            "url": r.get("url"),
                            "final_url": r.get("url"),
                            "title": r.get("title") or "",
                            "text": body_txt,
                            "published_date": r.get("publish_date"),
                        })
                return out_rows or None
        except Exception:
            pass
        return [{"url": url_list[0] if url_list else "", "title": "Parallel Fetch", "text": "\n\n".join(texts)}]

    action = (args.get("action") or "search").strip().lower()
    engine = (args.get("engine") or "auto").strip().lower()
    if engine not in ("auto", "tinyfish", "parallel", "exa"):
        fail(f"unknown engine {engine!r}")

    if action == "fetch" or (not args.get("query") and args.get("urls")):
        urls = args.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()][:10]
        if not urls:
            fail('fetch mode requires "urls"')
        engines = (("tinyfish", fetch_tinyfish), ("parallel", fetch_parallel), ("exa", fetch_exa))
        action = "fetch"
        params = (urls,)
    elif action == "search":
        query = (args.get("query") or "").strip()
        if not query:
            fail('search mode requires "query"')
        count = min(max(int(args.get("count") or 10), 1), 20)
        engines = (("tinyfish", search_tinyfish), ("parallel", search_parallel), ("exa", search_exa))
        params = (query, count)
    else:
        fail(f"unknown action {action!r}, use 'search' or 'fetch'")

    for used_engine, fn in engines:
        if engine != "auto" and engine != used_engine:
            continue
        items = fn(*params)
        if items:
            break
    else:
        fail(f"{action} failed or returned empty on engine={engine}")

    summary = (format_fetch_summary(used_engine, items) if action == "fetch"
               else format_search_summary(used_engine, query, items))
    data = {"engine": used_engine, "count": len(items), "results": items}
    if action == "search":
        data["query"] = query
    output({"ok": True, "action": action, **data, "data": {"result": summary, **data}})
