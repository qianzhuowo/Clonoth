"""Regression coverage for scheduler isolation and tool metadata/upserts."""
import asyncio
import re
import sys
from pathlib import Path
from typing import Any
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from supervisor import scheduler
from supervisor.task_router import TaskRouterMixin
from toolbox.builtins import schedules
from toolbox.builtins.apply_diff import apply_diff, _find_and_replace
from toolbox.builtins.read_file import _read_single_file_sync
from toolbox.builtins.search_in_files import _do_replace_sync
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry


def test_unique_diff_uses_actual_line_not_stale_hint():
    result, error, candidates, line = _find_and_replace("a\nb\nunique\n", "unique", "changed", 99)
    assert result == "a\nb\nchanged\n"
    assert error is None and candidates is None and line == 3


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_script_timeout_kills_process_tree_and_releases_running_slot(tmp_path, monkeypatch, platform):
    proc = Mock(pid=123)
    proc.communicate.side_effect = [subprocess.TimeoutExpired("command", 1), ("", "")]
    popen = Mock(return_value=proc)
    taskkill = Mock()
    killpg = Mock()
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    monkeypatch.setattr(scheduler.subprocess, "run", taskkill)
    monkeypatch.setattr(scheduler, "os", SimpleNamespace(name=platform, killpg=killpg))
    monkeypatch.setattr(scheduler, "signal", SimpleNamespace(SIGKILL=9))
    runner = scheduler.SchedulerThread(state=None, workspace_root=tmp_path)
    runner._inject_inbound = Mock()
    runner._fire_script({"command": "command", "timeout": 1}, "script")
    assert popen.call_args.kwargs["start_new_session"] is True
    if platform == "nt":
        assert taskkill.call_args.args[0] == ["taskkill", "/PID", "123", "/T", "/F"]
        killpg.assert_not_called()
    else:
        killpg.assert_called_once_with(123, 9)
        taskkill.assert_not_called()
    assert not runner._running_scripts
    runner._inject_inbound.assert_not_called()


def test_script_does_not_block_message_schedule(tmp_path, monkeypatch):
    state = SimpleNamespace(
        _lock=threading.Lock(), hook_registry=SimpleNamespace(fire=Mock()),
        _build_supervisor_hook_ctx=lambda **kw: kw,
    )
    runner = scheduler.SchedulerThread(state=state, workspace_root=tmp_path)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    worker_threads = []

    def blocking_script(*args):
        worker_threads.append(threading.current_thread())
        started.set()
        release.wait(3)
        done.set()

    monkeypatch.setattr(scheduler, "load_schedules", lambda root: [
        {"id": "slow", "type": "script", "cron": "* * * * *"},
        {"id": "message", "cron": "* * * * *", "text": "hello"},
    ])
    runner._fire_script = blocking_script
    runner._fire_message = Mock()
    try:
        runner._tick()
        assert started.wait(1)
        runner._fire_message.assert_called_once()
        assert worker_threads[0].daemon
        assert worker_threads[0] is not threading.current_thread()
    finally:
        release.set()
        assert done.wait(1)


def test_schedule_upsert_inherits_fields_and_preserves_other_approval_updates(tmp_path, monkeypatch):
    scheduler.save_schedules(tmp_path, [{
        "id": "job", "type": "script", "cron": "0 * * * *", "command": "old",
        "conversation_key": "discord:42", "timeout": 90, "silent": False,
        "enabled": False, "once": True,
    }])

    async def approve(ctx, op, args):
        current = scheduler.load_schedules(tmp_path)
        current.append({"id": "parallel", "text": "keep"})
        scheduler.save_schedules(tmp_path, current)
        return {}, None

    monkeypatch.setattr(schedules, "request_guard", approve)
    ctx = SimpleNamespace(workspace_root=tmp_path, conversation_key="wrong")
    res = asyncio.run(schedules.create_schedule({"id": "job", "command": "new"}, ctx))
    assert res["ok"]
    saved = scheduler.load_schedules(tmp_path)
    assert saved[0] == {
        "id": "job", "type": "script", "cron": "0 * * * *", "command": "new",
        "conversation_key": "discord:42", "timeout": 90, "silent": False,
        "enabled": False, "once": True,
    }
    assert saved[1]["id"] == "parallel"


