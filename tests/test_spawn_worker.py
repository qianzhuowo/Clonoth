"""Regression tests for the anonymous spawn_worker pseudo tool.

[AutoC 2026-09-12] Why: spawn_worker crosses tool composition, pseudo routing,
node loading, and runtime configuration. How: test those contracts directly with
small stubs and no Supervisor process. Purpose: future pseudo-tool refactors cannot
silently remove anonymous worker spawning or change its fresh/fork defaults.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from engine.context import RunContext

from engine.inference import pseudo_handlers
from engine.inference.pseudo_handlers import _handle_pseudo_tool
from engine.inference.pseudo_tools import build_node_tool_specs
from engine.node import Node, load_node


class _EmptyRegistry:
    """Provide the minimal ToolRegistry surface used by tool composition."""

    def list_specs(self) -> list[dict[str, Any]]:
        return []


def _tool_names(specs: list[dict[str, Any]]) -> set[str]:
    """Extract function names from OpenAI-format tool specs."""
    return {
        str((spec.get("function") or {}).get("name") or "")
        for spec in specs
        if isinstance(spec, dict)
    }


@pytest.mark.parametrize("has_active_children", [False, True])
def test_manage_workers_is_permanently_injected_and_replaces_preempt_and_ask(has_active_children):
    node = Node(id="parent", type="ai", delegate_targets=[])
    _, specs = build_node_tool_specs(registry=_EmptyRegistry(), node=node, has_active_children=has_active_children)
    names = _tool_names(specs)
    assert {"spawn_worker", "manage_workers"} <= names
    assert names.isdisjoint({"ask", "preempt_task"})
    spawn = next(s["function"] for s in specs if s["function"]["name"] == "spawn_worker")
    assert spawn["parameters"]["required"] == ["instruction"]
    assert spawn["parameters"]["properties"]["context_mode"]["default"] == "fork"


@pytest.mark.parametrize("allowed,expected", [
    (["ask"], set()),
    (["spawn_worker"], {"spawn_worker"}),
    (["manage_workers"], {"manage_workers"}),
])
def test_worker_controls_respect_explicit_allowlist(allowed, expected):
    node = Node(id="parent", type="ai", control_tools=allowed)
    names = _tool_names(build_node_tool_specs(registry=_EmptyRegistry(), node=node, has_active_children=True)[1])
    assert names & {"spawn_worker", "manage_workers"} == expected


def test_spawn_worker_rejects_empty_instruction(monkeypatch) -> None:
    """An empty instruction should emit a failure without dispatching."""
    emitted: list[dict[str, Any]] = []

    def fake_emit(ls: Any, pseudo_call: Any, content: str, **kwargs: Any) -> None:
        emitted.append(yaml.safe_load(content))

    async def forbidden_dispatch(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("empty instruction must not dispatch")

    monkeypatch.setattr(pseudo_handlers, "_emit_pseudo_tool_result", fake_emit)
    monkeypatch.setattr(pseudo_handlers, "_handle_pseudo_dispatch", forbidden_dispatch)
    pseudo_call = SimpleNamespace(id="spawn-empty", name="spawn_worker", arguments={"instruction": "  "})

    result = asyncio.run(_handle_pseudo_tool(SimpleNamespace(), pseudo_call, step=0))

    assert result is None
    assert emitted == [{"success": False, "error": "instruction is required"}]


def test_builtin_worker_template_is_ephemeral() -> None:
    """The built-in worker should be ephemeral and accept an instruction."""
    workspace_root = Path(__file__).resolve().parents[1]
    worker = load_node(workspace_root, "_worker")

    assert worker is not None
    assert worker.persistent is False
    assert worker.tool_access.mode == "all"
    assert "{{instruction}}" in str(worker.prompt)


def test_unified_dispatch_to_spec_and_dynamic_refresh(tmp_path: Path) -> None:
    """build_node_tool_specs emits a single dispatch_to tool whose enum and description refresh dynamically when nodes change."""
    from engine.inference.pseudo_tools import refresh_dispatch_to_spec, allowed_pseudo_names
    nodes_dir = tmp_path / "config" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "parent.yaml").write_text(
        "id: parent\ntype: ai\nname: Parent\ndelegate_targets:\n  - scout\n",
        encoding="utf-8",
    )
    (nodes_dir / "scout.yaml").write_text(
        "id: scout\ntype: ai\nname: Scout\ndescription: Code explorer\n",
        encoding="utf-8",
    )
    parent = load_node(tmp_path, "parent")
    assert parent is not None
    from engine.runner import _collect_node_info
    ds_info = _collect_node_info(tmp_path, list(parent.delegate_targets))
    _, specs = build_node_tool_specs(registry=_EmptyRegistry(), node=parent, downstream_info=ds_info)
    assert "dispatch_to" in _tool_names(specs)
    assert "dispatch_to_scout" not in _tool_names(specs)
    d_spec = next(s["function"] for s in specs if s["function"]["name"] == "dispatch_to")
    assert d_spec["parameters"]["properties"]["target"]["enum"] == ["scout"]
    assert "Scout" in d_spec["description"] and "Code explorer" in d_spec["description"]

    # Now dynamically add a new node 'smith' and update parent.yaml on disk
    (nodes_dir / "smith.yaml").write_text(
        "id: smith\ntype: ai\nname: Agent Smith\ndescription: Code writer\n",
        encoding="utf-8",
    )
    (nodes_dir / "parent.yaml").write_text(
        "id: parent\ntype: ai\nname: Parent\ndelegate_targets:\n  - scout\n  - smith\n",
        encoding="utf-8",
    )
    ls = SimpleNamespace(
        rctx=SimpleNamespace(workspace_root=tmp_path),
        node=parent,
        openai_tools=specs,
        allowed_pseudo_tools=allowed_pseudo_names(specs, parent),
    )
    changed = refresh_dispatch_to_spec(ls)
    assert changed is True
    d_spec_after = next(s["function"] for s in ls.openai_tools if s["function"]["name"] == "dispatch_to")
    assert d_spec_after["parameters"]["properties"]["target"]["enum"] == ["scout", "smith"]
    assert "Agent Smith" in d_spec_after["description"] and "Code writer" in d_spec_after["description"]
    # Calling refresh again without disk changes must return False (preserving cache)
    assert refresh_dispatch_to_spec(ls) is False

    # In tool_mode="json", ls.openai_tools is cleared to [] after inject_tool_definitions;
    # refresh_dispatch_to_spec must return False without appending native dispatch_to spec
    # or clobbering ls.allowed_pseudo_tools.
    parent.tool_mode = "json"
    json_ls = SimpleNamespace(
        rctx=SimpleNamespace(workspace_root=tmp_path),
        node=parent,
        openai_tools=[],
        allowed_pseudo_tools={"dispatch_to", "intermediate_reply", "spawn_worker", "manage_workers"},
    )
    assert refresh_dispatch_to_spec(json_ls) is False
    assert json_ls.openai_tools == []
    assert "intermediate_reply" in json_ls.allowed_pseudo_tools
    assert "spawn_worker" in json_ls.allowed_pseudo_tools


def test_spawn_worker_cache_warm_fork_persists_exact_parent_prefix(tmp_path: Path) -> None:
    """spawn_worker(context_mode='fork') saves the parent's exact pre-assistant messages and tools snapshot for 100% cache hit."""
    import json
    from engine.inference.tool_format import create_tool_formatter
    posts: list[dict[str, Any]] = []

    class _StubHttp:
        async def post(self, url: str, **kwargs: Any) -> Any:
            posts.append({"url": url, **kwargs})
            return SimpleNamespace(status_code=200, json=lambda: {"session_id": "child-fork-sid"}, text="OK")

    parent_messages = [
        {"role": "system", "content": "Parent exact system prompt"},
        {"role": "user", "content": "[SKILLS:CONSTANT]\nParent skills"},
        {"role": "user", "content": "Hello parent"},
        {"role": "assistant", "content": "Calling spawn_worker", "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "spawn_worker", "arguments": "{}"}}]},
    ]
    parent_tools = [{"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {}}}}]
    rctx = SimpleNamespace(
        workspace_root=tmp_path,
        supervisor_url="http://supervisor.test",
        session_id="branch_123",
        parent_session_id="main_parent_sid",
        sticky_session_id="main_parent_sid",
        task_id="task_parent",
        task_context={"conversation_key": "discord:123", "channel": "discord"},
        http=_StubHttp(),
        emit_event=lambda *a, **kw: asyncio.sleep(0),
        conversation_store=None,
    )
    ls = SimpleNamespace(
        rctx=rctx,
        node=Node(id="ereuna_main", type="ai"),
        messages=list(parent_messages),
        openai_tools=list(parent_tools),
        formatter=create_tool_formatter("native"),
        provider=SimpleNamespace(name="openai"),
    )
    call = SimpleNamespace(
        id="tc1",
        name="spawn_worker",
        arguments={"instruction": "Check file X", "context_mode": "fork"},
    )
    asyncio.run(_handle_pseudo_tool(ls, call, step=0))
    assert len(posts) == 1
    inbound_json = posts[0]["json"]
    origin = inbound_json["dispatch_origin"]
    assert origin["parent_sticky_session_id"] == "main_parent_sid"
    snap_rel = origin["fork_snapshot_path"]
    snap_data = json.loads((tmp_path / snap_rel).read_text(encoding="utf-8"))
    # Must contain the exact 3 messages prior to the current step's assistant tool_call message
    assert snap_data["messages"] == parent_messages[:3]
    assert snap_data["openai_tools"] == parent_tools


def test_run_ai_node_consumes_cache_warm_fork_snapshot(tmp_path: Path) -> None:
    """run_ai_node loads fork_snapshot_path, preserves exact parent prefix + tools, and unlinks snapshot."""
    import json
    from engine.inference.ai_step import run_ai_node
    from providers.base import BaseProvider, ProviderResponse
    from toolbox.registry import ToolRegistry

    snap_dir = tmp_path / "data" / "temp" / "fork_snapshots"
    snap_dir.mkdir(parents=True)
    snap_file = snap_dir / "fork_test123.json"
    parent_prefix = [
        {"role": "system", "content": "EXACT_PARENT_SYSTEM_PROMPT"},
        {"role": "user", "content": "EXACT_PARENT_HISTORY_TURN"},
    ]
    parent_tools = [
        {"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "switch_node", "description": "Switch", "parameters": {"type": "object", "properties": {}}}},
    ]
    snap_file.write_text(json.dumps({
        "messages": parent_prefix,
        "openai_tools": parent_tools,
        "sticky_session_id": "parent_sticky_1",
    }), encoding="utf-8")

    class _CaptureProvider(BaseProvider):
        def __init__(self) -> None:
            super().__init__(model="test-model", name="capture")
            self.seen_messages = None
            self.seen_tools = None

        async def chat(self, *, messages, tools=None):
            self.seen_messages = list(messages)
            self.seen_tools = list(tools or [])
            return ProviderResponse(ok=True, text="Worker finished analysis.")

        async def chat_stream(self, *, messages, tools=None, **kw):
            return await self.chat(messages=messages, tools=tools)

    rctx = SimpleNamespace(
        workspace_root=tmp_path,
        workspace=None,
        supervisor_url="http://127.0.0.1:0",
        session_id="child_fork_session",
        parent_session_id="parent_session",
        worker_id="w1",
        session_generation=1,
        task_id="worker_task_1",
        task_context={
            "dispatch_origin": {
                "fork_snapshot_path": "data/temp/fork_snapshots/fork_test123.json",
                "parent_sticky_session_id": "parent_sticky_1",
            }
        },
        child_session_id="",
        conversation_store=None,
        tool_call_log=[],
        total_usage={},
        first_shadow_message_id="",
        last_shadow_message_id="",
        completed_steps=0,
        current_llm_request_id="req1",
        begin_llm_request=lambda: "req1",
        http=SimpleNamespace(post=lambda *a, **kw: None, get=lambda *a, **kw: None),
        check_cancelled=lambda: asyncio.sleep(0, result=False),
        check_preempted=lambda: asyncio.sleep(0, result={"preempted": False, "message": "", "attachments": []}),
        consume_preempt=lambda *a, **kw: asyncio.sleep(0),
        emit_event=lambda *a, **kw: asyncio.sleep(0),
    )
    prov = _CaptureProvider()
    worker_node = Node(id="_worker", type="ai", output_mode="hybrid")
    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    assert "cancel_active_tasks" not in {s["name"] for s in reg.list_specs()}

    action = asyncio.run(run_ai_node(
        rctx=rctx,
        node=worker_node,
        provider=prov,
        registry=reg,
        instruction="Inspect module X",
        history=[],
    ))
    assert action.action == "finish"
    assert not snap_file.exists(), "snapshot file must be unlinked after consumption"
    assert prov.seen_messages[:2] == parent_prefix
    assert "Inspect module X" in prov.seen_messages[2]["content"]
    assert prov.seen_tools == parent_tools


class _Response:
    """Small HTTP response compatible with RunContext and pseudo handlers."""

    def __init__(self, payload: Any, status_code: int = 200, text: str = "OK") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return self._payload


class _ActionHttp:
    """Record manage_workers mutation requests."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append({"url": url, **kwargs})
        return _Response({"ok": True})


def _run_context(http: Any) -> RunContext:
    """Build a RunContext using one HTTP stub for both client fields."""
    return RunContext(
        workspace_root=Path("."),
        supervisor_url="http://supervisor.test",
        session_id="branch-current",
        parent_session_id="parent-session",
        worker_id="engine-worker",
        http=http,
        llm_http=http,
        task_id="parent-task",
    )


def _handler_state(http: Any, tasks: list[dict[str, Any]]) -> SimpleNamespace:
    """Build the minimal loop state consumed by manage_workers."""
    async def list_active_child_tasks() -> list[dict[str, Any]]:
        return list(tasks)

    rctx = SimpleNamespace(
        supervisor_url="http://supervisor.test",
        session_id="branch-current",
        parent_session_id="parent-session",
        task_id="parent-task",
        http=http,
        list_active_child_tasks=list_active_child_tasks,
    )
    return SimpleNamespace(rctx=rctx)


def _run_handler(monkeypatch: Any, args: dict[str, Any], *, tasks: list[dict[str, Any]], http: Any) -> dict[str, Any]:
    """Execute one manage_workers call and decode its emitted result."""
    emitted: list[str] = []

    def fake_emit(ls: Any, pseudo_call: Any, content: str, **kwargs: Any) -> None:
        emitted.append(content)

    monkeypatch.setattr(pseudo_handlers, "_emit_pseudo_tool_result", fake_emit)
    call = SimpleNamespace(id="manage-1", name="manage_workers", arguments=args)
    result = asyncio.run(_handle_pseudo_tool(_handler_state(http, tasks), call, step=0))

    assert result is None
    assert len(emitted) == 1
    return json.loads(emitted[0])


def test_run_context_lists_only_active_child_tasks_and_deduplicates() -> None:
    """Discovery should combine legacy and child-session tasks without user entries."""
    tasks = asyncio.run(_run_context(_DiscoveryHttp()).list_active_child_tasks())

    assert [task["task_id"] for task in tasks] == [
        "legacy-child-task-abcdef",
        "spawned-child-task-123456",
    ]
    assert tasks[1]["node_id"] == "_worker"
    assert tasks[1]["status"] == "awaiting_approval"


class _DiscoveryHttp:
    async def get(self, url, **kwargs):
        if url.endswith("/running_tasks"):
            return _Response({"tasks": [
                {"task_id": "parent-task", "node_id": "parent", "status": "running", "is_user_entry": True},
                {"task_id": "sibling-user-task", "node_id": "parent", "status": "running", "is_user_entry": True},
                {"task_id": "legacy-child-task-abcdef", "node_id": "scout", "status": "running", "is_user_entry": False},
            ]})
        if url.endswith("/children"):
            return _Response([
                {"task_id": "legacy-child-task-abcdef", "node_id": "scout", "status": "running", "session_id": "child-legacy"},
                {"task_id": "spawned-child-task-123456", "node_id": "_worker", "status": "awaiting_approval", "session_id": "child-spawned"},
                {"task_id": "completed-child-task", "node_id": "_worker", "status": "completed", "session_id": "child-completed"},
            ])
        raise AssertionError(f"unexpected GET {url}")


def test_manage_workers_list_formats_active_children(monkeypatch):
    tasks = [{"task_id": "abcdef123456-full", "node_id": "scout", "status": "running",
              "created_at": "2026-09-12T15:00:00+00:00"}]
    http = _ActionHttp()
    payload = _run_handler(monkeypatch, {"action": "list"}, tasks=tasks, http=http)
    assert payload["active_children"] == 1
    assert payload["tasks"][0]["task_id"] == "abcdef123456"
    assert payload["tasks"][0]["node_id"] == "scout"
    assert isinstance(payload["tasks"][0]["elapsed_sec"], int)
    assert not http.posts


@pytest.mark.parametrize("args,path,json_body,params", [
    ({"action": "message", "task_id": "abcdef123456", "message": "请补充验证"},
     "/tasks/abcdef123456-full/preempt", {"message": "请补充验证"}, None),
    ({"action": "preempt", "target": "scout"}, "/tasks/abcdef123456-full/preempt", None, None),
    ({"action": "cancel"}, "/sessions/parent-session/cancel_active_tasks", None, {"exclude_task_id": "parent-task"}),
    ({"action": "cancel", "node_id": "smith"}, "/sessions/parent-session/cancel_active_tasks", None,
     {"exclude_task_id": "parent-task", "node_id": "smith"}),
])
def test_manage_workers_routes_child_controls(monkeypatch, args, path, json_body, params):
    tasks = [{"task_id": "abcdef123456-full", "node_id": "scout", "status": "running"}]
    http = _ActionHttp()
    payload = _run_handler(monkeypatch, args, tasks=tasks, http=http)
    assert payload["success"] is True
    assert [(p["url"], p.get("json"), p.get("params")) for p in http.posts] == [
        ("http://supervisor.test/v1" + path, json_body, params),
    ]
