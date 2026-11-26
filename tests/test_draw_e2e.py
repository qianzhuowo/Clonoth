"""绘图插件端到端走查：真实加载器 + 真实引擎节点入口 + 真实路由面。

- engine 进程：load_single_plugin 注册 DrawAgentEntry → run_ai_node(draw.novelai_planner) 由钩子接管，
  假 provider 返回 submit_scene_plan，假 curl 产出 PNG，节点以 finish 结束，provider 常规循环不被调用。
- supervisor 进程：load_single_plugin 只挂路由；PluginRoutesFace.attach 到 FastAPI 后，
  面板静态页与管理 API 都在 /v1/plugins/draw/ 下可达。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from engine.context import EngineContext
from engine.faces.prompt_sections import PromptSectionRegistry
from engine.faces.tool_visibility import ToolVisibilityRegistry
from engine.hooks import HookRegistry, hook_registry
from engine.hooks.loader import load_single_plugin
from engine.inference.ai_step import run_ai_node
from engine.node import Node, ToolAccess
from engine.protocol import ACTION_FINISH
from providers.base import BaseProvider, ProviderResponse, ToolCall
from toolbox.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_ID = "draw.novelai_planner"

FAKE_CURL = r"""
import sys, zipfile
args = sys.argv[1:]
out = args[args.index("-o") + 1]
with zipfile.ZipFile(out, "w") as zf:
    zf.writestr("image_0.png", b"\x89PNG\r\n\x1a\n" + b"fake")