@pytest.mark.parametrize("has_carrier", [True, False])
def test_async_result_reuses_carrier_or_keeps_root_owner(has_carrier):
    carrier = SimpleNamespace(task_id="resumed-branch") if has_carrier else None
    router = SimpleNamespace(
        _lock=threading.Lock(),
        tasks={},
        sessions={"s": SimpleNamespace(conversation_key="discord:42", channel="discord")},
        _find_dispatch_callback_carrier_locked=Mock(return_value=carrier),
        _is_branchless_running_task_locked=Mock(return_value=None),
        _append_preempt_to_task_locked=Mock(),
        eventlog=SimpleNamespace(append=Mock(return_value={"seq": 9})),
        _apply_inbound_message=Mock(), _advance_inbound_cursor=Mock(),
        _create_entry_task_for_inbound_locked=Mock(),
    )
    result = TaskRouterMixin.inject_async_result(router, "s", "done", ["file.txt"], "original")
    router._find_dispatch_callback_carrier_locked.assert_called_once_with("original", "s")
    if has_carrier:
        assert result == {"ok": True, "strategy": "preempt", "task_id": "resumed-branch"}
        args = router._append_preempt_to_task_locked.call_args.args
        assert args[0] is carrier
        assert '<dispatch_result kind="async_tool">' in args[1]
        assert args[2] == [{"path": "file.txt"}]
        router.eventlog.append.assert_not_called()
    else:
        assert result["strategy"] == "inbound"
        payload = router._create_entry_task_for_inbound_locked.call_args.kwargs["payload"]
        assert payload["_dispatch_owner_root_task_id"] == "original"
        assert payload["message_type"] == "async_tool_result"
        assert payload["attachments"] == [{"path": "file.txt"}]


def test_registry_pure_flags_disabled_todos_and_retired_cancel(tmp_path):
    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    definitions = {spec["name"]: spec for spec in reg.list_specs()}
    for name in {"list_dir", "read_file", "get_context_window"}:
        assert definitions[name]["pure"] is True
    assert reg.get_spec("list_schedules")["name"] == "manage_schedule"
    assert "list_schedules" not in definitions
    assert definitions["write_file"]["pure"] is False
    assert not ({"cancel_active_tasks", "update_todo_list", "get_todo_list"} & definitions.keys())


@pytest.mark.parametrize("old_name,target,action", [
    ("read_image", "read_media", None), ("read_video", "read_media", None),
    ("list_schedules", "manage_schedule", "list"), ("create_schedule", "manage_schedule", "upsert"),
    ("delete_schedule", "manage_schedule", "delete"), ("list_mcp_clients", "manage_mcp_client", "list"),
    ("create_or_update_mcp_client", "manage_mcp_client", "upsert"), ("delete_mcp_client", "manage_mcp_client", "delete"),
    ("list_skills", "manage_skill", "list"), ("create_or_update_skill", "manage_skill", "upsert"),
    ("delete_skill", "manage_skill", "delete"), ("create_or_update_tool", "manage_tool", "upsert"),
    ("reload_tools", "manage_tool", "reload"),
])
def test_legacy_alias_resolves_spec_authorization_and_execution(tmp_path, monkeypatch, old_name, target, action):
    from toolbox import _common

    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    calls = []
    guards = []

    async def tool(args, ctx):
        calls.append(args)
        return {"ok": True, "data": {"result": "done"}}

    async def guard(ctx, op, params):
        guards.append(params)
        return {}, None

    reg.register_builtin_tool(target, "test target", {"type": "object"}, tool)
    monkeypatch.setattr(_common, "request_guard", guard)
    original_args = {"path": "sample.png"}
    expected = {**original_args, **({"action": action} if action else {})}
    assert reg._resolve_alias(old_name, original_args) == (target, expected)
    assert original_args == {"path": "sample.png"}
    assert reg.get_spec(old_name)["name"] == target
    assert asyncio.run(reg.authorize(name=old_name, arguments=original_args, ctx=None)) is None
    assert guards[-1] == {"tool_name": target, "arguments": expected}
    result = asyncio.run(reg.execute(name=old_name, arguments=original_args, ctx=None, _authorized=True))
    assert result["ok"] and calls == [expected]
    assert reg._resolve_alias(old_name, {"action": "custom"})[1]["action"] == "custom"


