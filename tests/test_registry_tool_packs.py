"""External tool registry: subdirectory tool packs and identity environment."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from toolbox.registry import ToolRegistry, _script_tool_env


def _tool(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"SPEC = {{'name': '{name}', 'description': 'd', "
        "'input_schema': {'type': 'object', 'properties': {}}}\n",
        encoding="utf-8",
    )


def test_registry_scans_tool_packs_and_skips_helpers(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    _tool(tools / "top_tool.py", "top_tool")
    _tool(tools / "pack" / "pack_entry.py", "pack_entry")
    _tool(tools / "pack" / "_private.py", "private_tool")
    _tool(tools / "pack" / "off.disabled.py", "disabled_tool")
    (tools / "pack" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (tools / "pack" / "__pycache__").mkdir()
    _tool(tools / "pack" / "__pycache__" / "cached.py", "cached_tool")

    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tools)
    names = {spec["name"] for spec in reg.list_specs()}
    assert {"top_tool", "pack_entry"} <= names
    assert not ({"private_tool", "disabled_tool", "cached_tool"} & names)


def test_script_env_carries_identity_and_drops_stale(monkeypatch) -> None:
    monkeypatch.setenv("CLONOTH_TOOL_CALL_ID", "stale-call")
    monkeypatch.setenv("CLONOTH_CONVERSATION_KEY", "stale-conv")
    ctx = SimpleNamespace(
        supervisor_url="http://127.0.0.1:8765", session_id="s1", parent_session_id="p1",
        task_id="t1", node_id="qq.orchestrator", conversation_key="qq_group:abc", tool_call_id="",
    )
    env = _script_tool_env(ctx)
    assert env["CLONOTH_SUPERVISOR_URL"] == "http://127.0.0.1:8765"
    assert env["CLONOTH_PARENT_SESSION_ID"] == "p1"
    assert env["CLONOTH_NODE_ID"] == "qq.orchestrator"
    assert env["CLONOTH_CONVERSATION_KEY"] == "qq_group:abc"
    assert "CLONOTH_TOOL_CALL_ID" not in env
