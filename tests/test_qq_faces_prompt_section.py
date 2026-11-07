"""qq_faces 插件：可用表情列表按请求临时注入（不进聊天记录）。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.qq_faces import prompt as qf


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ONEBOT_CUSTOM_FACE_NAMES_PATH", "ONEBOT_CUSTOM_FACE_PROMPT_LIMIT", "CLONOTH_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)


def _write_names(root: Path, names: list[str]) -> None:
    path = root / "config" / "qq_custom_faces.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 注释\n\n" + "\n".join(names) + "\n", encoding="utf-8")


def _ctx(root: Path, task_context: dict) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=root, task_context=task_context)


QQ = {"route_hints": {"platform": "qq", "target_type": "group"}}


def test_renders_current_names_only_for_qq_tasks(tmp_path: Path) -> None:
    _write_names(tmp_path, ["开心", "难过", "开心"])
    block = qf.render(_ctx(tmp_path, QQ))
    assert block and "[表情:名称]" in block and "可用名称：开心、难过" in block
    assert qf.render(_ctx(tmp_path, {})) is None
    # dispatch 子任务只继承 platform_auth，不继承 route_hints。
    assert qf.render(_ctx(tmp_path, {"platform_auth": {"platform": "qq"}})) is None


def test_reads_file_on_every_render_so_renames_apply(tmp_path: Path) -> None:
    _write_names(tmp_path, ["旧名字"])
    assert "旧名字" in qf.render(_ctx(tmp_path, QQ))
    _write_names(tmp_path, ["新名字"])
    block = qf.render(_ctx(tmp_path, QQ))
    assert "新名字" in block and "旧名字" not in block


def test_limit_and_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert qf.render(_ctx(tmp_path, QQ)) is None
    _write_names(tmp_path, ["a", "b", "c"])
    monkeypatch.setenv("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT", "2")
    assert "可用名称：a、b（另有 1 个未展示）" in qf.render(_ctx(tmp_path, QQ))
    monkeypatch.setenv("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT", "0")
    assert qf.render(_ctx(tmp_path, QQ)) is None


def test_names_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "x.txt"
    custom.write_text("自定义\n", encoding="utf-8")
    monkeypatch.setenv("ONEBOT_CUSTOM_FACE_NAMES_PATH", str(custom))
    assert "自定义" in qf.render(_ctx(tmp_path / "other", QQ))


def test_section_is_dynamic_and_not_persisted(tmp_path: Path) -> None:
    from engine.faces.prompt_sections import PromptSectionRegistry, PromptSectionContext

    _write_names(tmp_path, ["开心"])
    registry = PromptSectionRegistry()
    registry.register_section(qf.SECTION_NAME, qf.render, order=60, scope="dynamic")
    ctx = PromptSectionContext(workspace_root=tmp_path, task_context=QQ)
    assert registry.render_scope("static", ctx) == []
    parts = registry.render_scope("dynamic", ctx)
    assert len(parts) == 1 and "开心" in parts[0]
