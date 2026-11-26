"""drawtools 阻断性缺陷回归：.env 读 key、预设切换、settings 原子写、nai_generate 输出合法 JSON。"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRAWTOOLS = ROOT / "tools" / "drawtools"


@pytest.fixture
def common(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sys.path.insert(0, str(DRAWTOOLS))
    sys.modules.pop("common", None)
    mod = importlib.import_module("common")
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(mod, "SETTINGS_PATH", tmp_path / "settings.yaml")
    monkeypatch.setattr(mod, "SETTINGS_EXAMPLE_PATH", DRAWTOOLS / "settings.example.yaml")
    try:
        yield mod
    finally:
        sys.modules.pop("common", None)
        sys.path.remove(str(DRAWTOOLS))


def test_api_key_falls_back_to_workspace_dotenv(common, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVELAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text('# c\nexport OTHER=1\nNOVELAI_API_KEY="pst-dotenv-value"\n', encoding="utf-8")
    assert common.resolve_api_key({"api": {}}) == "pst-dotenv-value"
    # settings.yaml 显式 key 优先；环境变量次之。
    assert common.resolve_api_key({"api": {"api_key": "explicit"}}) == "explicit"
    monkeypatch.setenv("NOVELAI_API_KEY", "from-env")
    assert common.resolve_api_key({"api": {}}) == "from-env"


def test_switch_unknown_preset_raises_and_known_preset_persists_atomically(common, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preset not found"):
        common.set_selected_preset("no-such-preset")
    assert not (tmp_path / "settings.yaml").exists()

    ids = [p["id"] for p in common.iter_presets()]
    assert len(ids) >= 2
    target = common.set_selected_preset(ids[1])
    assert target["id"] == ids[1]
    saved = common.load_settings()
    assert saved["params"]["selected_preset_id"] == ids[1]
    assert common.selected_preset(saved)["id"] == ids[1]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".settings.")]


def _run_nai_generate(tmp_path: Path, args: dict, fake_curl: str) -> dict:
    fake = tmp_path / "fake_curl.py"
    fake.write_text(fake_curl, encoding="utf-8")
    # 工具按模块目录定位 .env/settings，这里不碰仓库私有配置，用环境变量给假 key；
    # curl 用 NOVELAI_CURL_BIN 替换成假脚本，绝不碰网络。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NOVELAI_API_KEY": "pst-test",
           "NOVELAI_CURL_BIN": json.dumps([sys.executable, str(fake)])}
    proc = subprocess.run(
        [sys.executable, str(DRAWTOOLS / "nai_generate.py")], input=json.dumps(args), cwd=str(tmp_path),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert proc.stdout.strip(), proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


FAKE_CURL_NON_ZIP = """
import sys
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
open(out, "wb").write(b'{"statusCode":200,"message":"proxy ok but no image"}')
sys.stdout.write("200")
"""


def test_nai_generate_reports_non_zip_body_as_json_error(tmp_path: Path) -> None:
    out = _run_nai_generate(tmp_path, {"prompt": "1girl", "filename": "../../evil.png"}, FAKE_CURL_NON_ZIP)
    assert out["ok"] is False
    assert "unexpected body" in out["error"]
    assert not (tmp_path.parent / "evil.png").exists()


FAKE_CURL_PNG = """
import sys, struct, zlib
argv = sys.argv[1:]
out = argv[argv.index("-o") + 1]
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
png = b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\\x00\\x00\\x00\\x00")) + chunk(b"IEND", b"")
open(out, "wb").write(png)
sys.stdout.write("200")
"""


def test_nai_generate_accepts_raw_png_and_sanitizes_filename(tmp_path: Path) -> None:
    out = _run_nai_generate(tmp_path, {"prompt": "1girl", "filename": "..\\\\x/my pic?.png"}, FAKE_CURL_PNG)
    assert out["ok"] is True, out
    path = out["data"]["path"]
    assert path.startswith("data/attachments/novelai/")
    assert Path(path).name in {"my_pic.png", "my_pic_.png"}
    assert ".." not in path and "/x/" not in path
    assert (tmp_path / path).is_file()
