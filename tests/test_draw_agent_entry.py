"""plugins/draw/agent/entry：钩子接管 draw.novelai_planner 的端到端离线测试（假 provider + 假 curl）。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from engine.protocol import ACTION_FINISH
from plugins.draw.agent import entry as entry_mod
from plugins.draw.agent.entry import DrawAgentEntry
from providers.base import ProviderResponse, ToolCall

FAKE_CURL_ZIP = r"""
import sys, zipfile
args = sys.argv[1:]
out = args[args.index("-o") + 1]
with zipfile.ZipFile(out, "w") as zf:
    zf.writestr("image_0.png", b"\x89PNG\r\n\x1a\n" + b"fake")
sys.stdout.write("200")
"""

PLAN = {"images": [
    {"index": 1, "title": "窗边", "orientation": "portrait", "scene": "indoors, window",
     "characters": [{"name": "小艾", "costume": "dress", "action": "smile"}]},
    {"index": 2, "title": "庭院", "orientation": "landscape", "scene": "outdoors, garden",
     "characters": [{"name": "路人", "appear": "red hair", "costume": "coat", "action": "standing"}]},
]}


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def chat(self, *, messages, tools=None):
        self.requests.append({"messages": messages, "tools": tools})
        return self.responses.pop(0)


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Any]] = []

    def append(self, session_id: str, msg: Any) -> None:
        self.rows.append((session_id, msg))


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kw):
        self.posts.append((url, kw.get("json") or {}))
        return SimpleNamespace(status_code=200)


def _workspace(tmp_path: Path, monkeypatch, *, api_key: str = "k", planner: dict | None = None,
               agent: dict | None = None) -> Path:
    draw = tmp_path / "tools" / "drawtools"
    (draw / "prompts" / "novelai").mkdir(parents=True)
    settings = {
        "api": {"api_key": api_key, "timeout_sec": 5},
        "generation": {"retry_wait_sec": 0, "retry_max_attempts": 0, "lock_retry_max_attempts": 0,
                       "lock_retry_wait_min_sec": 0, "lock_retry_wait_max_sec": 0},
        "params": {"selected_preset_id": "p1", "presets": [{"id": "p1", "name": "P", "positive_prefix": "best quality",
                                                             "negative_prefix": "lowres", "max_images": 0,
                                                             "max_characters_per_image": 4,
                                                             "params": {"model": "nai-diffusion-4-5-full"}}]},
        "planner": {"tool_protocol": "native", "max_plan_images": 4, **(planner or {})},
    }
    if agent is not None:
        settings["agent"] = agent
    (draw / "settings.yaml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    (draw / "character_tags.yaml").write_text(yaml.safe_dump({"characters": [
        {"name": "艾丽丝", "aliases": ["小艾"], "type": "girl", "danbooru": "alice", "appearance": "blonde hair"},
    ]}, allow_unicode=True), encoding="utf-8")
    for name, text in (("top-system", "OPENING"), ("tag-guide", "GUIDE"), ("scene-rules", "RULES")):
        (draw / "prompts" / "novelai" / f"{name}.md").write_text(text, encoding="utf-8")
    fake = tmp_path / "fake_curl.py"
    fake.write_text(FAKE_CURL_ZIP, encoding="utf-8")
    monkeypatch.setenv("NOVELAI_CURL_BIN", json.dumps([sys.executable, str(fake)]))
    return tmp_path


def _rctx(root: Path, http: FakeHttp, store: FakeStore | None = None):
    async def no_preempt():
        return {"preempted": False, "message": "", "attachments": []}

    async def noop(*a, **kw):
        return None

    async def not_cancelled():
        return False

    return SimpleNamespace(
        workspace_root=root, supervisor_url="http://sup.test", session_id="s1", parent_session_id="",
        worker_id="w1", session_generation=1, task_id="t1", child_session_id="", conversation_store=store,
        http=http, total_usage={}, response_info={}, first_shadow_message_id="", last_shadow_message_id="",
        begin_llm_request=lambda: "req1", check_cancelled=not_cancelled, check_preempted=no_preempt,
        consume_preempt=noop, emit_event=noop,
    )


def _ctx(root: Path, provider: Any, http: FakeHttp, store: FakeStore | None = None, *, node_id: str = "draw.novelai_planner",
         instruction: str = "画小艾在窗边", history: list | None = None):
    node = SimpleNamespace(id=node_id, tool_access=SimpleNamespace(mode="allowlist", allow=["search_web"]))
    return SimpleNamespace(node=node, provider=provider, rctx=_rctx(root, http, store), messages=[], tools=[],
                           extra={"instruction_text": instruction, "history": history or [], "is_resume": False})


def _plan_response() -> ProviderResponse:
    return ProviderResponse(ok=True, text="", tool_calls=[ToolCall(id="c1", name="submit_scene_plan", arguments=PLAN)],
                            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def test_entry_plans_generates_sends_and_finishes(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    provider = FakeProvider([_plan_response()])
    http, store = FakeHttp(), FakeStore()
    result = asyncio.run(DrawAgentEntry(registry=None).handle(_ctx(root, provider, http, store)))

    assert result.action.action == ACTION_FINISH
    text = result.action.result["text"]
    assert "已生成 2 张图片" in text and "窗边" in text and "庭院" in text
    # 每张图经 Supervisor 快通道直发，finish 不再带附件（避免重复发送）
    sends = [p for url, p in http.posts if url.endswith("/v1/sessions/s1/async_tool_result")]
    assert len(sends) == 2 and all(p["success"] is True and p["node_id"] == "draw.novelai_planner" for p in sends)
    assert all((root / p["attachment_paths"][0]).exists() for p in sends)
    assert "attachments" not in result.action.result
    # 消息链：模型看到的 system 含模板文本与角色信息，没有 tools 标记残留
    llm_messages = provider.requests[0]["messages"]
    joined = "\n".join(str(m.get("content") or "") for m in llm_messages)
    assert "OPENING" in joined and "GUIDE" in joined and "RULES" in joined and "艾丽丝" in joined
    assert "CLONOTH_DRAW_TOOL_PROTOCOL" not in joined and "画小艾在窗边" in joined
    assert [t["function"]["name"] for t in provider.requests[0]["tools"]] == ["submit_scene_plan"]
    # 历史：assistant 规划调用 + 两条生图 tool 结果 + 最终 assistant 文本
    roles = [m.role for _, m in store.rows]
    assert roles.count("tool") >= 2 and roles[-1] == "assistant"
    assert any(m.tool_calls and m.tool_calls[0]["function"]["name"] == "submit_scene_plan" for _, m in store.rows)


def _no_sleep(monkeypatch) -> list[float]:
    """重试前的等待换成只记录秒数。"""
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(entry_mod, "_sleep", record)
    return delays


def _failed(status: int) -> ProviderResponse:
    return ProviderResponse(ok=False, error="upstream boom", status_code=status)


def _no_tool_call() -> ProviderResponse:
    return ProviderResponse(ok=True, text="我不会用工具", tool_calls=[], usage={})


def _handle(root: Path, provider: Any, http: FakeHttp | None = None, **kw: Any):
    return asyncio.run(DrawAgentEntry(registry=None).handle(_ctx(root, provider, http or FakeHttp(), **kw)))


def test_entry_reports_planner_failure_without_generating(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch, planner={"max_attempts": 1}, agent={"planner_max_retries": 0})
    delays = _no_sleep(monkeypatch)
    provider = FakeProvider([_no_tool_call()])
    http = FakeHttp()
    result = _handle(root, provider, http)
    assert result.action.action == ACTION_FINISH
    text = result.action.result["text"]
    assert text.startswith("场景规划失败：") and "已自动重试" not in text
    assert not http.posts and len(provider.requests) == 1 and delays == []


def test_entry_retries_whole_planning_after_provider_failure(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    delays = _no_sleep(monkeypatch)
    provider = FakeProvider([_failed(500), _plan_response()])
    result = _handle(root, provider)
    assert "已生成 2 张图片" in result.action.result["text"] and len(provider.requests) == 2
    assert len(delays) == 1 and 1.6 <= delays[0] <= 2.4


def test_entry_does_not_retry_request_errors(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    delays = _no_sleep(monkeypatch)
    provider = FakeProvider([_failed(401), _plan_response()])
    text = _handle(root, provider).action.result["text"]
    assert text.startswith("场景规划失败：") and "已自动重试" not in text
    assert len(provider.requests) == 1 and delays == []


def test_entry_gives_up_after_configured_retries(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch, agent={"planner_max_retries": 2})
    delays = _no_sleep(monkeypatch)
    provider = FakeProvider([_failed(503)] * 5)
    http = FakeHttp()
    text = _handle(root, provider, http).action.result["text"]
    assert text.startswith("场景规划失败：") and text.endswith("（已自动重试 2 次）")
    assert len(provider.requests) == 3 and len(delays) == 2 and delays[1] > delays[0]
    assert not http.posts


def test_entry_retry_after_tool_format_failure_adds_one_correction(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch, planner={"max_attempts": 1}, agent={"planner_max_retries": 1})
    _no_sleep(monkeypatch)
    provider = FakeProvider([_no_tool_call(), _plan_response()])
    result = _handle(root, provider)
    assert "已生成 2 张图片" in result.action.result["text"]
    first, second = ("\n".join(str(m.get("content") or "") for m in r["messages"]) for r in provider.requests)
    marker = "上一轮没有交出有效的工具调用"
    assert marker not in first and second.count(marker) == 1
    assert provider.requests[1]["messages"][-1]["role"] == "user"


def _spy_runtime(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    real_run = entry_mod.PlannerRuntime.run

    async def run(self, request):
        seen.update(timeout=request.timeout_sec, protocol=self.protocol, provider=self.provider)
        return await real_run(self, request)

    monkeypatch.setattr(entry_mod.PlannerRuntime, "run", run)
    return seen


def test_entry_uses_agent_preset_model_when_configured(tmp_path: Path, monkeypatch):
    preset = {"id": "a1", "name": "主力", "channel": "relay", "model": "gpt-x", "timeout_sec": 77}
    root = _workspace(tmp_path, monkeypatch, planner={"tool_protocol": "text", "timeout_sec": 5},
                      agent={"current_preset_id": "a1", "presets": [{"id": "a0", "name": "别的"}, preset]})
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "config.yaml").write_text(yaml.safe_dump({"active_channel": "relay", "channels": [
        {"id": "relay", "name": "中转", "type": "openai", "base_url": "https://relay.test/v1", "api_key": "sk-test", "model": "m0"},
    ]}), encoding="utf-8")
    seen = _spy_runtime(monkeypatch)
    created: list[tuple[dict, Any]] = []
    planner_provider = FakeProvider([_plan_response()])

    def create(agent_preset, *, http):
        created.append((dict(agent_preset), http))
        return planner_provider

    monkeypatch.setattr(entry_mod.llm, "create_provider", create)
    node_provider = FakeProvider([])
    ctx = _ctx(root, node_provider, FakeHttp())
    ctx.rctx.llm_http = object()
    result = asyncio.run(DrawAgentEntry(registry=None).handle(ctx))

    assert "已生成 2 张图片" in result.action.result["text"]
    assert node_provider.requests == [] and len(planner_provider.requests) == 1
    assert len(created) == 1 and created[0][1] is ctx.rctx.llm_http
    assert (created[0][0]["id"], created[0][0]["model"], created[0][0]["api_key"]) == ("a1", "gpt-x", "sk-test")
    assert created[0][0]["provider"] == "openai" and planner_provider.channel_id == "relay"
    # 工具调用格式与超时跟着预设走，不再读 planner 段里的旧值
    assert seen["timeout"] == 77.0 and type(seen["protocol"]) is type(entry_mod.create_tool_protocol("native"))


def test_entry_without_presets_keeps_node_model_and_legacy_planner_values(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch, planner={"tool_protocol": "text", "timeout_sec": 42},
                      agent={"planner_max_retries": 0})
    seen = _spy_runtime(monkeypatch)
    node_provider = FakeProvider([_no_tool_call()])
    _handle(root, node_provider)
    assert seen["provider"] is node_provider and seen["timeout"] == 42.0
    assert type(seen["protocol"]) is type(entry_mod.create_tool_protocol("text"))


def test_entry_reports_unusable_agent_preset_without_retrying(tmp_path: Path, monkeypatch):
    preset = {"id": "a1", "name": "坏预设", "channel": "no-such-channel", "model": "m"}
    root = _workspace(tmp_path, monkeypatch, agent={"presets": [preset]})
    delays = _no_sleep(monkeypatch)
    node_provider = FakeProvider([_plan_response()])
    text = _handle(root, node_provider).action.result["text"]
    assert text.startswith("场景规划失败：场景 Agent 预设「坏预设」无法使用") and "渠道方案不存在" in text
    assert node_provider.requests == [] and delays == []


def test_entry_missing_api_key_reports_each_failure(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch, api_key="")
    monkeypatch.delenv("NOVELAI_API_KEY", raising=False)
    monkeypatch.setattr(entry_mod.novelai, "generate_image", _raise_missing_key)
    result = asyncio.run(DrawAgentEntry(registry=None).handle(_ctx(root, FakeProvider([_plan_response()]), FakeHttp())))
    text = result.action.result["text"]
    assert "2 张失败" in text and "API key is missing" in text


async def _raise_missing_key(*a, **kw):
    raise entry_mod.novelai.NovelAIError("NovelAI API key is missing.", code="API_KEY_MISSING")


def test_entry_image_size_follows_the_preset(tmp_path: Path, monkeypatch):
    """画幅跟着预设走；预设没写画幅时沿用旧的全局 planner.override_size。PLAN 是一张竖图加一张横图。"""
    root = _workspace(tmp_path, monkeypatch, planner={"override_size": "1024x1024"})
    path = root / "tools" / "drawtools" / "settings.yaml"
    seen: list[tuple[int, int]] = []

    async def capture(compiled, **kw):
        seen.append((compiled["width"], compiled["height"]))
        raise entry_mod.novelai.NovelAIError("stop here", code="TEST_STOP")

    monkeypatch.setattr(entry_mod.novelai, "generate_image", capture)

    def sizes(**preset_patch) -> list[tuple[int, int]]:
        settings = yaml.safe_load(path.read_text(encoding="utf-8"))
        preset = settings["params"]["presets"][0]
        preset["params"].update(preset_patch.pop("params", {}))
        preset.update(preset_patch)
        path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
        seen.clear()
        asyncio.run(DrawAgentEntry(registry=None).handle(_ctx(root, FakeProvider([_plan_response()]), FakeHttp())))
        return list(seen)

    assert sizes() == [(1024, 1024), (1024, 1024)]
    assert sizes(override_size="auto") == [(832, 1216), (1216, 832)]
    assert sizes(override_size="default", params={"width": 768, "height": 1280}) == [(768, 1280), (768, 1280)]


def test_entry_ignores_other_nodes_and_empty_request(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    agent = DrawAgentEntry(registry=None)
    assert asyncio.run(agent.handle(_ctx(root, FakeProvider([]), FakeHttp(), node_id="main"))) is None
    result = asyncio.run(agent.handle(_ctx(root, FakeProvider([]), FakeHttp(), instruction="")))
    assert result.action.action == ACTION_FINISH and "没有收到绘图请求" in result.action.result["text"]


def test_entry_falls_back_to_finish_attachments_when_direct_send_fails(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)

    class FailingHttp(FakeHttp):
        async def post(self, url: str, **kw):
            raise RuntimeError("supervisor down")

    result = asyncio.run(DrawAgentEntry(registry=None).handle(_ctx(root, FakeProvider([_plan_response()]), FailingHttp())))
    atts = result.action.result["attachments"]
    assert len(atts) == 2 and all(a["type"] == "image" and a["path"].startswith("data/attachments/novelai/") for a in atts)


def test_entry_sends_images_to_parent_session_when_dispatched(tmp_path: Path, monkeypatch):
    """被 qq.orchestrator 等节点委派时，图片必须发往父会话：子会话键是 agent:… 开头，适配器投递不了。"""
    root = _workspace(tmp_path, monkeypatch)
    http = FakeHttp()
    ctx = _ctx(root, FakeProvider([_plan_response()]), http, FakeStore())
    ctx.rctx.task_context = {"dispatch_origin": {
        "parent_session_id": "parent1", "caller_node_id": "qq.orchestrator",
        "parent_conversation_key": "qq_group:1", "context_mode": "fresh",
    }}
    result = asyncio.run(DrawAgentEntry(registry=None).handle(ctx))

    urls = [url for url, _ in http.posts if url.endswith("/async_tool_result")]
    assert urls == ["http://sup.test/v1/sessions/parent1/async_tool_result"] * 2
    assert "attachments" not in result.action.result


def test_entry_delivery_session_falls_back_to_own_session():
    pick = DrawAgentEntry._delivery_session_id
    assert pick(SimpleNamespace(session_id="s1")) == "s1"
    assert pick(SimpleNamespace(session_id="s1", task_context={})) == "s1"
    assert pick(SimpleNamespace(session_id="s1", task_context={"dispatch_origin": {"parent_session_id": "  "}})) == "s1"
    assert pick(SimpleNamespace(session_id="s1", task_context={"dispatch_origin": "bad"})) == "s1"
    assert pick(SimpleNamespace(session_id="s1", task_context={"dispatch_origin": {"parent_session_id": "p"}})) == "p"


