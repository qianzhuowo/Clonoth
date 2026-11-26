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


def _config(root: Path, kind: str, base_url: str, vision: dict | None = None) -> None:
    import yaml
    (root / "data").mkdir(exist_ok=True)
    data = {"active_channel": "main", "channels": [
        {"id": "main", "name": "主渠道", "type": kind, "base_url": base_url, "api_key": "test-key", "model": "main-model"},
        {"id": "gw", "name": "网关", "type": "gemini", "base_url": "https://gw.example/v1beta", "api_key": "${SLOT_KEY}", "model": "gw-model"},
    ]}
    if vision is not None:
        data["system_models"] = {"vision": vision}
    (root / "data" / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


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
    _config(tmp_path, "gemini", "https://gw.example")
    return tmp_path, seen


def test_request_contents_carry_user_role(captured):
    root, seen = captured
    out = read_media_mod.read_media({"path": "pic.png"}, root)
    assert out["ok"] and out["data"]["description"] == "a red square"
    assert seen["body"]["contents"][0]["role"] == "user"


def test_unset_vision_slot_uses_current_scheme(captured):
    root, seen = captured
    out = read_media_mod.read_media({"path": "pic.png"}, root)
    assert out["data"]["model"] == "main-model"
    assert seen["url"].startswith("https://gw.example/v1beta/models/main-model:generateContent")


def test_explicit_model_arg_wins(captured):
    root, _ = captured
    out = read_media_mod.read_media({"path": "pic.png", "model": "other"}, root)
    assert out["data"]["model"] == "other"


def test_vision_slot_picks_scheme_and_model(captured, monkeypatch):
    root, seen = captured
    monkeypatch.setenv("SLOT_KEY", "slot-key")
    _config(root, "gemini", "https://gw.example", vision={"channel": "gw", "model": "LL/gemini-4-argon"})
    out = read_media_mod.read_media({"path": "pic.png"}, root)
    assert out["data"]["model"] == "LL/gemini-4-argon"
    assert "/v1beta/v1beta/" not in seen["url"]
    assert "/models/LL/gemini-4-argon:generateContent" in seen["url"] and "key=slot-key" in seen["url"]


def test_non_gemini_scheme_is_rejected(captured):
    root, _ = captured
    _config(root, "openai", "https://x.test/v1")
    with pytest.raises(ValueError, match="Gemini"):
        read_media_mod.read_media({"path": "pic.png"}, root)