@pytest.mark.parametrize("format_", ["dict", "scalar", "text"])
def test_script_output_parses_large_json_before_text_truncation(tmp_path, monkeypatch, format_):
    import json
    from unittest.mock import AsyncMock
    from toolbox.registry import _make_script_tool

    large = "value" * 5000
    structured = {"ok": True, "data": {"result": large, "nested": [1, {"text": large}]}}
    output = json.dumps(structured) if format_ == "dict" else json.dumps(large) if format_ == "scalar" else large
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(output.encode(), b"")))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    tool = _make_script_tool(script_path=tmp_path / "output.py", timeout_sec=5)
    result = asyncio.run(tool({}, SimpleNamespace(workspace_root=tmp_path)))
    if format_ == "dict":
        assert result == structured
    else:
        assert result["data"]["result"] == large[:16000]


def test_reset_cascades_tasks_approvals_async_tools_and_keeps_call_chain(tmp_path):
    from datetime import datetime, timedelta, timezone
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState
    from supervisor.types import Approval, ApprovalStatus, Task, TaskKind, TaskStatus

    state = SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "events.jsonl", run_id="r"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    root = state.get_or_create_session(channel="test", conversation_key="test:reset")
    caller_branch, _ = state._create_entry_branch_locked(root, 1)
    sibling, _ = state._create_entry_branch_locked(root, 2)
    dispatch = state.get_or_create_session(channel="test", conversation_key="agent:worker:test:reset")
    nested, _ = state._create_entry_branch_locked(dispatch, 3)
    # Persisted-only nested sessions must survive missing in-memory parent indexes.
    state.parent_children.pop(dispatch, None)
    state.parent_entry_branches.pop(dispatch, None)
    state.entry_branch_parents.pop(nested, None)
    now = datetime.now(timezone.utc)

    def task(tid, sid, status=TaskStatus.pending, caller=None, lease=None):
        t = Task(task_id=tid, session_id=sid, kind=TaskKind.node, node_id="worker", status=status,
                 caller_task_id=caller, created_at=now, updated_at=now, lease_expires_at=lease)
        state.tasks[tid] = t
        return t

    parent = task("parent", root, TaskStatus.suspended)
    current = task("current", caller_branch, TaskStatus.running, "parent", now + timedelta(minutes=1))
    pending = task("pending", root)
    suspended = task("suspended", sibling, TaskStatus.suspended)
    leased = task("leased", dispatch, TaskStatus.running, lease=now + timedelta(minutes=1))
    expired = task("expired", dispatch, TaskStatus.running, lease=now - timedelta(minutes=1))
    nested_task = task("nested", nested)
    descendant = task("descendant", "detached", caller="nested")
    grandchild = task("grandchild", "another-detached", caller="descendant")
    unrelated = task("unrelated", "unrelated-session")

    for tid, sid in [("pending", root), ("grandchild", "another-detached"), ("current", caller_branch), ("parent", root), ("unrelated", "unrelated-session")]:
        state.approvals[tid] = Approval(
            approval_id=tid, session_id=sid, operation="write_file", task_id=tid,
            status=ApprovalStatus.pending, fingerprint=tid, requested_at=now,
        )
    for tid in ["pending", "grandchild", "current", "unrelated"]:
        state._async_tools[tid] = {"task_id": tid, "session_id": state.tasks[tid].session_id,
                                   "status": "running", "worker_id": "worker"}
    # Create real history files to validate physical cleanup and caller retention.
    conversation_dir = tmp_path / "data" / "conversations"
    for sid in [root, caller_branch, sibling, dispatch, nested]:
        (conversation_dir / f"{sid}.jsonl").write_text("", encoding="utf-8")

    result = state.reset_conversation(conversation_key="test:reset", exclude_session_id=caller_branch, exclude_task_id="current")
    assert result["ok"] and result["cancelled_tasks"] == 7
    assert result["cleared_children"] == 3
    assert not parent.cancel_requested and not current.cancel_requested and not unrelated.cancel_requested
    for t in [pending, suspended, expired, nested_task, descendant, grandchild]:
        assert t.status == TaskStatus.cancelled
        assert t.input["_branch_finalized"] and t.input["_branch_skipped_merge"]
    assert leased.cancel_requested and leased.status == TaskStatus.running
    assert leased.input["_branch_skipped_merge"]
    for tid in ["pending", "grandchild"]:
        assert state.approvals[tid].status == ApprovalStatus.denied
        assert state.approvals[tid].decision == "deny"
        assert state.approvals[tid].comment == "conversation reset"
    for tid in ["parent", "current", "unrelated"]:
        assert state.approvals[tid].status == ApprovalStatus.pending
    assert set(state.pull_pending_cancels("worker")) == {"pending", "grandchild"}
    assert caller_branch in state._skip_merge_sessions
    assert (conversation_dir / f"{caller_branch}.jsonl").exists()
    for sid in [root, sibling, dispatch, nested]:
        assert not (conversation_dir / f"{sid}.jsonl").exists()
    assert "agent:worker:test:reset" not in state.conversation_map


