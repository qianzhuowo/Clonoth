"""消息链断言（对应 LWB scene-planner-messages.js 的 assertScenePlannerMessages）。

要求：每个 assistant tool_call 都有紧随其后的 tool 回复、至少一条 user 消息、
非 tool 消息内容非空、tool 消息必须带 tool_call_id 且对应前一条调用。
"""
from __future__ import annotations

from typing import Any, Sequence


class PlannerMessagesInvalid(ValueError):
    pass


ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})


def assert_planner_messages(messages: Sequence[dict[str, Any]]) -> None:
    if not isinstance(messages, (list, tuple)) or not messages:
        raise PlannerMessagesInvalid("消息链为空")
    pending_call_ids: list[str] = []
    has_user = False
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise PlannerMessagesInvalid(f"messages[{index}] 不是对象")
        role = message.get("role")
        if role not in ALLOWED_ROLES:
            raise PlannerMessagesInvalid(f"messages[{index}].role={role!r} 无效")
        content = message.get("content")
        if not isinstance(content, str):
            raise PlannerMessagesInvalid(f"messages[{index}].content 必须是字符串")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not pending_call_ids:
                raise PlannerMessagesInvalid(f"messages[{index}] 是没有对应调用的工具返回")
            expected = pending_call_ids.pop(0)
            if call_id != expected:
                raise PlannerMessagesInvalid(f"messages[{index}].tool_call_id={call_id!r}，应为 {expected!r}")
            continue
        if pending_call_ids:
            raise PlannerMessagesInvalid(f"messages[{index}] 之前的工具调用 {pending_call_ids[0]!r} 没有回复")
        if role == "user":
            has_user = True
        calls = message.get("tool_calls")
        if calls:
            if role != "assistant":
                raise PlannerMessagesInvalid(f"messages[{index}] 非 assistant 却带 tool_calls")
            for call in calls:
                call_id = (call or {}).get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise PlannerMessagesInvalid(f"messages[{index}] 的 tool_call 缺少 id")
                pending_call_ids.append(call_id)
        elif not content.strip():
            raise PlannerMessagesInvalid(f"messages[{index}] 内容为空")
    if pending_call_ids:
        raise PlannerMessagesInvalid(f"末尾的工具调用 {pending_call_ids[0]!r} 没有回复")
    if not has_user:
        raise PlannerMessagesInvalid("消息链至少需要一条 user 消息")
