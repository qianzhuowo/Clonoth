"""子任务附件直发（dispatch_attachment）与防重发。

绘图子任务完成后，图片由 Supervisor 直接作为 outbound_message 发出，
回调给调用方的 dispatch_result 只带 attachments_outbound_sent 标记；
引擎侧 reply/finish 在这样的回合里丢弃模型再次选中的附件。
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from engine.inference import pseudo_handlers
from engine.protocol import ACTION_FINISH, TaskAction
from supervisor.task_router import TaskRouterMixin
from supervisor.types import TaskKind, TaskStatus

CHILD_NODE = "draw.novelai_planner"
IMAGE = {"path": "data/attachments/novelai/nai_test.png", "type": "image", "name": "nai_test.png"}


def _state(tmp_path: Path):
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState

    return SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "events.jsonl", run_id="r"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


def _create(state: Any, sid: str, node_id: str, input_data: dict[str, Any]):
    with state._lock:
        return state._create_task_locked(
            session_id=sid,
            session_generation=state._current_session_generation_locked(sid) or 1,
            kind=TaskKind.node, node_id=node_id, input_data=input_data,
        )


def _finish(text: str = "已生成 1 张") -> dict[str, Any]:
    return TaskAction(
        action=ACTION_FINISH, node_id=CHILD_NODE,
        result={"text": text, "attachments": [dict(IMAGE)]},
    ).to_dict()


def _events(state: Any, sid: str, type_: str) -> list[dict[str, Any]]:
    return [e for e in state.eventlog.list_events(session_id=sid) if e.get("type") == type_]


@pytest.fixture
def parent(tmp_path: Path):
    state = _state(tmp_path)
    sid = state.get_or_create_session(channel="test", conversation_key="test:draw")
    caller = _create(state, sid, "qq.orchestrator", {"instruction": "画一张"})
    return state, sid, caller


def _child(state: Any, sid: str, caller: Any):
    return _create(state, sid, CHILD_NODE, {
        "instruction": "画一张初音未来",
        "_dispatch_origin": {
            "parent_session_id": sid, "caller_node_id": "qq.orchestrator",
            "origin_task_id": caller.task_id,
        },
    })


def test_dispatch_attachments_are_sent_directly_and_callback_is_marked(parent) -> None:
    """调用方已结束：附件直发 + 新 inbound 只带标记。"""
    state, sid, caller = parent
    caller.status = TaskStatus.completed
    child = _child(state, sid, caller)

    state.complete_task(task_id=child.task_id, worker_id="w1", result=_finish())

    outbound = _events(state, sid, "outbound_message")
    assert len(outbound) == 1
    p = outbound[0]["payload"]
    assert p["message_type"] == "dispatch_attachment"
    assert p["attachments"] == [IMAGE]
    assert p["text"] == ""
    assert p["node_id"] == CHILD_NODE
    assert p["child_task_id"] == child.task_id
    assert p["conversation_key"] == "test:draw"

    inbound = _events(state, sid, "inbound_message")
    assert len(inbound) == 1
    ip = inbound[0]["payload"]
    assert ip["message_type"] == "dispatch_result"
    assert ip["attachments_outbound_sent"] is True
    assert "attachments" not in ip
    # 由该 inbound 派生的入口任务拿到防重发标记。
    handler = [t for t in state.tasks.values() if t.input.get("inbound_message_type") == "dispatch_result"]
    assert len(handler) == 1
    assert handler[0].input["inbound_attachments_outbound_sent"] is True


def test_dispatch_attachments_sent_before_preempting_running_caller(parent) -> None:
    """调用方仍在运行：附件直发，打断消息不再带附件但注明已投递。"""
    state, sid, caller = parent
    caller.status = TaskStatus.running
    child = _child(state, sid, caller)

    state.complete_task(task_id=child.task_id, worker_id="w1", result=_finish())

    assert len(_events(state, sid, "outbound_message")) == 1
    assert caller.preempt_requested is True
    assert caller.preempt_attachments == []
    assert '"attachments_delivered": true' in caller.preempt_message
    assert "已生成 1 张" in caller.preempt_message
    assert not _events(state, sid, "inbound_message")


def test_dispatch_attachment_not_sent_twice_for_same_child(parent) -> None:
    state, sid, caller = parent
    caller.status = TaskStatus.completed
    child = _child(state, sid, caller)
    with state._lock:
        for _ in range(2):
            state._inject_dispatch_result_via_origin_locked(child, {"text": "x", "attachments": [dict(IMAGE)]})
    assert len(_events(state, sid, "outbound_message")) == 1


def test_child_declared_delivery_marks_callback_without_resending(parent) -> None:
    """子节点自己逐张直发（NovelAI）：回调不带附件，但两条路径都注明已送达。"""
    state, sid, caller = parent
    caller.status = TaskStatus.completed
    child = _child(state, sid, caller)
    with state._lock:
        state._inject_dispatch_result_via_origin_locked(child, {"text": "已生成 4 张图片", "attachments_delivered": 4})
    assert not _events(state, sid, "outbound_message")
    ip = _events(state, sid, "inbound_message")[0]["payload"]
    assert ip["attachments_outbound_sent"] is True and "attachments" not in ip

    running = _create(state, sid, "qq.orchestrator", {"instruction": "画"})
    running.status = TaskStatus.running
    child2 = _create(state, sid, CHILD_NODE, {"instruction": "画", "_dispatch_origin": {
        "parent_session_id": sid, "caller_node_id": "qq.orchestrator", "origin_task_id": running.task_id,
    }})
    with state._lock:
        state._inject_dispatch_result_via_origin_locked(child2, {"text": "已生成 1 张图片", "attachments_delivered": 1})
    assert '"attachments_delivered": true' in running.preempt_message


def test_late_result_to_finished_nested_dispatcher_returns_to_its_caller(parent) -> None:
    """被委派的中间节点先结束、它的子任务结果迟到：唤醒的是该中间节点本身，
    并沿用它的 dispatch_origin（结果继续交回上游），而不是用会话默认入口节点直接对用户说话。"""
    state, sid, caller = parent
    caller.status = TaskStatus.completed
    child_sid = state.get_or_create_session(channel="test", conversation_key="agent:bootstrap.executor:test:draw")
    executor_origin = {
        "parent_session_id": sid, "caller_node_id": "qq.orchestrator", "origin_task_id": caller.task_id,
        "parent_conversation_key": "test:draw", "context_mode": "fresh",
    }
    executor = _create(state, child_sid, "bootstrap.executor", {"instruction": "列出路径", "_dispatch_origin": executor_origin})
    executor.status = TaskStatus.completed
    reviewer = _create(state, child_sid, "bootstrap.cmd_reviewer", {"instruction": "ls", "_dispatch_origin": {
        "parent_session_id": child_sid, "caller_node_id": "bootstrap.executor", "origin_task_id": executor.task_id,
    }})

    with state._lock:
        state._inject_dispatch_result_via_origin_locked(reviewer, {"text": "returncode=0\na.png"})

    woken = [t for t in state.tasks.values() if t.input.get("inbound_message_type") == "dispatch_result"]
    assert len(woken) == 1
    assert woken[0].node_id == "bootstrap.executor"
    assert woken[0].input["_dispatch_origin"]["origin_task_id"] == caller.task_id
    assert woken[0].input["_dispatch_origin"]["parent_session_id"] == sid
    assert woken[0].input["_dispatch_owner_root_task_id"] == executor.task_id


def test_late_result_to_finished_root_caller_wakes_caller_node(parent) -> None:
    state, sid, caller = parent
    caller.status = TaskStatus.completed
    child = _child(state, sid, caller)
    with state._lock:
        state._inject_dispatch_result_via_origin_locked(child, {"text": "done"})
    woken = [t for t in state.tasks.values() if t.input.get("inbound_message_type") == "dispatch_result"]
    assert [t.node_id for t in woken] == ["qq.orchestrator"]
    assert not woken[0].input.get("_dispatch_origin")


def test_runner_wrapper_marks_delivered_attachments() -> None:
    from engine.runner import _wrap_dispatch_result_text, _wrap_stored_inbound_callback

    live = _wrap_dispatch_result_text("已生成", message_type="dispatch_result", source=CHILD_NODE, task_id="t",
                                      attachments_delivered=True)
    stored = _wrap_stored_inbound_callback("已生成", message_type="dispatch_result", meta={
        "child_node_id": CHILD_NODE, "child_task_id": "t", "attachments_delivered": True,
    })
    assert '"attachments_delivered": true' in live and live == stored
    assert "attachments_delivered" not in _wrap_dispatch_result_text("x", message_type="dispatch_result")


def test_async_tool_success_with_attachments_goes_straight_out(tmp_path: Path) -> None:
    state = _state(tmp_path)
    sid = state.get_or_create_session(channel="test", conversation_key="test:async")
    task = _create(state, sid, "qq.orchestrator", {"instruction": "画"})
    task.status = TaskStatus.running

    ok = state.inject_async_result(
        sid, "done", ["data/attachments/novelai/a.png"], task.task_id,
        node_id="qq.orchestrator", tool_name="nai_generate", success=True,
    )
    assert ok == {"ok": True, "strategy": "outbound_attachment"}
    outbound = _events(state, sid, "outbound_message")
    assert len(outbound) == 1
    p = outbound[0]["payload"]
    assert p["message_type"] == "async_tool_attachment"
    assert p["attachments"] == [{"path": "data/attachments/novelai/a.png"}]
    assert p["tool_name"] == "nai_generate"
    assert p["task_id"] == task.task_id
    assert task.preempt_requested is False

    # 失败结果不走快速通道：错误文本必须回到模型。
    bad = state.inject_async_result(
        sid, "boom", ["data/attachments/novelai/b.png"], task.task_id, success=False,
    )
    assert bad["strategy"] == "preempt"
    assert task.preempt_requested is True
    assert "boom" in task.preempt_message
    assert len(_events(state, sid, "outbound_message")) == 1


def _loop_state(task_context: dict[str, Any], tool_atts: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        rctx=SimpleNamespace(task_context=task_context),
        tool_produced_attachments=list(tool_atts or []),
    )


def test_engine_drops_reselected_paths_only_on_already_sent_dispatch_turn() -> None:
    sent = _loop_state({"inbound_message_type": "dispatch_result", "inbound_attachments_outbound_sent": True})
    assert pseudo_handlers._reject_already_sent_dispatch_paths(sent, ["a.png"]) == []
    assert pseudo_handlers._dispatch_result_attachments_already_sent(sent) is True

    plain = _loop_state({"inbound_message_type": "dispatch_result"})
    assert pseudo_handlers._reject_already_sent_dispatch_paths(plain, ["a.png"]) == ["a.png"]

    user_turn = _loop_state({"inbound_attachments_outbound_sent": True})
    assert pseudo_handlers._reject_already_sent_dispatch_paths(user_turn, ["a.png"]) == ["a.png"]
    assert pseudo_handlers._reject_already_sent_dispatch_paths(plain, None) == []


def test_runner_task_context_carries_sent_flag() -> None:
    from engine.runner import _build_task_context

    ctx = _build_task_context({
        "task_context": {}, "inbound_message_type": "dispatch_result",
        "inbound_attachments_outbound_sent": True,
    })
    assert ctx["inbound_message_type"] == "dispatch_result"
    assert ctx["inbound_attachments_outbound_sent"] is True
    assert "inbound_attachments_outbound_sent" not in _build_task_context({"task_context": {}})


def test_inject_async_result_legacy_positional_call_still_preempts() -> None:
    carrier = SimpleNamespace(task_id="resumed-branch")
    router = SimpleNamespace(
        _lock=threading.Lock(), tasks={},
        sessions={"s": SimpleNamespace(conversation_key="discord:42", channel="discord")},
        _find_dispatch_callback_carrier_locked=Mock(return_value=carrier),
        _is_branchless_running_task_locked=Mock(return_value=None),
        _append_preempt_to_task_locked=Mock(),
        eventlog=SimpleNamespace(append=Mock(return_value={"seq": 9})),
        _apply_inbound_message=Mock(), _advance_inbound_=Mock(),
        _create_entry_task_for_inbound_locked=Mock(),
    )
    # 无 success 信息（旧式调用、引擎重启丢失通知）：带附件也不走直发，仍按承接 preempt。
    result = TaskRouterMixin.inject_async_result(router, "s", "done", ["file.txt"], "original")
    assert result == {"ok": True, "strategy": "preempt", "task_id": "resumed-branch"}
    router.eventlog.append.assert_not_called()
    # 显式 success=True 才直发。
    router._apply_outbound_message = Mock()
    result = TaskRouterMixin.inject_async_result(router, "s", "done", ["file.txt"], "original", success=True)
    assert result["strategy"] == "outbound_attachment"
    router.eventlog.append.assert_called_once()