def test_manage_skill_list_is_pure_and_legacy_delete_deny_keeps_manager_visible():
    from engine.inference.ai_step import _is_pure_tool_call
    from engine.inference.pseudo_tools import _filter_tool_specs
    from engine.node import Node, ToolAccess

    spec = {"name": "manage_skill", "input_schema": {"type": "object"}}
    assert _is_pure_tool_call("manage_skill", {"action": "list"}, spec)
    assert not _is_pure_tool_call("manage_skill", {"action": "delete", "name": "sample"}, spec)
    node = Node(id="agent", type="ai", tool_access=ToolAccess(mode="all", deny=["delete_skill"]))
    assert _filter_tool_specs(node, [spec]) == [spec]


@pytest.mark.parametrize("key", ["path", "image_path", "video_path", "paths"])
def test_read_media_legacy_paths_and_concurrent_mixed_inputs(tmp_path, monkeypatch, key):
    import base64
    from io import BytesIO
    import json
    from threading import Barrier
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    spec = spec_from_file_location("read_media_under_test", Path(__file__).resolve().parents[1] / "tools" / "read_media.py")
    media = module_from_spec(spec)
    spec.loader.exec_module(media)

    image, video = tmp_path / "sample.png", tmp_path / "sample.mp4"
    image.write_bytes(b"image bytes")
    video.write_bytes(b"video bytes")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://api.example.test/v1")
    captured = []

    def respond(request, timeout):
        captured.append(json.loads(request.data))
        return BytesIO(json.dumps({"candidates": [{"content": {"parts": [
            {"text": "private reasoning", "thought": True}, {"text": "media description"},
        ]}}]}).encode())

    monkeypatch.setattr(media.urllib_request, "urlopen", respond)
    if key == "paths":
        barrier = Barrier(2)
        prepare = media._prepare_media

        def concurrent_prepare(path, root):
            barrier.wait(timeout=2)
            return prepare(path, root)

        monkeypatch.setattr(media, "_prepare_media", concurrent_prepare)
        args = {"paths": [str(image), str(video)], "prompt": "OCR"}
    else:
        args = {key: str(video if key == "video_path" else image), "focus": "OCR"}
    result = media.read_media(args, tmp_path)
    assert result["data"]["result"] == "media description"
    parts = captured[0]["contents"][0]["parts"]
    assert "OCR" in parts[-1]["text"]
    assert len(parts) == (3 if key == "paths" else 2)
    assert base64.b64decode(parts[0]["inlineData"]["data"]) == (b"video bytes" if key == "video_path" else b"image bytes")
    if key == "paths":
        assert parts[1]["inlineData"]["mimeType"] == "video/mp4"