sys.stdout.write("200")
"""
PLAN = {"images": [{"index": 1, "title": "窗边", "orientation": "portrait", "scene": "indoors",
                    "characters": [{"name": "小艾", "costume": "dress", "action": "smile"}]}]}


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload, self.status_code = payload, status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Http:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append((url, kwargs.get("json") or {}))
        return _Response({"safety_level": "allow"})

    async def get(self, url: str, **kwargs: Any) -> _Response:
        return _Response({"cancelled": False})


class _Provider(BaseProvider):
    def __init__(self) -> None:
        super().__init__(model="fake", name="fake")
        self.calls: list[dict[str, Any]] = []

    async def chat(self, *, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        return ProviderResponse(ok=True, text="", usage={"total_tokens": 7},
                                tool_calls=[ToolCall(id="c1", name="submit_scene_plan", arguments=PLAN)])

    async def chat_stream(self, *, messages, tools=None, **kw):
        return await self.chat(messages=messages, tools=tools)


def _workspace(tmp_path: Path, monkeypatch) -> Path:
    draw = tmp_path / "tools" / "drawtools"
    (draw / "prompts" / "novelai").mkdir(parents=True)
    (draw / "settings.yaml").write_text(yaml.safe_dump({
        "api": {"api_key": "k", "timeout_sec": 5},
        "generation": {"retry_wait_sec": 0, "retry_max_attempts": 0, "lock_retry_max_attempts": 0,
                       "lock_retry_wait_min_sec": 0, "lock_retry_wait_max_sec": 0},
        "params": {"selected_preset_id": "p1", "presets": [{"id": "p1", "name": "P", "positive_prefix": "best quality",
                                                             "negative_prefix": "lowres", "max_images": 0,
                                                             "max_characters_per_image": 4,
                                                             "params": {"model": "nai-diffusion-4-5-full"}}]},
    }, allow_unicode=True), encoding="utf-8")
    (draw / "character_tags.yaml").write_text(yaml.safe_dump({"characters": [
        {"name": "艾丽丝", "aliases": ["小艾"], "type": "girl", "danbooru": "alice", "appearance": "blonde hair"}]},
        allow_unicode=True), encoding="utf-8")
    for name in ("top-system", "tag-guide", "scene-rules"):
        (draw / "prompts" / "novelai" / f"{name}.md").write_text(f"TPL-{name}", encoding="utf-8")
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    fake = tmp_path / "fake_curl.py"
    fake.write_text(FAKE_CURL, encoding="utf-8")
    monkeypatch.setenv("NOVELAI_CURL_BIN", json.dumps([sys.executable, str(fake)]))
    return tmp_path


def _rctx(root: Path, http: Any) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_root=root, workspace=None, workspace_name="", supervisor_url="http://supervisor.test",
        session_id="s1", parent_session_id="", worker_id="w1", session_generation=1, task_id="t1", task_context={},
        child_session_id="", conversation_store=None, tool_call_log=[], total_usage={}, response_info={},
        first_shadow_message_id="", last_shadow_message_id="", completed_steps=0, current_llm_request_id="",
        begin_llm_request=lambda: "req1", http=http,
        check_cancelled=lambda: asyncio.sleep(0, result=False),
        check_preempted=lambda: asyncio.sleep(0, result={"preempted": False, "message": "", "attachments": []}),
        consume_preempt=lambda *a, **kw: asyncio.sleep(0), emit_event=lambda *a, **kw: asyncio.sleep(0),
    )


@pytest.fixture
def engine_plugin(tmp_path: Path):
    """经真实加载器把 draw 插件装进全局 hook_registry（engine 进程），用完卸载并还原 sys.path/tools 模块。"""
    saved_path = list(sys.path)
    saved_tools = {k: v for k, v in sys.modules.items() if k == "tools" or k.startswith("tools.")}
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    ctx = EngineContext(hooks=hook_registry)
    ctx.contributions.mount("tool_visibility", ToolVisibilityRegistry())
    ctx.contributions.mount("prompt_sections", PromptSectionRegistry())
    ctx.contributions.mount("tools", registry)
    load_single_plugin(hook_registry, REPO_ROOT / "plugins", "draw", context=ctx, process="engine")
    try:
        yield registry
    finally:
        hook_registry.unload_plugin("draw")
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            sys.modules.pop(k, None)
        sys.modules.update(saved_tools)


def test_engine_node_is_taken_over_by_draw_agent(tmp_path: Path, monkeypatch, engine_plugin: ToolRegistry) -> None:
    root = _workspace(tmp_path, monkeypatch)
    assert "draw.agent_entry" in hook_registry.list_hooks().get("before_prompt_build", [])
    http = _Http()
    rctx = _rctx(root, http)
    provider = _Provider()
    node = Node(id=NODE_ID, type="ai", output_mode="hybrid", tool_access=ToolAccess(mode="allowlist", allow=["search_web"]))

    action = asyncio.run(run_ai_node(
        rctx=rctx, node=node, provider=provider, registry=engine_plugin,
        instruction="画小艾在窗边", history=[{"role": "user", "content": "之前聊过的"}],
    ))

    assert action.action == ACTION_FINISH
    assert "已生成 1 张图片" in action.to_dict()["result"]["text"]
    # provider 只被 agent 调用一次（规划），引擎常规循环没有再调
    assert len(provider.calls) == 1
    llm_text = "\n".join(str(m.get("content") or "") for m in provider.calls[0]["messages"])
    assert "TPL-top-system" in llm_text and "TPL-tag-guide" in llm_text and "TPL-scene-rules" in llm_text
    assert "艾丽丝" in llm_text and "画小艾在窗边" in llm_text and "之前聊过的" in llm_text
    assert [t["function"]["name"] for t in provider.calls[0]["tools"]] == ["submit_scene_plan"]
    # 图片经 Supervisor 快通道直发；文件真实落盘
    sends = [p for url, p in http.posts if url.endswith("/v1/sessions/s1/async_tool_result")]
    assert len(sends) == 1 and sends[0]["success"] is True and sends[0]["node_id"] == NODE_ID
    assert (root / sends[0]["attachment_paths"][0]).read_bytes().startswith(b"\x89PNG")
    assert rctx.total_usage.get("total_tokens") == 7


def test_supervisor_process_mounts_panel_and_api(tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from engine.faces.routes import PluginRoutesFace

    root = _workspace(tmp_path, monkeypatch)
    hooks = HookRegistry()
    ctx = EngineContext(hooks=hooks)
    face = PluginRoutesFace()
    ctx.contributions.mount("routes", face)
    meta = load_single_plugin(hooks, REPO_ROOT / "plugins", "draw", context=ctx, process="supervisor")
    assert meta["web"]["panels"][0]["entry"] == "/v1/plugins/draw/web/"
    # supervisor 进程不注册引擎钩子
    assert not hooks.list_hooks().get("before_prompt_build")

    app = FastAPI()
    face.attach(app, workspace_root=root)
    client = TestClient(app)
    page = client.get("/v1/plugins/draw/web/")
    assert page.status_code == 200 and "layout-editor.js" in page.text and 'id="view-prompts"' in page.text
    for asset in ("app.js", "style.css", "layout-editor.js", "layout-editor.css"):
        assert client.get(f"/v1/plugins/draw/web/{asset}").status_code == 200
    assert client.get("/v1/plugins/draw/web/../__init__.py").status_code in (403, 404)

    settings = client.get("/v1/plugins/draw/settings").json()["data"]
    assert settings["api_key_configured"] is True and settings["api"]["api_key"] == ""
    assert client.get("/v1/plugins/draw/characters").json()["data"]["characters"][0]["name"] == "艾丽丝"
    assert client.get("/v1/plugins/draw/prompts").json()["data"]["guide"]["content"] == "TPL-tag-guide"
    chain = client.post("/v1/plugins/draw/preview", json={}).json()["data"]["chain"]
    assert chain[-1]["role"] == "tools"
    dry = client.post("/v1/plugins/draw/test", json={"scene": "indoors", "dry_run": True,
                                                      "characters": [{"name": "小艾", "costume": "dress"}]}).json()["data"]
    assert dry["character_prompts"][0]["prompt"].startswith("girl, alice, blonde hair, dress")
    # 生成一张 → 图片列表 → 文件端点 → 删除
    gen = client.post("/v1/plugins/draw/test", json={"scene": "indoors"}).json()["data"]
    name = gen["path"].rsplit("/", 1)[-1]
    assert client.get("/v1/plugins/draw/images").json()["data"]["count"] == 1
    assert client.get(f"/v1/plugins/draw/images/file/{name}").content.startswith(b"\x89PNG")
    assert client.get("/v1/plugins/draw/images/file/..%2F..%2Fx.png").status_code == 404
    assert client.post("/v1/plugins/draw/images/delete", json={"names": [name]}).json()["data"]["deleted"] == [name]
