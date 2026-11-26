"""独立绘图 agent 依赖的引擎/Supervisor 接入点。

绘图 agent（plugins/draw/agent）不走引擎的推理循环：它在 before_prompt_build
钩子里接管 draw.novelai_planner，自己调模型和工具，然后直接返回完成结果。
这里固定它依赖的四个接入点，引擎改动破坏其中任何一个时这些用例会先失败：

1. 钩子返回的 finish 动作直接结束节点，provider 不被调用，附件原样带出。
2. 带 resume_data 重新进入节点时钩子仍会触发，并能从 extra 识别是恢复。
3. 插件可用 RunContext 构造 ToolContext，经工具注册表执行真实工具。
4. Supervisor 把 finish 结果里的附件作为 outbound_message 发出。
另外记录一个现状：任务完成时尚未消费的打断消息不会被 Supervisor 处理。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.hooks import HookResult, hook_registry
from engine.inference.ai_step import run_ai_node
from engine.node import Node
from engine.protocol import ACTION_FINISH, TaskAction
from providers.base import BaseProvider, ProviderResponse
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry

DRAW_NODE_ID = "draw.novelai_planner"
IMAGE = {"path": "data/attachments/novelai/nai_test.png", "type": "image", "name": "nai_test.png"}


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Http:
    """Supervisor stub: policy allows every operation, nothing is cancelled."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append(url)
        return _Response({"safety_level": "allow"})

    async def get(self, url: str, **kwargs: Any) -> _Response:
        return _Response({"cancelled": False})


class _ForbiddenProvider(BaseProvider):
    """The engine loop must not reach the provider once the agent took over."""

    def __init__(self) -> None:
        super().__init__(model="test-model", name="forbidden")
        self.calls = 0

    async def chat(self, *, messages, tools=None):
        self.calls += 1
        return ProviderResponse(ok=True, text="engine loop ran")

    async def chat_stream(self, *, messages, tools=None, **kw):
        return await self.chat(messages=messages, tools=tools)


def _rctx(tmp_path: Path, http: Any) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_root=tmp_path,
        workspace=None,
        workspace_name="",
        supervisor_url="http://supervisor.test",
        session_id="s1",
        parent_session_id="",
        worker_id="w1",
        session_generation=1,
        task_id="t1",
        task_context={},
        child_session_id="",
        conversation_store=None,
        tool_call_log=[],
        total_usage={},
        response_info={},
        first_shadow_message_id="",
        last_shadow_message_id="",
        completed_steps=0,
        current_llm_request_id="",
        begin_llm_request=lambda: "req1",
        http=http,
        check_cancelled=lambda: asyncio.sleep(0, result=False),
        check_preempted=lambda: asyncio.sleep(0, result={"preempted": False, "message": "", "attachments": []}),
        consume_preempt=lambda *a, **kw: asyncio.sleep(0),
        emit_event=lambda *a, **kw: asyncio.sleep(0),
    )


class _TakeoverProbe:
    """Stand-in for the draw agent entry hook."""

    name = "draw_agent_seam_probe"
    priority = 1000

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.seen: list[dict[str, Any]] = []

    async def handle(self, ctx: Any) -> HookResult | None:
        if getattr(ctx.node, "id", "") != DRAW_NODE_ID:
            return None
        self.seen.append({
            "is_resume": bool(ctx.extra.get("is_resume")),
            "instruction": ctx.extra.get("instruction_text"),
            "history": list(ctx.extra.get("history") or []),
            "provider": ctx.provider,
        })
        rctx = ctx.rctx
        tool_ctx = ToolContext(
            supervisor_url=rctx.supervisor_url,
            session_id=rctx.session_id,
            run_id=rctx.task_id,
            worker_id=rctx.worker_id,
            workspace_root=rctx.workspace_root,
            http=rctx.http,
            registry=self.registry,
            task_id=rctx.task_id,
            session_generation=rctx.session_generation,
            parent_session_id=rctx.parent_session_id,
            node_id=ctx.node.id,
        )
        tool_result = await self.registry.execute(name="probe_tool", arguments={"q": "x"}, ctx=tool_ctx)
        rctx.total_usage["total_tokens"] = rctx.total_usage.get("total_tokens", 0) + 42
        return HookResult(action=TaskAction(
            action=ACTION_FINISH,
            node_id=ctx.node.id,
            result={"text": f"done:{tool_result['data']['echo']}", "attachments": [dict(IMAGE)]},
        ))


@pytest.fixture
def probe(tmp_path: Path):
    # ToolRegistry 会把 tmp_path 插进 sys.path 并可能从那里导入 `tools` 包，
    # 用完必须还原，否则后续用例导入仓库里的 tools.* 会失败。
    saved_path = list(sys.path)
    saved_tools_modules = {k: v for k, v in sys.modules.items() if k == "tools" or k.startswith("tools.")}
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")

    async def _probe_tool(arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "data": {"echo": arguments.get("q")}}

    registry.register_builtin_tool(
        "probe_tool", "probe", {"type": "object", "properties": {"q": {"type": "string"}}}, _probe_tool,
    )
    handler = _TakeoverProbe(registry)
    dispose = hook_registry.register("before_prompt_build", handler)
    try:
        yield handler
    finally:
        dispose()
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            sys.modules.pop(k, None)
        sys.modules.update(saved_tools_modules)


