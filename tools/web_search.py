from __future__ import annotations

"""
Fault-tolerant web search wrapper for Clonoth.

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
"""

SPEC = {
    "name": "web_search",
    "description": (
        "极简联网搜索工具。用于网页搜索、最新资料检索、新闻查询，以及 X/Twitter 帖子搜索。"
        "只需要填写 query 字段，例如：{\"query\": \"今天 AI 新闻\"}。"
        "不要调用 exa_search 或 x_search；本工具会根据配置自动选择 Exa 或 X 搜索。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或用户问题原文。必填。",
            },
            "num_results": {
                "type": "integer",
                "default": 5,
                "description": "返回结果数量，默认 5，最大 10。",
            },
            "source": {
                "type": "string",
                "default": "auto",
                "enum": ["auto", "exa", "x", "both"],
                "description": "可选搜索来源。默认 auto。一般不要填写；需要 X/Twitter 时可填 x。",
            },
        },
        "required": ["query"],
    },
}

TIMEOUT_SEC = 180.0


if __name__ == "__main__":
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from typing import Any

    TOOL_DIR = Path(__file__).resolve().parent

    def output(result: dict[str, Any]) -> None:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error: str, *, hint: str = "") -> None:
        message = str(error)
        if hint:
            message = f"{message}\n修复建议：{hint}"
        print(json.dumps({"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}}, ensure_ascii=False))
        sys.exit(1)

    def load_dotenv() -> dict[str, str]:
        env_path = Path(os.getcwd()) / ".env"
        values: dict[str, str] = {}
        if not env_path.is_file():
            return values
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
        except Exception:
            return {}
        return values

    def has_config_key(dotenv: dict[str, str], *names: str) -> bool:
        for name in names:
            if str(os.environ.get(name) or dotenv.get(name) or "").strip():
                return True
        return False

    def extract_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return " ".join(str(item).strip() for item in value if str(item).strip()).strip()
        if not isinstance(value, dict):
            return ""

        query_keys = [
            "query", "q", "keyword", "keywords", "search", "search_query", "searchQuery",
            "term", "terms", "text", "content", "prompt", "question", "input", "message",
        ]
        for key in query_keys:
            if key in value:
                extracted = extract_text(value.get(key))
                if extracted:
                    return extracted

        nested_keys = ["args", "arguments", "params", "parameters", "data", "payload", "request"]
        for key in nested_keys:
            if key in value:
                extracted = extract_text(value.get(key))
                if extracted:
                    return extracted

        return ""

    def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def extract_int_alias(args: dict[str, Any], keys: list[str], default: int, minimum: int, maximum: int) -> int:
        for key in keys:
            if key not in args:
                continue
            return clamp_int(args.get(key), default, minimum, maximum)
        return default

    def normalize_source(args: dict[str, Any], query: str, dotenv: dict[str, str]) -> str:
        raw = ""
        for key in ["source", "provider", "search_provider", "searchProvider", "engine", "type"]:
            if key in args:
                raw = str(args.get(key) or "").strip().lower()
                break
        if not raw:
            raw = str(os.environ.get("WEB_SEARCH_PROVIDER") or dotenv.get("WEB_SEARCH_PROVIDER") or os.environ.get("WEB_SEARCH_SOURCE") or dotenv.get("WEB_SEARCH_SOURCE") or "auto").strip().lower()

        aliases = {
            "web": "exa",
            "general": "exa",
            "google": "exa",
            "exa_search": "exa",
            "twitter": "x",
            "tweet": "x",
            "tweets": "x",
            "x_search": "x",
            "x/twitter": "x",
            "all": "both",
            "multi": "both",
        }
        source = aliases.get(raw, raw)
        if source not in {"auto", "exa", "x", "both"}:
            source = "auto"

        if source != "auto":
            return source

        lower_query = query.lower()
        wants_x = any(token in lower_query for token in ["twitter", "tweet", "tweets", "x 上", "x上", "推特", "推文"])
        has_exa = has_config_key(dotenv, "EXA_API_KEY")
        has_x = has_config_key(dotenv, "XAI_API_KEY", "OPENAI_API_KEY")
        if wants_x and has_x:
            return "x"
        if has_exa:
            return "exa"
        if has_x:
            return "x"
        return "exa"

    def run_child(tool_name: str, child_args: dict[str, Any]) -> dict[str, Any]:
        script = TOOL_DIR / f"{tool_name}.py"
        if not script.is_file():
            return {"ok": False, "error": f"tool script not found: {tool_name}", "data": {"result": f"ERROR: tool script not found: {tool_name}"}}
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(child_args, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
                timeout=150,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"{tool_name} timeout", "data": {"result": f"ERROR: {tool_name} timeout"}}
        except Exception as exc:
            return {"ok": False, "error": f"{tool_name} execution failed: {exc}", "data": {"result": f"ERROR: {tool_name} execution failed: {exc}"}}

        stdout = (proc.stdout or "").strip()
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        err = (proc.stderr or "").strip()
        text = stdout or err or f"{tool_name} exited with code {proc.returncode}"
        ok = proc.returncode == 0
        return {"ok": ok, "error": "" if ok else text, "data": {"result": text}}

    raw_input = json.loads((sys.stdin.read() or "{}").lstrip("\ufeff"))
    args = raw_input if isinstance(raw_input, dict) else {}
    query = extract_text(raw_input)
    if not query:
        fail(
            "缺少搜索关键词。",
            hint="请重新调用 web_search，格式固定为：{\"query\": \"要搜索的内容\"}。如果已有用户问题，请把用户问题原文放入 query。",
        )

    num_results = extract_int_alias(
        args,
        ["num_results", "numResults", "max_results", "maxResults", "limit", "top_k", "topK", "count"],
        5,
        1,
        10,
    )
    dotenv = load_dotenv()
    source = normalize_source(args, query, dotenv)

    calls: list[tuple[str, dict[str, Any]]] = []
    if source in {"exa", "both"}:
        calls.append(("exa_search", {"query": query, "num_results": num_results}))
    if source in {"x", "both"}:
        calls.append(("x_search", {"query": query, "max_tokens": 16000}))

    if not calls:
        calls.append(("exa_search", {"query": query, "num_results": num_results}))

    child_results: dict[str, dict[str, Any]] = {}
    result_sections: list[str] = [f"联网搜索结果：{query}"]
    citations: list[str] = []
    any_ok = False

    for tool_name, child_args in calls:
        result = run_child(tool_name, child_args)
        child_results[tool_name] = result
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        result_text = str(data.get("result") or result.get("error") or "").strip()
        label = "Exa 网页搜索" if tool_name == "exa_search" else "X/Twitter 搜索"
        if result.get("ok") is True:
            any_ok = True
            result_sections.extend(["", f"## {label}", result_text or "搜索成功，但没有返回可读文本。"])
            for url in data.get("citations") or []:
                url_text = str(url or "").strip()
                if url_text and url_text not in citations:
                    citations.append(url_text)
        else:
            result_sections.extend(["", f"## {label}失败", result_text or str(result.get("error") or "未知错误")])

    final_text = "\n".join(result_sections).strip()
    if not any_ok:
        final_text += "\n\n修复建议：请确认 .env 或环境变量中已配置 EXA_API_KEY，或 XAI_API_KEY/OPENAI_API_KEY。工具调用格式固定为：{\"query\": \"要搜索的内容\"}。"
        output({
            "ok": False,
            "error": "all configured search providers failed",
            "data": {
                "result": final_text,
                "query": query,
                "source": source,
                "citations": citations,
                "providers": child_results,
            },
        })

    output({
        "ok": True,
        "data": {
            "result": final_text,
            "query": query,
            "source": source,
            "citations": citations,
            "providers": child_results,
        },
    })
