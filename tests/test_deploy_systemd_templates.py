"""deploy/systemd 模板：占位符、启动方式与无敏感信息。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = ROOT / "deploy" / "systemd"


def _units() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(UNIT_DIR.iterdir()) if p.is_file()}


def test_templates_exist_and_use_placeholder_only() -> None:
    units = _units()
    assert {"clonoth.service", "clonoth-onebot.service", "clonoth-data-cleanup.service", "clonoth-data-cleanup.timer"} <= set(units)
    for name, text in units.items():
        assert not re.search(r"/opt/|/www/|/root/|[A-Za-z]:\\\\", text), name
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text.replace("127.0.0.1", "")), name
        assert not re.search(r"\d{6,}", text), name
        if name.endswith(".service"):
            assert "@INSTALL_DIR@" in text, name


def test_onebot_unit_runs_adapter_as_script_after_supervisor() -> None:
    text = _units()["clonoth-onebot.service"]
    assert "adapters/onebot/app.py" in text
    assert " -m adapters" not in text
    assert "Requires=clonoth.service" in text
    assert "/v1/health" in text


def test_cleanup_timer_runs_hourly() -> None:
    text = _units()["clonoth-data-cleanup.timer"]
    assert "OnUnitActiveSec=1h" in text
    assert "-m engine.data_cleanup" in _units()["clonoth-data-cleanup.service"]