def test_manage_schedule_and_tool_wrappers_preserve_crud_and_metadata(tmp_path, monkeypatch):
    from toolbox import _common
    from toolbox.builtins import manage_schedule, manage_tool
    from toolbox.context import ToolContext

    async def guard(*args):
        return {}, None

    monkeypatch.setattr(_common, "request_guard", guard)
    monkeypatch.setattr(schedules, "request_guard", guard)
    # write_file imports the guard at module scope.
    import importlib
    monkeypatch.setattr(importlib.import_module("toolbox.builtins.write_file"), "request_guard", guard)
    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    ctx = ToolContext(workspace_root=tmp_path, registry=reg, supervisor_url="test", session_id="s", run_id="r", worker_id="w", http=None)
    upsert = asyncio.run(manage_schedule({"action": "upsert", "id": "job", "cron": "* * * * *", "text": "hello"}, ctx))
    assert upsert["ok"]
    assert asyncio.run(manage_schedule({"action": "list"}, ctx))["ok"]
    assert asyncio.run(manage_schedule({"action": "delete", "id": "job"}, ctx))["ok"]
    assert scheduler.load_schedules(tmp_path) == []
    result = asyncio.run(manage_tool({"action": "upsert", "name": "example", "script": "output({'ok': True})", "pure": True, "async_mode": True}, ctx))
    assert result["ok"]
    assert reg.get_spec("example")["pure"] is True
    assert reg.get_spec("example")["async_mode"] is True
    assert asyncio.run(manage_tool({"action": "reload"}, ctx))["ok"]


def test_grep_replace_supports_dollar_capture_groups_and_literal_mode(tmp_path: Path) -> None:
    f1 = tmp_path / "regex_cap.txt"
    f1.write_text("foo_123 and bar_456", encoding="utf-8")
    rx1 = re.compile(r"([a-z]+)_(\d+)")
    res1 = _do_replace_sync(tmp_path, rx1, "$2:$1", "regex_cap.txt", 10, 1_000_000, tmp_path, is_regex=True)
    assert res1["ok"] is True
    assert f1.read_text(encoding="utf-8") == "123:foo and 456:bar"

    f2 = tmp_path / "literal.txt"
    f2.write_text("hello world", encoding="utf-8")
    rx2 = re.compile(re.escape("world"))
    res2 = _do_replace_sync(tmp_path, rx2, r"\\1 $1", "literal.txt", 10, 1_000_000, tmp_path, is_regex=False)
    assert res2["ok"] is True
    assert f2.read_text(encoding="utf-8") == r"hello \\1 $1"


