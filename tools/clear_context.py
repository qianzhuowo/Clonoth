from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '清空指定 Discord 频道的对话上下文（Bot 端历史 + Engine session 上下文）。channel_id 为 Discord 频道 ID。',
 'input_schema': {'properties': {'channel_id': {'description': 'Discord 频道 ID', 'type': 'string'}},
                  'required': ['channel_id'],
                  'type': 'object'},
 'name': 'clear_context'}

TIMEOUT_SEC = 30.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error): print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import httpx
    import shutil
    from pathlib import Path
    
    channel_id = str(args.get("channel_id", "")).strip()
    if not channel_id:
        fail("channel_id is required")
    
    results = {}
    
    # 1. 清 Bot 端 _channel_history
    try:
        resp = httpx.post(
            "http://127.0.0.1:8768/discord",
            json={"code": f"_channel_history[{channel_id}] = []\nreturn {{'cleared': True}}"},
            timeout=10.0,
        )
        bot_result = resp.json()
        results["bot_history"] = "cleared" if bot_result.get("ok") else f"failed: {bot_result}"
    except Exception as e:
        results["bot_history"] = f"error: {e}"
    
    # 2. 查 session_id
    conversation_key = f"discord:{channel_id}"
    session_id = None
    try:
        resp = httpx.get("http://127.0.0.1:8765/v1/health", timeout=5.0)
        if resp.status_code == 200:
            workspace_root = resp.json().get("workspace_root", "")
    except Exception:
        workspace_root = "/www/wwwroot/Clonoth"
    
    # 从 events 查找 session
    try:
        resp = httpx.get(
            "http://127.0.0.1:8765/v1/events",
            params={"after_seq": 0, "types": "session_created"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            for ev in resp.json():
                payload = ev.get("payload") or {}
                if payload.get("conversation_key") == conversation_key:
                    session_id = ev.get("session_id")
    except Exception:
        pass
    
    # 3. 删 Engine context 文件
    if session_id:
        ctx_dir = Path(workspace_root) / "data" / "node_contexts" / session_id
        if ctx_dir.exists() and ctx_dir.is_dir():
            file_count = len(list(ctx_dir.glob("*.json")))
            shutil.rmtree(ctx_dir, ignore_errors=True)
            results["engine_context"] = f"deleted {file_count} files for session {session_id}"
        else:
            results["engine_context"] = f"no context dir for session {session_id}"
    else:
        # 尝试直接搜索
        ctx_base = Path(workspace_root) / "data" / "node_contexts"
        deleted = 0
        if ctx_base.exists():
            for d in ctx_base.iterdir():
                if d.is_dir():
                    for f in d.glob("*.json"):
                        try:
                            import json
                            data = json.loads(f.read_text())
                            # 检查是否包含这个 conversation_key 相关内容
                        except:
                            pass
        results["engine_context"] = f"session_id not found for {conversation_key}, context files may need manual cleanup"
    
    results["session_id"] = session_id
    results["conversation_key"] = conversation_key
    output(results)
