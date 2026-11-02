"""Tests for QQ voice input/output helpers (no network)."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[1] / "adapters" / "onebot"
if "adapters.onebot" not in sys.modules:
    importlib.import_module("adapters")
    _pkg = types.ModuleType("adapters.onebot")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["adapters.onebot"] = _pkg

pytest.importorskip("nonebot.adapters.onebot.v11")
voice = importlib.import_module("adapters.onebot.voice")
config = importlib.import_module("adapters.onebot.config")

ANCHOR = voice.REPLY_ANCHOR


def _seg(type_, **data):
    return types.SimpleNamespace(type=type_, data=data)


def test_compose_user_text_modalities():
    assert voice.compose_user_text("你好", [], False) == ("你好", "你好", "text")
    text, hist, mod = voice.compose_user_text("[语音]", ["明天开会"], True)
    assert mod == "voice" and "【语音转写】\n明天开会" in text and hist == "[语音转写] 明天开会"
    text, hist, mod = voice.compose_user_text("看这个 [语音]", ["好的"], True)
    assert "【用户文字】\n看这个" in text and hist == "看这个 [语音转写] 好的"
    assert voice.compose_user_text("[语音]", [], True)[2] == "voice"


def test_reply_hint_inserted_before_anchor(monkeypatch):
    monkeypatch.setattr(config, "ONEBOT_VOICE_REPLY", True)
    monkeypatch.setattr(config, "ONEBOT_VOICE_REPLY_ON_VOICE_INPUT", True)
    out = voice.append_reply_modality_hint(f"上下文\n{ANCHOR}", "voice", is_dm=False)
    assert out.index("【QQ回复方式提示】") < out.index(ANCHOR)
    assert "群聊中应保持克制" in out
    monkeypatch.setattr(config, "ONEBOT_VOICE_REPLY", False)
    assert voice.append_reply_modality_hint("x", "voice", is_dm=True) == "x"


def test_voice_plan(monkeypatch):
    for k in ("ONEBOT_VOICE_REPLY", "ONEBOT_VOICE_REPLY_ON_VOICE_INPUT", "ONEBOT_VOICE_REPLY_AUTO_ON_TEXT",
              "ONEBOT_VOICE_REPLY_PRIVATE", "ONEBOT_VOICE_REPLY_GROUP"):
        monkeypatch.setattr(config, k, True)
    group = {"type": "group", "group_id": 1}
    assert voice.voice_plan("正文<tts>早点睡</tts>", group, "text") == ("正文早点睡", ["早点睡"])
    assert voice.voice_plan("纯文字", group, "text") == ("纯文字", [])
    assert voice.voice_plan("收到```code```", group, "voice") == ("收到```code```", ["收到"])
    monkeypatch.setattr(config, "ONEBOT_VOICE_REPLY_GROUP", False)
    assert voice.voice_plan("[[tts]]嗨[[/tts]]", group, "text") == ("嗨", [])


def test_transcribe_uses_napcat_mp3(monkeypatch, tmp_path):
    mp3 = tmp_path / "v.mp3"
    mp3.write_bytes(b"ID3data")
    posted = []

    class FakeResp:
        headers = {}

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, files=None, headers=None, **kw):
            posted.append((url, files))
            return FakeResp({"text": "你好世界"})

    class FakeBot:
        async def call_api(self, api, **kw):
            assert api == "get_record" and kw["out_format"] == "mp3"
            return {"file": str(mp3)}

    monkeypatch.setattr(voice.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(config, "ONEBOT_VOICE_INPUT", True)
    message = [_seg("text", text="hi"), _seg("record", file="abc.amr")]
    transcripts, had = asyncio.run(voice.transcribe_message(FakeBot(), message))
    assert had and transcripts == ["你好世界"]
    assert posted[0][0].endswith("/v1/audio/transcribe")
    assert posted[0][1]["file"] == ("voice.mp3", b"ID3data", "audio/mpeg")
    assert asyncio.run(voice.transcribe_message(FakeBot(), [_seg("text", text="x")])) == ([], False)