def test_read_file_oversized_supports_start_line_streaming_and_head_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rf_mod = sys.modules["toolbox.builtins.read_file"]

    monkeypatch.setattr(rf_mod, "_MAX_TEXT_FILE_BYTES", 2048)
    big_file = tmp_path / "big.txt"
    lines = [f"line_{i:04d}_" + ("x" * 30) for i in range(1, 201)]
    big_file.write_text("\n".join(lines), encoding="utf-8")
    assert big_file.stat().st_size > 2048

    r_head, mm = _read_single_file_sync(big_file, "big.txt", None, None, tmp_path)
    assert mm is None
    assert r_head["success"] is True
    assert r_head["oversized"] is True
    assert r_head["startLine"] == 1
    assert r_head["endLine"] == 50
    assert "line_0001_" in r_head["content"]
    assert "line_0050_" in r_head["content"]
    assert "line_0051_" not in r_head["content"]

    r_mid, _ = _read_single_file_sync(big_file, "big.txt", 120, 130, tmp_path)
    assert r_mid["success"] is True
    assert r_mid["oversized"] is True
    assert r_mid["startLine"] == 120
    assert r_mid["endLine"] == 130
    assert r_mid["lineCount"] == 11
    assert "line_0120_" in r_mid["content"]
    assert "line_0130_" in r_mid["content"]
    assert "line_0131_" not in r_mid["content"]

    monkeypatch.setattr(rf_mod, "_MAX_OVERSIZED_SCAN_CHARS", 1000)
    r_deep, _ = _read_single_file_sync(big_file, "big.txt", 150, 160, tmp_path)
    assert r_deep["success"] is False


def test_apply_diff_multi_match_shift_tracks_actual_replaced_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ad_mod = sys.modules["toolbox.builtins.apply_diff"]

    async def _noop_guard(ctx: Any, op: str, params: dict[str, Any]) -> tuple[dict[str, Any], None]:
        return {}, None

    monkeypatch.setattr(ad_mod, "request_guard", _noop_guard)

    f = tmp_path / "code.py"
    f.write_text(
        "line1\n"
        "target\n"
        "marker\n"
        "line4\n"
        "target\n"
        "line6\n"
        "marker\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        supervisor_url="http://127.0.0.1:1",
        session_id="s1",
        run_id="r1",
        worker_id="w1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
    )
    res = asyncio.run(apply_diff(
        {
            "path": "code.py",
            "diffs": [
                {"search": "target", "replace": "t_a\nt_b\nt_c", "start_line": 5},
                {"search": "marker", "replace": "FIRST_MARKER", "start_line": 3},
            ],
        },
        ctx,
    ))
    assert res["ok"] is True
    lines = f.read_text(encoding="utf-8").splitlines()
    assert lines[2] == "FIRST_MARKER"
    assert lines[-1] == "marker"


@pytest.mark.parametrize("mode,allow,deny,expected", [
    ("all", [], ["manage_memory:!list"], ["list"]),
    ("allowlist", ["manage_memory:list"], [], ["list"]),
    ("allowlist", ["manage_memory:!upsert,delete"], [], ["list"]),
    ("allowlist", ["list_memories"], [], ["list"]),
    ("all", [], ["save_memory", "delete_memory"], ["list"]),
    ("all", [], ["manage_memory:delete"], ["list", "upsert"]),
    ("allowlist", ["manage_memory"], ["delete_memory"], ["list", "upsert"]),
])
def test_manage_memory_action_permissions_prune_schema_and_enforce_synonyms(mode, allow, deny, expected):
    import copy
    from engine.builtin.knowledge_inject import PLUGIN_META
    from engine.inference.ai_step import _is_pure_tool_call
    from engine.inference.pseudo_tools import build_node_tool_specs, is_tool_action_allowed
    from engine.node import Node, ToolAccess

    spec = next(spec for spec in PLUGIN_META["tools"] if spec["name"] == "manage_memory")
    original = copy.deepcopy(spec["input_schema"])
    node = Node(id="reader", type="ai", control_tools="none", tool_access=ToolAccess(
        mode=mode, allow=allow, deny=deny,
    ))
    allowed_names, visible = build_node_tool_specs(registry=SimpleNamespace(list_specs=lambda: [spec]), node=node)
    assert "manage_memory" in allowed_names
    assert visible[0]["function"]["parameters"]["properties"]["action"]["enum"] == expected
    assert spec["input_schema"] == original
    for action, canonical in [("list", "list"), ("get", "list"), ("read", "list"),
                              ("upsert", "upsert"), ("save", "upsert"), ("create", "upsert"),
                              ("update", "upsert"), ("delete", "delete"), ("remove", "delete")]:
        assert is_tool_action_allowed(node, "manage_memory", {"action": action}) == (canonical in expected)
    assert is_tool_action_allowed(node, "manage_memory", {}) == ("list" in expected)
    assert is_tool_action_allowed(node, "manage_memory", {"content": "write"}) == ("upsert" in expected)
    for legacy, action in [("list_memories", "list"), ("save_memory", "upsert"), ("delete_memory", "delete")]:
        assert is_tool_action_allowed(node, legacy, {}) == (action in expected)
    assert _is_pure_tool_call("manage_memory", {"action": "list"}, spec)
    assert _is_pure_tool_call("manage_memory", {}, spec)
    assert not _is_pure_tool_call("manage_memory", {"action": "delete"}, spec)
    assert not _is_pure_tool_call("manage_memory", {"content": "write"}, spec)
    assert _is_pure_tool_call("read_file", {}, None)
    assert _is_pure_tool_call("custom_reader", {}, {"pure": True})


