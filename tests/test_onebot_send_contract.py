from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from clonoth_sdk.types import DeliveryContext

_MODULE_PATH = Path(__file__).resolve().parents[1] / "platform" / "onebot" / "send_contract.py"
_SPEC = importlib.util.spec_from_file_location("_onebot_send_contract", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_EMOJI_MODULE_PATH = Path(__file__).resolve().parents[1] / "platform" / "onebot" / "emoji_handler.py"
_EMOJI_SPEC = importlib.util.spec_from_file_location("_onebot_emoji_handler", _EMOJI_MODULE_PATH)
assert _EMOJI_SPEC is not None and _EMOJI_SPEC.loader is not None
_EMOJI_MODULE = importlib.util.module_from_spec(_EMOJI_SPEC)
sys.modules[_EMOJI_SPEC.name] = _EMOJI_MODULE
_EMOJI_SPEC.loader.exec_module(_EMOJI_MODULE)

from _onebot_emoji_handler import (  # type: ignore[import-not-found]  # noqa: E402
    process_emojis,
    strip_output_markers,
)
from _onebot_send_contract import (  # type: ignore[import-not-found]  # noqa: E402
    IdempotencyOwnershipError,
    OneBotAmbiguousAckError,
    OneBotSendContractError,
    OneBotSendNotStartedError,
    OutboundSendContext,
    TwoPhaseIdempotencyStore,
    classify_send_exception,
    context_from_sources,
    image_content_identity,
    make_idempotency_key,
    protected_claim_send,
    validate_send_request,
)


class _Bot:
    pass


def test_markdown_styles_are_preserved_by_default() -> None:
    source = (
        "抓到真凶了！你写成了 PUB_CACHE，正确叫法是 PUB_CACHE；"
        "同时保留 *斜体*、**粗体**、_下划线斜体_ 和 __下划线粗体__。"
    )

    assert strip_output_markers(source) == source

    segments = asyncio.run(process_emojis(source, _Bot(), []))
    assert "".join(str(item.get("content") or "") for item in segments) == source


def test_markdown_style_cleanup_can_be_explicitly_enabled() -> None:
    source = "*斜体*、**粗体**、_下划线斜体_、__下划线粗体__"

    assert strip_output_markers(source, strip_markdown_styles=True) == (
        "斜体、粗体、下划线斜体、下划线粗体"
    )


def test_markdown_style_cleanup_config_defaults_off_and_can_be_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _ROOT / "platform" / "onebot" / "config.py"

    def load_config(name: str):
        spec = importlib.util.spec_from_file_location(name, config_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.delenv("ONEBOT_STRIP_MARKDOWN_STYLES", raising=False)
    assert load_config("_onebot_config_markdown_default").STRIP_MARKDOWN_STYLES is False

    monkeypatch.setenv("ONEBOT_STRIP_MARKDOWN_STYLES", "1")
    assert load_config("_onebot_config_markdown_enabled").STRIP_MARKDOWN_STYLES is True


def test_missing_bot_and_target_are_explicit_contract_errors() -> None:
    with pytest.raises(OneBotSendContractError, match="missing bot"):
        validate_send_request(None, {"type": "group", "group_id": 1})
    with pytest.raises(OneBotSendContractError, match="missing target"):
        validate_send_request(_Bot(), None)
    with pytest.raises(OneBotSendContractError, match="missing group_id"):
        validate_send_request(_Bot(), {"type": "group"})


def test_two_phase_failure_releases_pending_and_success_commits_sent(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "claims.sqlite3")

        first = await store.begin("event-1")
        assert first.acquired is True
        assert await store.state("event-1") == "pending"

        concurrent = await store.begin("event-1")
        assert concurrent.acquired is False
        assert concurrent.state == "pending"

        await store.release(first)
        assert await store.state("event-1") is None

        retry = await store.begin("event-1")
        assert retry.acquired is True
        await store.commit(retry)
        assert await store.state("event-1") == "sent"

        replay = await store.begin("event-1")
        assert replay.acquired is False
        assert replay.state == "sent"

    asyncio.run(exercise())


def test_sent_state_survives_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "claims.sqlite3"
        first = TwoPhaseIdempotencyStore(path)
        claim = await first.begin("restart-key")
        await first.commit(claim)
        first.close()
        restarted = TwoPhaseIdempotencyStore(path)
        assert await restarted.state("restart-key") == "sent"
        duplicate = await restarted.begin("restart-key")
        assert duplicate.acquired is False and duplicate.state == "sent"

    asyncio.run(exercise())


def test_event_id_is_preferred_and_fallback_uses_target_and_content() -> None:
    target = {"type": "private", "user_id": 42}
    event_key_a = make_idempotency_key(target, "first body", event_id="outbound-7")
    event_key_b = make_idempotency_key(target, "changed body", event_id="outbound-7")
    assert event_key_a == event_key_b

    fallback_a = make_idempotency_key(target, "first body")
    fallback_b = make_idempotency_key(target, "changed body")
    assert fallback_a != fallback_b
    assert fallback_a != make_idempotency_key({"type": "private", "user_id": 43}, "first body")


def test_image_identity_uses_file_content_not_path_or_transport() -> None:
    payload = b"same image bytes"
    local_path_identity = image_content_identity(payload)
    base64_resend_identity = image_content_identity(payload)
    assert local_path_identity == base64_resend_identity
    assert local_path_identity.startswith("image:sha256:")


def test_send_exception_classification_distinguishes_retry_safety() -> None:
    retryable = classify_send_exception(RuntimeError("ENOENT: no such file or directory"))
    assert retryable.retryable is True

    ambiguous = classify_send_exception(RuntimeError("Timeout: NTEvent sendMsg"))
    assert ambiguous.retryable is False

    bad_request = classify_send_exception(RuntimeError("invalid group parameter"))
    assert bad_request.retryable is False


@dataclass
class _Trigger:
    inbound_seq: int = 12
    conversation_key: str = "qq_group:abc"
    task_id: str = "task-from-trigger"
    platform_data: dict = field(default_factory=lambda: {"legacy": True})


@dataclass
class _MainState:
    platform_data: dict = field(
        default_factory=lambda: {"task_id": "task-from-main-state"}
    )


def test_context_accepts_current_and_future_sdk_callback_sources() -> None:
    context = context_from_sources(
        trigger=_Trigger(),
        main_state=_MainState(),
        platform_data={"conversation_key": "qq_group:future"},
        event_data={
            "event_seq": 99,
            "event_id": "evt-99",
            "payload": {
                "task_id": "task-from-event",
                "source_inbound_seq": 12,
            },
        },
    )
    assert context == OutboundSendContext(
        event_seq=99,
        event_id="evt-99",
        task_id="task-from-event",
        source_inbound_seq=12,
        conversation_key="qq_group:future",
    )


def test_child_context_gives_each_message_under_one_event_a_stable_key() -> None:
    root = OutboundSendContext(event_id="evt-1")
    first = root.child("text:0:hello")
    same = root.child("text:0:hello")
    second = root.child("text:1:world")
    assert first.event_id == same.event_id
    assert first.event_id != second.event_id
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
    root = Path(__file__).resolve().parents[1] / "platform" / "onebot"
    name = f"_onebot_runtime_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_real_attachment_stack_awaits_all_and_aggregates_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    release = asyncio.Event()
    calls: list[str] = []

    async def send_path(bot, target, path, filename="", **kwargs):
        calls.append(path.name)
        if path.name == "first.png":
            await release.wait()
            raise ConnectionError("connection reset")
        return "message-2"

    monkeypatch.setattr(runtime, "_send_attachment_path", send_path)

    async def exercise() -> None:
        task = asyncio.create_task(runtime._send_text_and_attachments(
            object(), {"type": "group", "group_id": 1}, "",
            [{"path": str(tmp_path / "first.png")}, {"path": str(tmp_path / "second.png")}],
            send_context=DeliveryContext(event_id="evt-batch", idempotency_key="id:evt-batch"),
        ))
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(runtime.OneBotAttachmentBatchError):
            await task

    asyncio.run(exercise())
    assert calls == ["first.png", "second.png"]


def test_real_image_timeout_is_ambiguous_and_does_not_auto_resend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    image = tmp_path / "image.png"
    image.write_bytes(b"png payload")
    monkeypatch.setattr(runtime, "_IMAGE_TIMEOUT_RESEND_DELAY_SEC", 0)

    class Bot:
        self_id = "7"
        def __init__(self):
            self.files: list[str] = []
        async def send_group_msg(self, *, group_id: int, message: Any):
            file = message["data"]["file"] if isinstance(message, dict) else message[0]["data"]["file"]
            self.files.append(file)
            if len(self.files) == 1:
                raise RuntimeError("Timeout: NTEvent sendMsg")
            return {"message_id": 88}

    bot = Bot()
    context = DeliveryContext(event_id="evt-image", attempt=3, idempotency_key="id:evt-image")
    with pytest.raises(runtime.OneBotAmbiguousAckError):
        asyncio.run(runtime._send_attachment_path(
            bot, {"type": "group", "group_id": 1}, image, send_context=context,
        ))
    assert len(bot.files) == 1
    assert not bot.files[0].startswith("base64://")
    identity = runtime.image_content_identity(image.read_bytes())
    child = context.child(identity)
    key = runtime.make_idempotency_key(
        {"type": "group", "group_id": 1}, identity,
        event_id=child.idempotency_key or child.event_id,
    )
    assert asyncio.run(runtime._outbound_idempotency.state(key)) == "ambiguous"


def test_real_pending_owner_waits_and_forward_batch_is_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    gate = asyncio.Event()

    class Bot:
        self_id = "9"
        def __init__(self):
            self.send_count = 0
            self.forward_calls: list[tuple[str, dict[str, Any]]] = []
        async def send_group_msg(self, **kwargs):
            self.send_count += 1
            await gate.wait()
            return {"message_id": 9}
        async def call_api(self, name: str, **kwargs):
            self.forward_calls.append((name, kwargs))
            return {"message_id": 10}

    bot = Bot()
    target = {"type": "group", "group_id": 2}
    context = DeliveryContext(event_id="evt-owner")

    async def owner_conflict() -> None:
        first = asyncio.create_task(runtime._send_qq_message(
            bot, target, "same", send_context=context, content_identity="same",
        ))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(runtime._send_qq_message(
            bot, target, "same", send_context=context, content_identity="same",
        ))
        await asyncio.sleep(0.05)
        assert not second.done()
        gate.set()
        assert await first == "9"
        assert (await second).startswith("idempotent:")

    asyncio.run(owner_conflict())
    assert bot.send_count == 1

    first_image = tmp_path / "a.png"
    second_image = tmp_path / "b.png"
    first_image.write_bytes(b"a")
    second_image.write_bytes(b"b")
    monkeypatch.setattr(runtime, "ENABLE_IMAGE_FORWARD_MERGE", True)
    monkeypatch.setattr(runtime, "IMAGE_FORWARD_MERGE_THRESHOLD", 2)
    asyncio.run(runtime._send_attachments(
        bot, target, [{"path": str(first_image)}, {"path": str(second_image)}],
        send_context=DeliveryContext(event_id="evt-forward", idempotency_key="id:evt-forward"),
    ))
    assert [name for name, _ in bot.forward_calls] == ["send_group_forward_msg"]


def test_stale_idempotency_owner_cannot_commit_after_takeover(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "owners.sqlite3"
        first = TwoPhaseIdempotencyStore(path)
        second = TwoPhaseIdempotencyStore(path)
        stale = await first.begin("logical")
        first._db.execute("UPDATE claims SET lease_until=0 WHERE key='logical'")
        winner = await second.begin("logical")
        assert winner.acquired is True and winner.owner != stale.owner
        await second.commit(winner)
        with pytest.raises(IdempotencyOwnershipError):
            await first.commit(stale)
        assert await first.state("logical") == "sent"

    asyncio.run(exercise())


def test_image_all_fallback_failures_are_aggregated_under_same_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    image = tmp_path / "failure.png"
    image.write_bytes(b"failure image")
    monkeypatch.setattr(runtime, "_IMAGE_TIMEOUT_RESEND_DELAY_SEC", 0)
    begin_owners: list[str] = []
    original_begin = runtime._outbound_idempotency.begin

    async def recording_begin(key: str):
        claim = await original_begin(key)
        if claim.acquired:
            begin_owners.append(claim.owner)
        return claim

    monkeypatch.setattr(runtime._outbound_idempotency, "begin", recording_begin)

    class Bot:
        async def send_group_msg(self, **kwargs: Any):
            raise ConnectionError("connection reset")

    with pytest.raises(runtime.OneBotAttachmentBatchError) as raised:
        asyncio.run(runtime._send_attachment_path(
            Bot(), {"type": "group", "group_id": 3}, image,
            send_context=DeliveryContext(event_id="all-fail", idempotency_key="id:all-fail"),
        ))
    assert len(raised.value.errors) == 3
    assert len(begin_owners) == 1


def test_long_forward_heartbeat_prevents_cross_instance_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    runtime._outbound_idempotency.lease_seconds = 0.15
    first_image, second_image = tmp_path / "one.png", tmp_path / "two.png"
    first_image.write_bytes(b"one")
    second_image.write_bytes(b"two")

    class Bot:
        self_id = "1"
        async def call_api(self, name: str, **kwargs: Any):
            await asyncio.sleep(0.4)
            return {"message_id": 99}

    async def exercise() -> None:
        task = asyncio.create_task(runtime._try_send_images_as_forward(
            Bot(), {"type": "group", "group_id": 5},
            [{"path": str(first_image)}, {"path": str(second_image)}],
            send_context=DeliveryContext(event_id="long-forward", idempotency_key="id:long-forward"),
        ))
        await asyncio.sleep(0.25)
        row = runtime._outbound_idempotency._db.execute(
            "SELECT key,lease_until FROM claims WHERE state='pending'"
        ).fetchone()
        assert row is not None and row[1] > time.time()
        contender = TwoPhaseIdempotencyStore(runtime._outbound_idempotency.path)
        contender.lease_seconds = 0.15
        conflict = await contender.begin(row[0])
        assert conflict.acquired is False and conflict.state == "pending"
        assert await task is True

    asyncio.run(exercise())


def test_file_upload_crash_lease_replays_and_commits_content_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    attachment = tmp_path / "artifact.bin"
    attachment.write_bytes(b"artifact bytes")
    target = {"type": "group", "group_id": 8}
    context = DeliveryContext(event_id="file-event", idempotency_key="id:file-event")
    identity = f"file:sha256:{runtime.hashlib.sha256(attachment.read_bytes()).hexdigest()}"
    child = context.child(identity)
    key = runtime.make_idempotency_key(
        target, identity, event_id=child.idempotency_key or child.event_id,
    )

    async def abandoned_claim() -> None:
        claim = await runtime._outbound_idempotency.begin(key)
        assert claim.acquired
        runtime._outbound_idempotency._db.execute(
            "UPDATE claims SET lease_until=0 WHERE key=?", (key,),
        )

    asyncio.run(abandoned_claim())

    class Bot:
        def __init__(self):
            self.calls = 0
        async def call_api(self, api: str, **kwargs: Any):
            self.calls += 1
            assert api == "upload_group_file"
            assert kwargs["file"].startswith("base64://")
            return {"message_id": 123}

    bot = Bot()
    assert asyncio.run(runtime._send_attachment_path(
        bot, target, attachment, send_context=context,
    )) == "123"
    assert bot.calls == 1
    assert asyncio.run(runtime._outbound_idempotency.state(key)) == "sent"



def test_force_replay_context_bypasses_previous_onebot_sent_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)

    class Bot:
        def __init__(self):
            self.calls = 0
        async def send_group_msg(self, **kwargs: Any):
            self.calls += 1
            return {"message_id": self.calls}

    bot = Bot()
    target = {"type": "group", "group_id": 10}
    first = DeliveryContext(
        event_id="force-platform", idempotency_key="id:force-platform",
    )
    replay = DeliveryContext(
        event_id="force-platform", idempotency_key="id:force-platform:replay:1",
        replay_generation=1, force_replay=True,
    )
    assert asyncio.run(runtime._send_qq_message(
        bot, target, "body", send_context=first, content_identity="body",
    )) == "1"
    assert asyncio.run(runtime._send_qq_message(
        bot, target, "body", send_context=replay, content_identity="body",
    )) == "2"
    assert bot.calls == 2
    assert runtime._outbound_idempotency.pending_ttl > 240


def test_bridge_independent_identical_text_and_file_requests_are_not_permanently_deduped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    artifact = tmp_path / "same.bin"
    artifact.write_bytes(b"same file")

    class Bot:
        self_id = "42"
        def __init__(self):
            self.text_calls = 0
            self.file_calls = 0
        async def send_group_msg(self, **kwargs: Any):
            self.text_calls += 1
            return {"message_id": self.text_calls}
        async def call_api(self, api: str, **kwargs: Any):
            assert api == "upload_group_file"
            self.file_calls += 1
            return {"message_id": 100 + self.file_calls}

    bot = Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)

    async def resolve(*args: Any, **kwargs: Any):
        return runtime.ProactiveTarget("group", 99, "Group99"), ""

    async def emojis(text: str, *args: Any, **kwargs: Any):
        return [runtime.MessageSegment.text(text)]

    monkeypatch.setattr(runtime, "_forward_bridge_resolve_target", resolve)
    monkeypatch.setattr(runtime, "process_emojis", emojis)
    monkeypatch.setattr(
        runtime, "_forward_bridge_resolve_files",
        lambda *a, **k: ([{"path": str(artifact), "name": artifact.name}], []),
    )

    async def exercise() -> None:
        first_text = {"action": "remind", "target_type": "group", "target_ref": "x", "text": "same"}
        await runtime._forward_bridge_execute(first_text)
        await runtime._forward_bridge_execute(first_text)  # same request retry
        second_text = {"action": "remind", "target_type": "group", "target_ref": "x", "text": "same"}
        await runtime._forward_bridge_execute(second_text)
        assert first_text["_request_identity"] != second_text["_request_identity"]

        first_file = {
            "action": "file", "target_type": "group", "target_ref": "x",
            "file_paths": [str(artifact)],
        }
        await runtime._forward_bridge_execute(first_file)
        await runtime._forward_bridge_execute(first_file)  # same request retry
        second_file = {
            "action": "file", "target_type": "group", "target_ref": "x",
            "file_paths": [str(artifact)],
        }
        await runtime._forward_bridge_execute(second_file)
        assert first_file["_request_identity"] != second_file["_request_identity"]

    asyncio.run(exercise())
    assert bot.text_calls == 2
    assert bot.file_calls == 2


def test_sent_retention_and_bound_never_delete_valid_pending(tmp_path: Path) -> None:
    now = [1_000.0]
    store = TwoPhaseIdempotencyStore(
        tmp_path / "retention.sqlite3", sent_ttl=10.0, max_items=3,
        clock=lambda: now[0],
    )

    async def exercise() -> None:
        pending = await store.begin("pending-owner")
        assert pending.acquired
        for index in range(8):
            claim = await store.begin(f"sent-{index}")
            await store.commit(claim, sent_ttl=5.0)
        assert await store.state("pending-owner") == "pending"
        sent_count = store._db.execute(
            "SELECT COUNT(*) FROM claims WHERE state='sent'"
        ).fetchone()[0]
        assert sent_count <= 2  # one valid pending row occupies the configured bound
        now[0] += 6.0
        assert await store.state("pending-owner") == "pending"
        assert store._db.execute(
            "SELECT COUNT(*) FROM claims WHERE state='sent'"
        ).fetchone()[0] == 0

    asyncio.run(exercise())


def test_context_free_fallback_uses_short_sent_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    assert runtime._sent_ttl_for_context(DeliveryContext()) == runtime.ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS
    assert runtime._sent_ttl_for_context(
        DeliveryContext(idempotency_key="id:durable")
    ) is None


def test_bridge_http_handler_uses_public_request_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    captured: list[dict[str, Any]] = []

    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(dict(payload))
        return {"ok": True, "result": "sent"}

    monkeypatch.setattr(runtime, "_forward_bridge_execute", execute)

    class Request:
        def __init__(self, payload: dict[str, Any], headers: dict[str, str]):
            self._payload = payload
            self.headers = headers

        async def json(self) -> dict[str, Any]:
            return dict(self._payload)

    async def exercise() -> None:
        response = await runtime._forward_bridge_http_handler(Request(
            {
                "op": "remind",
                "request_id": "json-logical-request",
                "_request_identity": "spoofed-private-value",
            },
            {"Idempotency-Key": "header-logical-request"},
        ))
        assert response.status == 200

        response = await runtime._forward_bridge_http_handler(Request(
            {"op": "remind"},
            {"Idempotency-Key": "header-only-request"},
        ))
        assert response.status == 200

    asyncio.run(exercise())
    assert captured[0]["request_id"] == "json-logical-request"
    assert captured[0]["_request_identity"] == "json-logical-request"
    assert captured[1]["request_id"] == "header-only-request"
    assert captured[1]["_request_identity"] == "header-only-request"


def test_qq_forward_http_retry_reuses_identity_and_new_call_is_distinct() -> None:
    received: list[tuple[str, str, str, dict[str, Any]]] = []
    processed: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            request_id = str(payload["request_id"])
            received.append((
                request_id,
                self.headers.get("Idempotency-Key", ""),
                self.headers.get("X-Request-ID", ""),
                payload,
            ))
            if request_id not in processed:
                # Bridge completed the send, but its first response was lost.
                processed.add(request_id)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            response = json.dumps({"ok": True, "result": "sent"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env.update({
            "ONEBOT_FORWARD_BRIDGE_HOST": "127.0.0.1",
            "ONEBOT_FORWARD_BRIDGE_PORT": str(server.server_port),
            "ONEBOT_FORWARD_HTTP_ATTEMPTS": "2",
            "ONEBOT_FORWARD_HTTP_RETRY_DELAY": "0",
            "CLONOTH_SESSION_ID": "session-test",
            "CLONOTH_TASK_ID": "task-shared-by-both-logical-calls",
        })
        args = json.dumps({
            "op": "remind",
            "target_type": "self",
            "text": "identical content",
        })
        tool_path = _ROOT / "tools" / "qq_forward.py"

        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, str(tool_path)],
                input=args,
                text=True,
                capture_output=True,
                cwd=_ROOT,
                env=env,
                timeout=10,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr or completed.stdout
            assert json.loads(completed.stdout)["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(received) == 4
    first_id, second_id = received[0][0], received[2][0]
    assert first_id == received[1][0]
    assert second_id == received[3][0]
    assert first_id != second_id
    assert len(processed) == 2
    for request_id, idempotency_key, x_request_id, payload in received:
        assert idempotency_key == request_id
        assert x_request_id == request_id
        assert payload["request_id"] == request_id


def test_protected_send_waits_through_outer_cancellation_and_commits(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "shield.sqlite3")
        claim = await store.begin("event:shield")
        started, finish = asyncio.Event(), asyncio.Event()

        async def platform_send():
            started.set()
            await finish.wait()
            return {"message_id": 91}

        task = asyncio.create_task(protected_claim_send(
            store, claim, platform_send,
            message_id_getter=lambda result: str(result["message_id"]),
        ))
        await started.wait()
        task.cancel()
        finish.set()
        assert await task == {"message_id": 91}
        assert await store.state("event:shield") == "sent"

    asyncio.run(exercise())


def test_protected_send_cancel_during_commit_still_commits(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "commit-shield.sqlite3")
        claim = await store.begin("event:commit-shield")
        started, finish = asyncio.Event(), asyncio.Event()
        original_commit = store.commit

        async def delayed_commit(*args, **kwargs):
            started.set()
            await finish.wait()
            await original_commit(*args, **kwargs)

        store.commit = delayed_commit  # type: ignore[method-assign]
        task = asyncio.create_task(protected_claim_send(
            store, claim, lambda: asyncio.sleep(0, result={"message_id": 92}),
            message_id_getter=lambda result: str(result["message_id"]),
        ))
        await started.wait()
        task.cancel()
        finish.set()
        assert await task == {"message_id": 92}
        assert await store.state("event:commit-shield") == "sent"

    asyncio.run(exercise())


def test_onebot_explicit_not_started_failure_releases_claim(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "not-started.sqlite3")
        claim = await store.begin("event:not-started")

        async def platform_send():
            raise OneBotSendNotStartedError("cancelled before API call")

        with pytest.raises(OneBotSendNotStartedError):
            await protected_claim_send(store, claim, platform_send)
        assert await store.state("event:not-started") is None

    asyncio.run(exercise())


def test_onebot_internal_cancel_is_ambiguous_restart_and_force_generation(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "ambiguous.sqlite3"
        store = TwoPhaseIdempotencyStore(path)
        claim = await store.begin("event:cancel")

        async def platform_send():
            raise asyncio.CancelledError()

        with pytest.raises(OneBotAmbiguousAckError):
            await protected_claim_send(store, claim, platform_send)
        assert await store.state("event:cancel") == "ambiguous"
        store.close()

        restarted = TwoPhaseIdempotencyStore(path)
        with pytest.raises(OneBotAmbiguousAckError):
            await restarted.begin("event:cancel")
        replay = await restarted.begin("event:cancel:replay:1")
        assert await protected_claim_send(
            restarted, replay, lambda: asyncio.sleep(0, result="message-2"),
        ) == "message-2"
        assert await restarted.state("event:cancel:replay:1") == "sent"

    asyncio.run(exercise())


def test_onebot_commit_cancel_fault_marks_message_id_ambiguous(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "commit-cancel.sqlite3")
        claim = await store.begin("event:commit-cancel")

        async def cancelled_commit(*args, **kwargs):
            raise asyncio.CancelledError()

        store.commit = cancelled_commit  # type: ignore[method-assign]
        with pytest.raises(OneBotAmbiguousAckError):
            await protected_claim_send(
                store, claim, lambda: asyncio.sleep(0, result={"message_id": 93}),
                message_id_getter=lambda result: str(result["message_id"]),
            )
        row = store._db.execute(
            "SELECT state,platform_message_id FROM claims WHERE key='event:commit-cancel'"
        ).fetchone()
        assert row == ("ambiguous", "93")

    asyncio.run(exercise())