def _run_node(tmp_path: Path, probe: _TakeoverProbe, **kwargs: Any):
    http = _Http()
    rctx = _rctx(tmp_path, http)
    provider = _ForbiddenProvider()
    node = Node(id=DRAW_NODE_ID, type="ai", output_mode="hybrid")
    action = asyncio.run(run_ai_node(
        rctx=rctx, node=node, provider=provider, registry=probe.registry,
        instruction="画一张初音未来", history=[{"role": "user", "content": "之前的对话"}], **kwargs,
    ))
    return action, rctx, provider, http


def test_hook_finish_ends_node_without_engine_loop(tmp_path: Path, probe: _TakeoverProbe) -> None:
    action, rctx, provider, http = _run_node(tmp_path, probe)

    assert action.action == ACTION_FINISH
    assert provider.calls == 0
    assert action.to_dict()["result"] == {"text": "done:x", "attachments": [IMAGE]}
    # 钩子里拿得到节点解析出的模型渠道、原始请求和会话历史。
    assert probe.seen[0]["provider"] is provider
    assert probe.seen[0]["instruction"] == "画一张初音未来"
    assert probe.seen[0]["history"] == [{"role": "user", "content": "之前的对话"}]
    assert probe.seen[0]["is_resume"] is False
    # 用量记在 RunContext 上，runner 在节点返回后从这里读取写入 TaskRecord。
    assert rctx.total_usage == {"total_tokens": 42}
    # 真实工具经注册表执行，并走了 Supervisor 的策略检查。
    assert any(url.endswith("/v1/ops/request") for url in http.posts)


def test_hook_fires_again_when_node_is_resumed(tmp_path: Path, probe: _TakeoverProbe) -> None:
    action, _rctx_obj, provider, _http = _run_node(
        tmp_path, probe,
        resume_data={"type": "child_result", "child_node_id": "x", "result": {"text": "r"}},
    )

    assert action.action == ACTION_FINISH
    assert provider.calls == 0
    assert probe.seen[0]["is_resume"] is True


def _supervisor_state(tmp_path: Path):
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState

    return SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "events.jsonl", run_id="r"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


def _entry_task(state: Any, session_id: str):
    from supervisor.types import TaskKind

    with state._lock:
        return state._create_task_locked(
            session_id=session_id,
            session_generation=state._current_session_generation_locked(session_id) or 1,
            kind=TaskKind.node,
            node_id=DRAW_NODE_ID,
            input_data={"instruction": "画一张初音未来"},
        )


def _finish_result() -> dict[str, Any]:
    return TaskAction(
        action=ACTION_FINISH, node_id=DRAW_NODE_ID,
        result={"text": "已生成 1 张", "attachments": [dict(IMAGE)]},
    ).to_dict()


def test_supervisor_delivers_finish_attachments_to_user(tmp_path: Path) -> None:
    state = _supervisor_state(tmp_path)
    sid = state.get_or_create_session(channel="test", conversation_key="test:draw")
    task = _entry_task(state, sid)

    state.complete_task(task_id=task.task_id, worker_id="w1", result=_finish_result())

    outbound = [e for e in state.eventlog.list_events(session_id=sid) if e.get("type") == "outbound_message"]
    assert len(outbound) == 1
    payload = outbound[0]["payload"]
    assert payload["text"] == "已生成 1 张"
    assert payload["attachments"] == [IMAGE]
    assert payload["node_id"] == DRAW_NODE_ID


def test_unconsumed_preempt_message_is_left_on_finished_task(tmp_path: Path) -> None:
    """现状记录：agent 必须自己轮询并消费打断消息，否则用户追加的话会丢。"""
    from supervisor.types import TaskStatus

    state = _supervisor_state(tmp_path)
    sid = state.get_or_create_session(channel="test", conversation_key="test:draw-preempt")
    task = _entry_task(state, sid)
    task.status = TaskStatus.running
    assert state.preempt_task(task.task_id, message="改成横图")

    state.complete_task(task_id=task.task_id, worker_id="w1", result=_finish_result())

    assert task.status == TaskStatus.completed
    assert task.preempt_message == "改成横图"
    # 消息只留在已结束任务的快照字段里：没有后续任务接手，也没有发给用户。
    assert [t.task_id for t in state.tasks.values()] == [task.task_id]
    outbound = [e for e in state.eventlog.list_events(session_id=sid) if e.get("type") == "outbound_message"]
    assert len(outbound) == 1
    assert "改成横图" not in str(outbound[0]["payload"])