@pytest.mark.parametrize("prompt,estimate,threshold,source,effective", [
    (80000, 12000, 160000, "provider_reported", 80000),
    (0, 40000, 100000, "char_estimate", 40000),
    (-1, 200000, 160000, "char_estimate", 200000),
])
def test_get_context_window_exposes_token_metrics_and_does_not_limit_calls(tmp_path, monkeypatch, prompt, estimate, threshold, source, effective):
    from toolbox.builtins import tasks
    import httpx

    cfg = {} if threshold == 160000 else {"engine": {"compact": {"threshold_tokens": threshold}}}
    monkeypatch.setattr(tasks, "load_runtime_config", lambda root: cfg)
    urls = []
    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"prompt_tokens": prompt, "estimated_tokens": estimate, "message_count": 12})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            ctx = SimpleNamespace(http=http, supervisor_url="http://supervisor", workspace_root=tmp_path,
                                  route_session_id=lambda: "parent", task_id="same-task")
            for _ in range(4):
                result = await tasks.get_context_window({}, ctx)
                assert result["ok"]
                data = result["data"]
                assert data["effective_tokens"] == effective
                assert data["source"] == source
                assert data["compact_threshold"] == threshold
                assert data["utilization_percent"] == pytest.approx(effective / threshold * 100)
                assert data["remaining_tokens"] == max(0, threshold - effective)
                assert data["message_count"] == 12
                for text in [f"{effective:,}", f"{threshold:,}", source, "messages=12", f"prompt_tokens={prompt}"]:
                    assert text in data["result"]
    asyncio.run(run())
    assert urls == ["http://supervisor/v1/sessions/parent/context_usage"] * 4


def test_manage_memory_dispatch_and_cache_invalidation(tmp_path):
    from engine.builtin import knowledge_inject as memory
    _LEGACY_TOOL_ALIASES = ToolRegistry._LEGACY_ALIAS_MAP

    ctx = SimpleNamespace(workspace_root=tmp_path, node_id="main", caller_node_id="", node_extra={}, _node_id="main", _node_extra={}, workspace_name="")
    async def run():
        assert (await memory.manage_memory({"action": "create", "book": "test", "id": "entry", "content": "remember"}, ctx))["ok"]
        result = await memory.manage_memory({"action": "read", "book": "test", "id": "entry"}, ctx)
        assert result["data"]["entries"][0]["content"] == "remember"
        memory._hit_cache["entry"] = "now"
        assert (await memory.manage_memory({"action": "remove", "book": "test", "id": "entry"}, ctx))["ok"]
        assert "entry" not in memory._hit_cache
        assert not (await memory.manage_memory({"action": "unknown"}, ctx))["ok"]
    asyncio.run(run())
    public = {spec["name"] for spec in memory.PLUGIN_META["tools"]}
    assert "manage_memory" in public
    assert not public.intersection({"save_memory", "list_memories", "delete_memory"})
    assert _LEGACY_TOOL_ALIASES["save_memory"] == ("manage_memory", "upsert")
    assert _LEGACY_TOOL_ALIASES["list_memories"] == ("manage_memory", "list")
    assert _LEGACY_TOOL_ALIASES["delete_memory"] == ("manage_memory", "delete")


