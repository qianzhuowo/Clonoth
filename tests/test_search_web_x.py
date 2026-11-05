"""search_web: X/Twitter search is merged in and only enabled by XAI_API_KEY.

The tool is copied into an isolated root so the developer's own .env / data/keys_*
files cannot enable X by accident. Only the X branch is exercised (against a local
fake Responses endpoint); the web engines need the internet and are not touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, args: dict, env_extra: dict | None = None, dotenv: str = "") -> tuple[int, dict]:
    (tmp_path / "tools").mkdir(exist_ok=True)
    shutil.copy(ROOT / "tools" / "search_web.py", tmp_path / "tools" / "search_web.py")
    if dotenv:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("XAI_", "TINYFISH_", "EXA_")) and "proxy" not in k.lower()}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "search_web.py")],
        input=json.dumps(args), capture_output=True, text=True, encoding="utf-8",
        timeout=60, env=env, cwd=str(tmp_path),
    )
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def fake_xai():
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            payload = {
                "model": body["model"],
                "output": [{"type": "message", "content": [{
                    "type": "output_text", "text": "post summary",
                    "annotations": [{"type": "url_citation", "url": "https://x.com/a/status/1"}] * 2,
                }]}],
                "usage": {"input_tokens": 3, "output_tokens": 4,
                          "server_side_tool_usage_details": {"x_search_calls": 1}},
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen
    server.shutdown()


def test_x_only_without_xai_key_is_rejected(tmp_path):
    # OPENAI_API_KEY must not enable X: it usually points at a gateway without x_search.
    rc, payload = _run(tmp_path, {"query": "q", "sources": "x"}, {"OPENAI_API_KEY": "sk-gateway"})
    assert rc == 1 and payload["ok"] is False
    assert "XAI_API_KEY" in payload["error"]


def test_unknown_sources_is_rejected(tmp_path):
    rc, payload = _run(tmp_path, {"query": "q", "sources": "bing"})
    assert rc == 1 and "unknown sources" in payload["error"]


def test_x_search_via_dotenv_key(tmp_path, fake_xai):
    base, seen = fake_xai
    rc, payload = _run(
        tmp_path, {"query": "today AI", "sources": "x"},
        dotenv=f"XAI_API_KEY=xai-test\nXAI_BASE_URL={base}\nXAI_MODEL=grok-test\n",
    )
    assert rc == 0, payload
    assert seen[0]["path"] == "/v1/responses"
    assert seen[0]["auth"] == "Bearer xai-test"
    assert seen[0]["body"]["model"] == "grok-test"
    assert seen[0]["body"]["tools"] == [{"type": "x_search"}]
    x = payload["data"]["x"]
    assert x["text"] == "post summary"
    assert x["citations"] == ["https://x.com/a/status/1"]
    assert "[X/Twitter] post summary" in payload["data"]["result"]
    assert "engine" not in payload["data"]  # web was not searched


def test_x_failure_reports_error(tmp_path):
    rc, payload = _run(
        tmp_path, {"query": "q", "sources": "x"},
        {"XAI_API_KEY": "xai-test", "XAI_BASE_URL": "http://127.0.0.1:9/v1"},
    )
    assert rc == 1 and payload["ok"] is False
    assert "X search failed" in payload["error"]
