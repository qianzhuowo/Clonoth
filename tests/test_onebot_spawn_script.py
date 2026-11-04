"""Integrated mode must launch the OneBot adapter as a script, not with -m."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from supervisor.process_manager import ProcessManager


def test_onebot_adapter_spawned_as_script(tmp_path):
    captured: dict = {}
    pm = ProcessManager.__new__(ProcessManager)
    pm.onebot_adapter = None
    pm.workspace_root = tmp_path
    pm.supervisor_url = "http://127.0.0.1:8765"
    for attr in (
        "onebot_host", "onebot_entry_node", "onebot_owner_id", "onebot_owner_name",
        "onebot_tts_format", "onebot_tts_voice", "onebot_tts_instruction",
    ):
        setattr(pm, attr, "")
    pm.onebot_port = 18080
    for attr in (
        "onebot_voice_input", "onebot_voice_reply", "onebot_voice_reply_on_voice_input",
        "onebot_voice_reply_auto_on_text", "onebot_voice_reply_private",
        "onebot_voice_reply_group", "onebot_delete_audio_after_use",
    ):
        setattr(pm, attr, True)

    def fake_spawn(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(popen=SimpleNamespace(poll=lambda: None))

    pm._spawn = fake_spawn
    pm.start_onebot_adapter()

    assert captured.get("module") is None
    assert Path(captured["script_path"]) == tmp_path / "adapters" / "onebot" / "app.py"