@pytest.mark.parametrize("name,args,blocked", [
    ("manage_memory", {"action": "upsert", "content": "write"}, True),
    ("manage_memory", {"action": "delete", "id": "entry"}, True),
    ("manage_memory", {"content": "implicit write"}, True),
    ("save_memory", {"content": "legacy write"}, True),
    ("delete_memory", {"id": "entry"}, True),
    ("manage_memory", {"action": "list"}, False),
])
def test_ai_step_rejects_ungranted_memory_actions_before_execution(monkeypatch, name, args, blocked):
    from engine.inference import ai_step
    from engine.inference.tool_format import NativeToolFormatter
    from engine.node import Node, ToolAccess

    node = Node(id="reader", type="ai", tool_access=ToolAccess(mode="allowlist", allow=["manage_memory:list"]))
    ls = SimpleNamespace(node=node, messages=[], formatter=NativeToolFormatter(),
        allowed_real_tools={"manage_memory", "save_memory", "list_memories", "delete_memory"},
        allowed_pseudo_tools=set(), provider=SimpleNamespace(name="test"), last_usage={}, openai_tools=[],
        rctx=SimpleNamespace(tool_call_log=[], response_info={}, current_llm_request_id=""),
        _reasoning_started_iso="", _reasoning_ended_iso="")
    tc = SimpleNamespace(id="call", name=name, arguments=args)
    resp = SimpleNamespace(tool_calls=[tc], text="", provider_meta={}, reasoning="", thought_summary="", inline_data=[])
    observed = []
    sentinel = object()
    async def stop_before_execution(point, context):
        assert point == "before_tool_call"
        observed.append(context.extra)
        return SimpleNamespace(action=sentinel)
    monkeypatch.setattr(ai_step.hook_registry, "afire", stop_before_execution)
    monkeypatch.setattr(ai_step, "_shadow_write", lambda *a, **kw: None)
    assert asyncio.run(ai_step._handle_tool_calls_inner(ls, resp, 0)) is sentinel
    assert observed[0]["unauthorized_count"] == int(blocked)
    assert len(observed[0]["real_tool_calls"]) == int(not blocked)
    assert len(ls.rctx.tool_call_log) == int(not blocked)


def test_legacy_manager_actions_cannot_override_authorized_alias(tmp_path):
    from engine.inference.pseudo_tools import _filter_tool_specs, is_tool_action_allowed
    from engine.node import Node, ToolAccess

    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    for old, action in [("list_memories", "list"), ("save_memory", "upsert"), ("delete_memory", "delete")]:
        name, args = registry._resolve_alias(old, {"action": "forged"})
        assert name == "manage_memory" and args["action"] == action
    node = Node(id="skill-reader", type="ai", tool_access=ToolAccess(mode="all", deny=["delete_skill"]))
    spec = {"name": "manage_skill", "input_schema": {"properties": {"action": {"enum": ["list", "upsert", "delete"]}}}}
    assert _filter_tool_specs(node, [spec])[0]["input_schema"]["properties"]["action"]["enum"] == ["list", "upsert"]
    assert not is_tool_action_allowed(node, "manage_skill", {"action": "remove"})
    assert not is_tool_action_allowed(node, "delete_skill", {})
    assert not is_tool_action_allowed(node, "list_skills", {"action": "remove"})
