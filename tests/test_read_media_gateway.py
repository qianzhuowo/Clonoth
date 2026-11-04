"""read_media: gateway-compatible request (explicit role) and configurable default model."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tool_read_media", ROOT / "tools" / "read_media.py")
read_media_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(read_media_mod)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def captured(monkeypatch, tmp_path):
    (tmp_path / "pic.png").write_bytes(_PNG)
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        payload = {"candidates": [{"content": {"parts": [{"text": "a red square"}]}}]}
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    class _Ctx(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(read_media_mod.urllib_request, "urlopen", lambda r, timeout=None: _Ctx(fake_urlopen(r).read()))
    for key in ("CLONOTH_VISION_API_KEY", "CLONOTH_VISION_BASE_URL", "GEMINI_API_KEY", "GEMINI_BASE_URL", "OPENAI_BASE_URL", "CLONOTH_VISION_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return tmp_path, seen


def test_request_contents_carry_user_role(captured):
    root, seen = captured
    out = read_media_mod.read_media({"path": "pic.png"}, root)
    assert out["ok"] and out["data"]["description"] == "a red square"
    assert seen["body"]["contents"][0]["role"] == "user"


def test_default_model_can_be_set_by_env(captured, monkeypatch):
    root, seen = captured
    monkeypatch.setenv("CLONOTH_VISION_MODEL", "gw/vision-model")
    out = read_media_mod.read_media({"path": "pic.png"}, root)
    assert out["data"]["model"] == "gw/vision-model"
    assert "/models/gw/vision-model:generateContent" in seen["url"]


def test_explicit_model_arg_wins_over_env(captured, monkeypatch):
    root, _ = captured
    monkeypatch.setenv("CLONOTH_VISION_MODEL", "gw/vision-model")
    out = read_media_mod.read_media({"path": "pic.png", "model": "other"}, root)
    assert out["data"]["model"] == "other"


def test_vision_slot_in_config_wins_over_env(captured, monkeypatch):
    root, seen = captured
    monkeypatch.setenv("CLONOTH_VISION_MODEL", "env-model")
    monkeypatch.setenv("SLOT_KEY", "slot-key")
    (root / "data").mkdir()
    (root / "data" / "config.yaml").write_text(
        "system_models:\n  vision:\n    model: LL/gemini-4-argon\n    base_url: https://gw.example/v1\n    api_key: ${SLOT_KEY}\n",
        encoding="utf-8",
    )
    out = read_media_mod.read_media({"path": "pic.png"}, root)
    assert out["data"]["model"] == "LL/gemini-4-argon"
    assert seen["url"].startswith("https://gw.example/v1beta/models/LL/gemini-4-argon:generateContent")
