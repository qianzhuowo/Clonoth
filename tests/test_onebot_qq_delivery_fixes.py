"""[2026-09-17] QQ 投递修复回归测试：

1. _send_qq_message_once：NapCat 超时 / EventChecker 失败时绝不重发（修复连续 @ 两次）；
   仅 at/uid 类错误才把 at 降级为文本重发；ENOENT 原样重发一次。
2. 搜索进度心跳：仅在搜索进行中发送，still-running 最多 N 条。
3. 死信：SDK 立即消费 trigger 并回调 on_delivery_dead_letter；QQ 侧发一句提示。
4. 表情/图片与正文拆开发送：表情失败不阻塞正文。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.outbound_store import OutboundRecord  # noqa: E402
from clonoth_sdk.state import MainTaskState, SessionState, TriggerInfo  # noqa: E402
from clonoth_sdk.types import DeliveryContext, Event  # noqa: E402


# ----------------------------------------------------------------------
#  nonebot 桩 + 插件运行时加载（与 test_onebot_send_contract.py 保持一致）
# ----------------------------------------------------------------------

def _install_nonebot_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    class Matcher:
        def handle(self, *args: Any, **kwargs: Any):
            return lambda func: func

        def append_handler(self, *args: Any, **kwargs: Any):
            return lambda func: func

    class Driver:
        def on_startup(self, func=None):
            return (lambda value: value) if func is None else func

        def on_shutdown(self, func=None):
            return (lambda value: value) if func is None else func

    nonebot = types.ModuleType("nonebot")
    nonebot.get_bot = lambda *a, **k: None
    nonebot.get_driver = lambda: Driver()
    nonebot.on_message = lambda *a, **k: Matcher()
    nonebot.on_notice = lambda *a, **k: Matcher()

    class Segment(dict):
        @classmethod
        def image(cls, file: str):
            return cls(type="image", data={"file": file})

        @classmethod
        def text(cls, text: str):
            return cls(type="text", data={"text": text})

        @classmethod
        def reply(cls, message_id: Any):
            return cls(type="reply", data={"id": message_id})

        @classmethod
        def at(cls, user_id: Any):
            return cls(type="at", data={"qq": user_id})

        @property
        def type(self) -> str:
            return self["type"]

        @property
        def data(self) -> dict:
            return self["data"]

        def __add__(self, other: Any):
            return Message([self]) + other

    class Message(list):
        def __init__(self, value: Any = None):
            if value is None:
                value = []
            elif isinstance(value, (str, dict)):
                value = [value]
            super().__init__(value)

        def __add__(self, other: Any):
            return Message(list(self) + (list(other) if isinstance(other, list) else [other]))

    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    for name, value in {
        "Bot": object, "Event": object, "GroupMessageEvent": object,
        "GroupUploadNoticeEvent": object, "PrivateMessageEvent": object,
        "Message": Message, "MessageSegment": Segment,
    }.items():
        setattr(v11, name, value)
    exception = types.ModuleType("nonebot.adapters.onebot.v11.exception")
    exception.ActionFailed = type("ActionFailed", (RuntimeError,), {})
    rule = types.ModuleType("nonebot.rule")
    rule.Rule = lambda *a, **k: object()
    rule.to_me = lambda *a, **k: object()

    modules = {
        "nonebot": nonebot,
        "nonebot.adapters": types.ModuleType("nonebot.adapters"),
        "nonebot.adapters.onebot": types.ModuleType("nonebot.adapters.onebot"),
        "nonebot.adapters.onebot.v11": v11,
        "nonebot.adapters.onebot.v11.exception": exception,
        "nonebot.rule": rule,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_nonebot_stubs(monkeypatch)
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(tmp_path))
    root = _ROOT / "platform" / "onebot"
    name = f"_onebot_runtime_fix_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _at_message(runtime, text: str = "老大，结论如下"):
    seg = runtime.MessageSegment
    return runtime.Message([seg.at("10001"), seg.text(" "), seg.text(text)])


def _has_at(message: Any) -> bool:
    return any(isinstance(seg, dict) and seg.get("type") == "at" for seg in message)


def _patch_group_helpers(monkeypatch: pytest.MonkeyPatch, runtime) -> None:
    async def passthrough(bot, group_id, message):
        return message

    monkeypatch.setattr(runtime, "_sanitize_group_at_segments", passthrough)
    monkeypatch.setattr(runtime, "_group_member_display_name", lambda group_id, qq: "阡濯")
    monkeypatch.setattr(runtime, "_qq_profile_display_name", lambda qq: "")


class _GroupBot:
    self_id = "10002"

    def __init__(self, failures: list[Exception]):
        self.failures = list(failures)
        self.sent: list[Any] = []

    async def send_group_msg(self, *, group_id: int, message: Any):
        self.sent.append(message)
        if self.failures:
            raise self.failures.pop(0)
        return {"message_id": 100 + len(self.sent)}


_TIMEOUT_TEXT = (
    "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
    "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet: {\"result\": 0}"
)
_EVENTCHECKER_TEXT = (
    "EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
    "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet: "
    "{\"result\": 1006514, \"errMsg\": \"网络连接异常!\"}"
)
_ENOENT_TEXT = "ENOENT: no such file or directory, open '/app/.config/QQ/NapCat/temp/143d6a94'"


# ----------------------------------------------------------------------
#  1. 连续 @ 两次
# ----------------------------------------------------------------------

@pytest.mark.parametrize("error_text", [_TIMEOUT_TEXT, _EVENTCHECKER_TEXT])
def test_group_send_ambiguous_ack_with_at_never_resends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_text: str,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_group_helpers(monkeypatch, runtime)
    bot = _GroupBot([runtime.ActionFailed(error_text)])
    target = {"type": "group", "group_id": 100000001}

    with pytest.raises(runtime.ActionFailed):
        asyncio.run(runtime._send_qq_message_once(bot, target, _at_message(runtime)))

    # 只发了一次：不再把 at 转文本补发第二条。
    assert len(bot.sent) == 1
    assert _has_at(bot.sent[0])


def test_group_send_uid_error_with_at_resends_as_text_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_group_helpers(monkeypatch, runtime)
    bot = _GroupBot([runtime.ActionFailed("Get Uid Error")])
    target = {"type": "group", "group_id": 100000001}

    message_id = asyncio.run(runtime._send_qq_message_once(bot, target, _at_message(runtime)))

    assert message_id == "102"
    assert len(bot.sent) == 2
    assert _has_at(bot.sent[0])
    assert not _has_at(bot.sent[1])
    assert any(
        seg.get("type") == "text" and seg["data"]["text"] == "@阡濯" for seg in bot.sent[1]
    )


def test_group_send_enoent_retries_once_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_group_helpers(monkeypatch, runtime)
    bot = _GroupBot([runtime.ActionFailed(_ENOENT_TEXT)])
    target = {"type": "group", "group_id": 100000001}

    message_id = asyncio.run(runtime._send_qq_message_once(bot, target, _at_message(runtime)))

    assert message_id == "102"
    assert len(bot.sent) == 2
    # ENOENT 不是 at 问题：原样重发，at 段保留。
    assert _has_at(bot.sent[1])


def test_group_send_unknown_error_with_at_raises_without_resend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_group_helpers(monkeypatch, runtime)
    bot = _GroupBot([runtime.ActionFailed("rich media transfer failed")])
    target = {"type": "group", "group_id": 100000001}

    with pytest.raises(runtime.ActionFailed):
        asyncio.run(runtime._send_qq_message_once(bot, target, _at_message(runtime)))
    assert len(bot.sent) == 1


def test_napcat_error_classifiers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    AF = runtime.ActionFailed
    assert runtime._is_napcat_ambiguous_ack(AF(_TIMEOUT_TEXT))
    assert runtime._is_napcat_ambiguous_ack(AF(_EVENTCHECKER_TEXT))
    assert not runtime._is_napcat_ambiguous_ack(AF(_ENOENT_TEXT))
    assert runtime._is_napcat_at_uid_error(AF("Get Uid Error"))
    assert runtime._is_napcat_at_uid_error(AF("at member not found"))
    # data=None 之类的字段不能误命中 at。
    assert not runtime._is_napcat_at_uid_error(AF("ActionFailed(status='failed', retcode=1200, data=None)"))
    assert not runtime._is_napcat_at_uid_error(AF(_TIMEOUT_TEXT))
    assert not runtime._is_napcat_at_uid_error(AF(_ENOENT_TEXT))


# ----------------------------------------------------------------------
#  2. 搜索进度心跳
# ----------------------------------------------------------------------

def test_search_in_progress_tracks_start_and_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    start = "[qq.orchestrator] 执行 1 个工具：web_search"
    done = "[qq.orchestrator] web_search: 已获得结果: query=英国 解体"
    failed = "[qq.orchestrator] web_search: 失败: timeout"
    other = "[qq.orchestrator] 执行 1 个工具：read_file"

    assert runtime._search_in_progress([]) is False
    assert runtime._search_in_progress([start]) is True
    assert runtime._search_in_progress([start, done]) is False
    assert runtime._search_in_progress([start, failed]) is False
    assert runtime._search_in_progress([start, done, start]) is True
    assert runtime._search_in_progress([other]) is False
    assert runtime._search_in_progress([start, done, other]) is False


def test_still_running_notice_is_capped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "_SEARCH_PROGRESS_STILL_RUNNING_MAX_COUNT", 3)
    sent: list[str] = []

    async def fake_send(bot, target, message, **kwargs):
        sent.append(str(message))
        return "1"

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    clock = {"now": 1000.0}
    monkeypatch.setattr(runtime.time, "time", lambda: clock["now"])

    platform_data: dict[str, Any] = {}
    target = {"type": "group", "group_id": 1, "conversation_key": "qq_group:1"}

    async def drive(rounds: int) -> None:
        for _ in range(rounds):
            await runtime._maybe_send_search_progress_notice(object(), target, platform_data)
            clock["now"] += 21.0

    asyncio.run(drive(12))
    assert sent[0] == runtime._SEARCH_PROGRESS_FIRST_NOTICE
    assert sent.count(runtime._SEARCH_PROGRESS_STILL_RUNNING_NOTICE) == 3
    assert len(sent) == 4


def test_update_progress_only_notifies_while_search_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    notices: list[int] = []

    async def fake_notice(bot, target, platform_data):
        notices.append(1)

    async def fake_switch(bot, event, platform_data, stage):
        return None

    monkeypatch.setattr(runtime, "_maybe_send_search_progress_notice", fake_notice)
    monkeypatch.setattr(runtime, "_switch_react_stage", fake_switch)
    monkeypatch.setattr(runtime, "_target_from_platform_data", lambda pd: {"type": "group", "group_id": 1})

    class _Event:
        message_id = 42
        user_id = 7

    trigger = TriggerInfo(
        inbound_seq=1, conversation_key="qq_group:1", session_id="s",
        is_dm=False, platform_data={"event": _Event(), "bot": object()},
    )
    callbacks = runtime.TangQiuCallbacks()

    running = MainTaskState(progress_records=["[qq.orchestrator] 执行 1 个工具：web_search"])
    asyncio.run(callbacks.update_progress(trigger, running))
    assert notices == [1]

    finished = MainTaskState(progress_records=[
        "[qq.orchestrator] 执行 1 个工具：web_search",
        "[qq.orchestrator] web_search: 已获得结果: query=x",
    ])
    asyncio.run(callbacks.update_progress(trigger, finished))
    assert notices == [1]  # 搜索已结束，不再发心跳


# ----------------------------------------------------------------------
#  3. 死信即结束 trigger 并提示用户
# ----------------------------------------------------------------------

class _DeadLetterCallbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def on_delivery_dead_letter(self, trigger, conversation_key, error, **kwargs):
        self.calls.append({"trigger": trigger, "conversation_key": conversation_key, "error": error, **kwargs})


def _bare_router(callbacks: Any) -> EventRouter:
    router = EventRouter.__new__(EventRouter)
    router._state = SessionState()
    router._cb = callbacks
    return router


def _event(event_type: str, src_seq: int) -> Event:
    return Event(
        seq=10, event_id="evt-1", ts="", run_id="r", session_id="s", component="engine",
        type=event_type, payload={"source_inbound_seq": src_seq, "text": "hello"},
    )


def test_sdk_dead_letter_consumes_trigger_and_notifies_adapter() -> None:
    callbacks = _DeadLetterCallbacks()
    router = _bare_router(callbacks)
    trigger = TriggerInfo(inbound_seq=5, conversation_key="qq_group:1", session_id="s", is_dm=False)
    router._state.register_trigger(trigger)
    main_state = router._state.get_or_create_main_state(5)
    record = OutboundRecord(key="k", event={}, event_id="evt-1", seq=10, task_id="task-1")
    error = RuntimeError("ambiguous")

    asyncio.run(router._finish_dead_letter_trigger(
        _event("outbound_message", 5), record, error,
        route="qq_group:1", delivery_key="k", ambiguous_ack=True,
    ))

    assert router._state.get_trigger(5) is None
    assert router._state.get_main_state(5) is None
    assert trigger.platform_data["_stale_main_state"] is main_state
    assert len(callbacks.calls) == 1
    call = callbacks.calls[0]
    assert call["trigger"] is trigger
    assert call["conversation_key"] == "qq_group:1"
    assert call["event_type"] == "outbound_message"
    assert call["ambiguous_ack"] is True
    assert call["idempotency_key"] == "k"
    assert call["task_id"] == "task-1"


def test_sdk_dead_letter_intermediate_reply_keeps_trigger() -> None:
    callbacks = _DeadLetterCallbacks()
    router = _bare_router(callbacks)
    trigger = TriggerInfo(inbound_seq=5, conversation_key="qq_group:1", session_id="s", is_dm=False)
    router._state.register_trigger(trigger)
    record = OutboundRecord(key="k", event={}, event_id="evt-1", seq=10, task_id="task-1")

    asyncio.run(router._finish_dead_letter_trigger(
        _event("intermediate_reply", 5), record, RuntimeError("x"),
        route="qq_group:1", delivery_key="k", ambiguous_ack=False,
    ))

    assert router._state.get_trigger(5) is trigger
    assert callbacks.calls[0]["event_type"] == "intermediate_reply"


def test_sdk_dead_letter_without_adapter_callback_is_safe() -> None:
    router = _bare_router(object())
    trigger = TriggerInfo(inbound_seq=5, conversation_key="qq_group:1", session_id="s", is_dm=False)
    router._state.register_trigger(trigger)
    record = OutboundRecord(key="k", event={}, event_id="evt-1", seq=10, task_id="task-1")
    asyncio.run(router._finish_dead_letter_trigger(
        _event("outbound_message", 5), record, RuntimeError("x"),
        route="qq_group:1", delivery_key="k", ambiguous_ack=False,
    ))
    assert router._state.get_trigger(5) is None


def test_qq_dead_letter_callback_sends_single_notice_and_clears_reacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    sent: list[tuple[Any, Any]] = []
    cleared: list[Any] = []

    async def fake_send(bot, target, message, **kwargs):
        sent.append((target, message))
        return "1"

    async def fake_clear(bot, event):
        cleared.append(event)

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    monkeypatch.setattr(runtime, "_clear_message_reacts", fake_clear)

    class _Event:
        message_id = 42
        user_id = 10001

    trigger = TriggerInfo(
        inbound_seq=1, conversation_key="qq_group:100000001", session_id="s", is_dm=False,
        platform_data={
            "event": _Event(), "bot": object(), "type": "group", "group_id": 100000001,
            "conversation_key": "qq_group:100000001",
        },
    )
    callbacks = runtime.TangQiuCallbacks()
    asyncio.run(callbacks.on_delivery_dead_letter(
        trigger, "qq_group:100000001", RuntimeError("ambiguous"),
        event_type="outbound_message", ambiguous_ack=True, idempotency_key="k1", task_id="t",
    ))
    assert len(sent) == 1
    assert sent[0][0]["group_id"] == 100000001
    assert sent[0][1] == runtime._DELIVERY_DEAD_LETTER_NOTICE
    assert cleared == [trigger.platform_data["event"]]

    # 中间回复死信：不提示。
    asyncio.run(callbacks.on_delivery_dead_letter(
        trigger, "qq_group:100000001", RuntimeError("x"),
        event_type="intermediate_reply", ambiguous_ack=False, idempotency_key="k2", task_id="t",
    ))
    assert len(sent) == 1


# ----------------------------------------------------------------------
#  4. 表情/图片与正文拆开发送
# ----------------------------------------------------------------------

def _patch_process_emojis(monkeypatch: pytest.MonkeyPatch, runtime, segments: list[dict[str, Any]]) -> None:
    async def fake_process(text, bot, bqbs, names, metadata, **kwargs):
        return [dict(seg) for seg in segments]

    async def fake_inline(items):
        return items, False

    monkeypatch.setattr(runtime, "process_emojis", fake_process)
    monkeypatch.setattr(runtime, "_inline_emoji_segments", fake_inline)
    monkeypatch.setattr(runtime, "_current_custom_face_names", lambda: [])
    monkeypatch.setattr(runtime, "_current_custom_face_metadata", lambda: {})


def test_split_text_sends_text_first_then_emoji_and_emoji_failure_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_process_emojis(monkeypatch, runtime, [
        {"type": "text", "content": "表情包测试呀 "},
        {"type": "image", "url": "https://gxh.vip.qq.com/club/item/parcel/item/abc.png", "emoji": True},
        {"type": "text", "content": "\n最新消息如下"},
    ])
    sent: list[Any] = []

    async def fake_send(bot, target, message, **kwargs):
        sent.append(message)
        if any(seg.get("type") == "image" for seg in message):
            raise runtime.ActionFailed(_ENOENT_TEXT)
        return f"mid-{len(sent)}"

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    monkeypatch.setattr(runtime.asyncio, "sleep", _no_sleep)

    target = {"type": "group", "group_id": 1, "reply_message_id": 9, "reply_sender_id": 10001}
    ok = asyncio.run(runtime._send_split_text(
        object(), target, "任意文本", send_context=DeliveryContext(event_id="evt", idempotency_key="id"),
    ))

    assert ok is True
    assert len(sent) == 3
    first_text, emoji_msg, second_text = sent
    # 引用与 @ 只挂在第一条正文上，后续正文仍保持在表情之后。
    assert [seg["type"] for seg in first_text] == ["reply", "at", "text", "text"]
    assert "".join(seg["data"].get("text", "") for seg in first_text if seg["type"] == "text") \
        == " 表情包测试呀"
    assert [seg["type"] for seg in emoji_msg] == ["image"]
    assert emoji_msg[0]["data"]["file"] == "https://gxh.vip.qq.com/club/item/parcel/item/abc.png"
    assert emoji_msg[0]["data"]["cache"] == "false"
    assert emoji_msg[0]["data"]["proxy"] == "false"
    assert emoji_msg[0]["data"]["sub_type"] == 1
    assert emoji_msg[0]["data"]["summary"] == "[表情]"
    assert "".join(seg["data"].get("text", "") for seg in second_text) == "最新消息如下"


def test_split_text_text_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_process_emojis(monkeypatch, runtime, [
        {"type": "text", "content": "正文"},
        {"type": "image", "url": "https://example/x.png", "emoji": True},
    ])
    sent: list[Any] = []

    async def fake_send(bot, target, message, **kwargs):
        sent.append(message)
        raise runtime.ActionFailed(_TIMEOUT_TEXT)

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    monkeypatch.setattr(runtime.asyncio, "sleep", _no_sleep)

    with pytest.raises(runtime.ActionFailed):
        asyncio.run(runtime._send_split_text(object(), {"type": "group", "group_id": 1}, "x"))
    # 正文失败即中止，不再继续发表情。
    assert len(sent) == 1


def test_split_text_emoji_only_part_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_process_emojis(monkeypatch, runtime, [
        {"type": "image", "url": "https://example/x.png", "emoji": True},
    ])

    async def fake_send(bot, target, message, **kwargs):
        raise runtime.ActionFailed(_ENOENT_TEXT)

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    monkeypatch.setattr(runtime.asyncio, "sleep", _no_sleep)

    with pytest.raises(runtime.ActionFailed):
        asyncio.run(runtime._send_split_text(object(), {"type": "group", "group_id": 1}, "x"))


def test_ordered_text_and_image_segments_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    groups = runtime._ordered_text_and_image_segments([
        {"type": "text", "content": "  "},
        {"type": "image", "url": "u1", "emoji": True},
        {"type": "image", "url": "", "emoji": True},
        {"type": "at", "qq": "1"},
        {"type": "text", "content": " hi"},
    ])
    assert [kind for kind, _segments in groups] == ["emoji:0", "text:0"]
    assert [[seg["type"] for seg in segments] for _kind, segments in groups] == [
        ["image"], ["at", "text"],
    ]


def test_inline_emoji_segments_uses_base64_for_local_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    source = tmp_path / "emoji.gif"
    source.write_bytes(b"GIF89a test emoji")

    segments, changed = asyncio.run(runtime._inline_emoji_segments([
        {"type": "image", "url": f"file://{source}", "emoji": True},
    ]))

    assert changed is True
    assert segments[0]["url"].startswith("base64://R0lGODlh")


async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
    return None


# ----------------------------------------------------------------------
#  5. 表情 URL 直发 + base64 兜底 + NapCat temp 不可写告警
# ----------------------------------------------------------------------

def test_split_text_emoji_url_first_then_base64_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_process_emojis(monkeypatch, runtime, [
        {"type": "text", "content": "正文"},
        {"type": "image", "url": "https://example/x.png", "emoji": True},
    ])
    inline_calls: list[Any] = []

    async def fake_inline(items):
        inline_calls.append(items)
        for item in items:
            item["url"] = "base64://QUJD"
        return items, True

    monkeypatch.setattr(runtime, "_inline_emoji_segments", fake_inline)
    sent: list[Any] = []

    async def fake_send(bot, target, message, **kwargs):
        sent.append(message)
        files = [seg["data"].get("file") for seg in message if seg.get("type") == "image"]
        if files and files[0].startswith("https://"):
            raise runtime.ActionFailed("download failed: http 403")
        return f"mid-{len(sent)}"

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    monkeypatch.setattr(runtime.asyncio, "sleep", _no_sleep)

    ok = asyncio.run(runtime._send_split_text(object(), {"type": "group", "group_id": 1}, "x"))

    assert ok is True
    # 正文 -> URL 直发失败 -> base64 重发成功；只在失败后才下载。
    assert len(sent) == 3
    assert sent[1][0]["data"]["file"] == "https://example/x.png"
    assert sent[2][0]["data"]["file"] == "base64://QUJD"
    assert sent[2][0]["data"]["sub_type"] == 1
    assert len(inline_calls) == 1


def test_split_text_emoji_temp_enoent_alerts_and_skips_base64(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    _patch_process_emojis(monkeypatch, runtime, [
        {"type": "text", "content": "正文"},
        {"type": "image", "url": "https://example/x.png", "emoji": True},
    ])
    inline_calls: list[Any] = []

    async def fake_inline(items):
        inline_calls.append(items)
        return items, True

    monkeypatch.setattr(runtime, "_inline_emoji_segments", fake_inline)
    sent: list[Any] = []

    async def fake_send(bot, target, message, **kwargs):
        sent.append(message)
        if any(seg.get("type") == "image" for seg in message):
            raise runtime.ActionFailed(_ENOENT_TEXT)
        return "mid"

    monkeypatch.setattr(runtime, "_send_qq_message", fake_send)
    monkeypatch.setattr(runtime.asyncio, "sleep", _no_sleep)

    with caplog.at_level("ERROR"):
        ok = asyncio.run(runtime._send_split_text(object(), {"type": "group", "group_id": 1}, "x"))

    assert ok is True
    assert len(sent) == 2
    assert inline_calls == []
    assert any("NapCat 数据目录不可写" in rec.getMessage() for rec in caplog.records)


def test_napcat_temp_enoent_path_walks_cause_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    inner = runtime.ActionFailed(_ENOENT_TEXT)
    outer = RuntimeError("OneBot send failed")
    outer.__cause__ = inner
    assert runtime._napcat_temp_enoent_path(outer) == "/app/.config/QQ/NapCat/temp/143d6a94"
    assert runtime._napcat_temp_enoent_path(RuntimeError("ENOENT: open '/tmp/x'")) == ""


def test_process_emojis_prefers_live_url_over_stale_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    calls: list[str] = []

    class FakeBot:
        async def call_api(self, api, **kwargs):
            calls.append(api)
            return [
                {"desc": "开心", "md5": "aaa", "resId": "r1", "url": "https://live/new.png"},
            ]

    metadata = [{"name": "开心", "base_name": "开心", "md5": "aaa", "url": "https://stale/old.png"}]
    segments = asyncio.run(runtime.process_emojis("哈[表情:开心]", FakeBot(), [], ["开心"], metadata))
    images = [seg for seg in segments if seg["type"] == "image"]
    assert images and images[0]["url"] == "https://live/new.png"
    assert calls == ["fetch_custom_face_detail"]


def test_process_emojis_falls_back_to_metadata_url_when_live_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)

    class FakeBot:
        async def call_api(self, api, **kwargs):
            raise RuntimeError("napcat down")

    metadata = [{"name": "开心", "base_name": "开心", "md5": "aaa", "url": "https://stale/old.png"}]
    segments = asyncio.run(runtime.process_emojis("[表情:开心]", FakeBot(), [], ["开心"], metadata))
    assert segments == [{"type": "image", "url": "https://stale/old.png", "emoji": True}]
