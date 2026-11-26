"""plugins/draw 角色库：清洗与启用判定、导入导出、录入单个角色（规则 / 写盘 / QQ 工具的管理员校验）。"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from plugins.draw import admin
from plugins.draw import characters as rules
from plugins.draw.character_tool import ADMIN_ONLY_MESSAGE, INPUT_SCHEMA, TOOL_NAME, save_draw_character
from toolbox.context import platform_is_admin_from_task_context

REPO_ROOT = Path(__file__).resolve().parents[1]


def _workspace(tmp_path: Path) -> Path:
    draw = tmp_path / "tools" / "drawtools"
    draw.mkdir(parents=True)
    (draw / "character_tags.example.yaml").write_text(yaml.safe_dump({"characters": [
        {"name": "艾丽丝", "aliases": ["小艾"], "type": "girl", "danbooru": "alice", "appearance": "blonde hair",
         "outfits": [{"name": "校服", "tags": "school uniform"}]}]}, allow_unicode=True), encoding="utf-8")
    return tmp_path


def _private(root: Path) -> Path:
    return root / "tools" / "drawtools" / "character_tags.yaml"


def _call(arguments: dict, *, root: Path, is_admin: object = True) -> dict:
    ctx = SimpleNamespace(workspace_root=root, platform_is_admin=is_admin)
    return asyncio.run(save_draw_character(arguments, ctx))


# ---------------------------------------------------------------- rules


def test_legacy_file_is_read_with_stable_ids_and_cleaning():
    legacy = {"characters": [
        {"name": "初音未来", "aliases": ["Miku", "miku", "初音未来", ""], "danbooru": "hatsune_miku",
         "appearance": "very  long hair,\n aqua hair", "outfits": [{"name": "默认", "tags": "x"}, {"name": "", "tags": ""}]},
        {"name": "初音未来"}, "junk", {"name": ""}]}
    first, second = rules.clean_library(legacy), rules.clean_library(legacy)
    assert first == second and rules.revision(first) == rules.revision(second)
    assert len(first["characters"]) == 1
    character = first["characters"][0]
    assert character["id"].startswith("char-") and character["enabled"] is True and character["type"] == "girl"
    assert character["aliases"] == ["Miku"] and character["appearance"] == "very long hair, aqua hair"
    assert character["outfits"] == [{"name": "默认", "tags": "x", "enabled": True}]


@pytest.mark.parametrize("bad", [
    {"characters": [{"name": "x"}, {"name": "X"}]},
    {"characters": [{"name": ""}]},
    {"characters": "no"},
    {"groups": [{"name": rules.UNGROUPED_NAME}]},
    {"groups": [{"name": "a"}, {"name": "A"}]},
    {"groups": [{"name": "x" * (rules.MAX_GROUP_NAME_LENGTH + 1)}]},
])
def test_strict_cleaning_rejects_bad_libraries(bad):
    with pytest.raises(rules.CharacterError):
        rules.clean_library(bad, strict=True)


def test_only_effectively_enabled_characters_and_items_are_active():
    library = rules.clean_library({
        "groups": [{"id": "g1", "name": "主角团", "enabled": False}, {"id": "g2", "name": "配角"}],
        "characters": [
            {"id": "c1", "name": "A", "group_id": "g1"},
            {"id": "c2", "name": "B", "group_id": "g2",
             "outfits": [{"name": "o1", "tags": "t1"}, {"name": "o2", "tags": "t2", "enabled": False}],
             "dynamic_states": [{"name": "d1", "tags": "s1", "enabled": False}, {"name": "d2", "tags": "s2"}]},
            {"id": "c3", "name": "C", "enabled": False},
            {"id": "c4", "name": "D", "group_id": "gone", "negative_tags": "beard"}]}, strict=True)
    assert library["characters"][3]["group_id"] == ""
    active = rules.active_characters(library)
    assert [c["name"] for c in active] == ["B", "D"]
    assert active[0]["outfits"] == [{"name": "o1", "tags": "t1"}] and active[0]["dynamic_states"] == [{"name": "d2", "tags": "s2"}]
    assert active[1]["negative_tags"] == "beard" and "id" not in active[0] and "enabled" not in active[0]


def test_export_then_import_roundtrip_and_conflict_modes():
    library = rules.clean_library({
        "groups": [{"id": "g1", "name": "主角团"}],
        "characters": [
            {"id": "c1", "name": "A", "group_id": "g1", "appearance": "red hair", "negative_tags": "hat", "danbooru": "a_tag",
             "outfits": [{"name": "o1", "tags": "t1", "enabled": False}], "dynamic_states": [{"name": "d1", "tags": "s1"}]},
            {"id": "c2", "name": "B", "enabled": False}]}, strict=True)
    exported = rules.export_library(library)
    data = exported["data"]
    assert exported["filename"] == "character-tags.json" and data["type"] == "novel-draw-characters" and data["version"] == 4
    first = data["characters"][0]
    assert first["negativeTags"] == "hat" and first["danbooruTag"] == "a_tag" and first["groupId"] == "g1"
    assert first["dynamicStates"] == [{"name": "d1", "tags": "s1"}] and "enabled" not in first

    # 导进空库：角色、分组都建出来；as_disabled 让新角色先停用。
    fresh = rules.merge_import({}, data, as_disabled=True)
    assert fresh["summary"] == {"added": 2, "replaced": 0, "skipped": 0, "new_groups": 1}
    assert [g["name"] for g in fresh["library"]["groups"]] == ["主角团"]
    assert all(c["enabled"] is False for c in fresh["library"]["characters"])

    # 导回原库：replace 覆盖内容，本机的启用状态（角色、条目）不动；skip 不动。
    data["characters"][0]["appearance"] = "blue hair"
    replaced = rules.merge_import(library, data)
    assert replaced["summary"]["replaced"] == 2 and replaced["summary"]["added"] == 0
    a, b = replaced["library"]["characters"]
    assert a["appearance"] == "blue hair" and a["outfits"] == [{"name": "o1", "tags": "t1", "enabled": False}] and b["enabled"] is False
    skipped = rules.merge_import(library, data, conflict="skip")
    assert skipped["summary"]["skipped"] == 2 and skipped["library"]["characters"][0]["appearance"] == "red hair"

    assert rules.export_library(library, scope="group", group_id="g1")["data"]["characters"][0]["name"] == "A"
    assert [c["name"] for c in rules.export_library(library, scope="selection", ids=["c2"])["data"]["characters"]] == ["B"]
    for kwargs in ({"scope": "group", "group_id": "nope"}, {"scope": "selection", "ids": []}, {"scope": "weird"}):
        with pytest.raises(rules.CharacterError):
            rules.export_library(library, **kwargs)
    with pytest.raises(rules.CharacterError):
        rules.merge_import(library, {"type": "something-else", "characters": []})


def test_upsert_creates_then_refuses_then_updates_only_given_fields():
    library = {"characters": [{"id": "c1", "name": "艾丽丝", "aliases": ["小艾"], "appearance": "blonde hair", "danbooru": "alice",
                               "enabled": False, "outfits": [{"name": "校服", "tags": "school uniform", "enabled": False}]}]}
    created = rules.upsert_character(library, {"name": "新人", "appearance": "black  hair", "aliases": "阿新, 新新"})
    assert created["created"] is True and created["active"] is True
    assert created["character"]["appearance"] == "black hair" and created["character"]["aliases"] == ["阿新", "新新"]
    assert [c["name"] for c in created["library"]["characters"]] == ["艾丽丝", "新人"]

    # 已有角色：按别名、不分大小写也认得出；默认不改动，并带回现有内容。
    with pytest.raises(rules.CharacterExists) as exists:
        rules.upsert_character(library, {"name": "小艾", "appearance": "pink hair"})
    assert exists.value.character["name"] == "艾丽丝" and exists.value.character["appearance"] == "blonde hair"

    updated = rules.upsert_character(library, {
        "name": "小艾", "aliases": ["Alice", "小艾"], "negative_tags": "hat",
        "outfits": [{"name": "校服", "tags": "serafuku"}, {"name": "泳装", "tags": "swimsuit"}]}, if_exists="update")
    character = updated["character"]
    assert updated["created"] is False and updated["active"] is False  # 本来就停用，录入不替管理员启用
    assert character["id"] == "c1" and character["name"] == "艾丽丝" and character["enabled"] is False
    assert character["aliases"] == ["小艾", "Alice"] and character["negative_tags"] == "hat"
    assert character["appearance"] == "blonde hair" and character["danbooru"] == "alice"  # 没给的字段不动
    assert character["outfits"] == [{"name": "校服", "tags": "serafuku", "enabled": False}, {"name": "泳装", "tags": "swimsuit", "enabled": True}]


def test_upsert_rejects_bad_input_and_alias_collisions():
    library = {"characters": [{"name": "艾丽丝", "aliases": ["小艾"]}, {"name": "鲍勃"}]}
    for fields, kwargs in (({"name": "  "}, {}), ({"name": "x"}, {"if_exists": "replace"}),
                           ({"name": "新人", "aliases": ["小艾"]}, {}),
                           ({"name": "鲍勃", "aliases": ["艾丽丝"]}, {"if_exists": "update"}),
                           ({"name": "新人"}, {"group_name": rules.UNGROUPED_NAME})):
        with pytest.raises(rules.CharacterError):
            rules.upsert_character(library, fields, **kwargs)


def test_upsert_puts_character_into_named_group():
    library = {"groups": [{"id": "g1", "name": "主角团", "enabled": False}], "characters": []}
    existing = rules.upsert_character(library, {"name": "A"}, group_name="主角团")
    assert existing["character"]["group_id"] == "g1" and existing["active"] is False and len(existing["library"]["groups"]) == 1
    fresh = rules.upsert_character(library, {"name": "B"}, group_name="配角")
    assert [g["name"] for g in fresh["library"]["groups"]] == ["主角团", "配角"]
    assert fresh["character"]["group_id"] == fresh["library"]["groups"][1]["id"] and fresh["active"] is True


# ---------------------------------------------------------------- writing


def test_record_character_writes_private_file_and_invalidates_panel_draft(tmp_path: Path):
    root = _workspace(tmp_path)
    draft = admin.get_character_library(root)
    assert draft["is_private"] is False and [c["name"] for c in draft["characters"]] == ["艾丽丝"]

    result = admin.record_character(root, {"name": "新人", "appearance": "black hair"})
    assert result["created"] is True and result["active"] is True
    on_disk = yaml.safe_load(_private(root).read_text(encoding="utf-8"))
    assert [c["name"] for c in on_disk["characters"]] == ["艾丽丝", "新人"]  # example 里原有的角色一起带进私有文件
    assert not list(_private(root).parent.glob(".*.lock"))

    # 面板还拿着录入前的草稿：保存被拒绝，录入的角色留在盘上。
    with pytest.raises(admin.AdminError) as stale:
        admin.save_character_library(root, {"characters": draft["characters"], "revision": draft["revision"]})
    assert stale.value.status == 409
    assert [c["name"] for c in admin.list_characters(root)] == ["艾丽丝", "新人"]

    with pytest.raises(admin.AdminError) as exists:
        admin.record_character(root, {"name": "小艾"})
    assert exists.value.status == 409 and exists.value.details["character"]["name"] == "艾丽丝"
    with pytest.raises(admin.AdminError) as bad:
        admin.record_character(root, {"name": ""})
    assert bad.value.status == 400


def test_character_file_lock_blocks_then_recovers_from_stale_lock(tmp_path: Path, monkeypatch):
    root = _workspace(tmp_path)
    monkeypatch.setattr(admin, "LOCK_TIMEOUT_SEC", 0.2)
    lock = _private(root).with_name(".character_tags.yaml.lock")
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(admin.AdminError) as busy:
        admin.record_character(root, {"name": "新人"})
    assert busy.value.status == 409 and not _private(root).exists()
    # 持有者崩溃留下的旧锁文件不会永远卡住写入。
    old = time.time() - admin.LOCK_STALE_SEC - 5
    os.utime(lock, (old, old))
    assert admin.record_character(root, {"name": "新人"})["created"] is True
    assert not lock.exists()


# ---------------------------------------------------------------- tool


@pytest.mark.parametrize("is_admin", [False, None, "true", 1])
def test_tool_refuses_non_admin_and_writes_nothing(tmp_path: Path, is_admin):
    root = _workspace(tmp_path)
    result = _call({"name": "新人", "appearance": "black hair"}, root=root, is_admin=is_admin)
    assert result["ok"] is False and result["error"] == ADMIN_ONLY_MESSAGE
    assert not _private(root).exists()


def test_tool_refuses_when_context_has_no_admin_flag(tmp_path: Path):
    root = _workspace(tmp_path)
    result = asyncio.run(save_draw_character({"name": "新人"}, SimpleNamespace(workspace_root=root)))
    assert result["ok"] is False and not _private(root).exists()


def test_tool_records_reports_existing_and_updates(tmp_path: Path):
    root = _workspace(tmp_path)
    created = _call({"name": "新人", "aliases": ["阿新"], "appearance": "black hair", "group": "配角",
                     "outfits": [{"name": "便服", "tags": "hoodie"}]}, root=root)
    assert created["ok"] is True and created["data"]["created"] is True
    assert "已录入角色「新人」" in created["data"]["result"] and "hoodie" in created["data"]["result"]
    library = admin.get_character_library(root)
    assert library["groups"][0]["name"] == "配角" and library["characters"][1]["group_id"] == library["groups"][0]["id"]

    # 已存在：不改动，把现有内容交给模型去问用户。
    existing = _call({"name": "小艾", "appearance": "pink hair"}, root=root)
    assert existing["ok"] is False and existing["data"]["character"]["name"] == "艾丽丝"
    assert "blonde hair" in existing["error"] and 'if_exists="update"' in existing["error"]
    assert admin.list_characters(root)[0]["appearance"] == "blonde hair"

    # 模型把没提到的可选字段填成空值：不能清掉已有内容。
    updated = _call({"name": "艾丽丝", "appearance": "", "aliases": [], "danbooru": "alice_liddell", "group": "",
                     "if_exists": "update"}, root=root)
    assert updated["ok"] is True and updated["data"]["created"] is False and "已更新角色「艾丽丝」" in updated["data"]["result"]
    alice = admin.list_characters(root)[0]
    assert alice["appearance"] == "blonde hair" and alice["aliases"] == ["小艾"] and alice["danbooru"] == "alice_liddell"
    assert alice["group_id"] == ""

    bad = _call({"name": ""}, root=root)
    assert bad["ok"] is False and "名称" in bad["error"]


def test_tool_warns_when_recorded_character_is_inactive(tmp_path: Path):
    root = _workspace(tmp_path)
    admin.save_character_library(root, {"groups": [{"id": "g1", "name": "停用组", "enabled": False}], "characters": []})
    result = _call({"name": "新人", "group": "停用组"}, root=root)
    assert result["ok"] is True and result["data"]["active"] is False and "停用" in result["data"]["result"]


# ---------------------------------------------------------------- wiring


def test_admin_flag_only_comes_from_explicit_adapter_identity():
    assert platform_is_admin_from_task_context({"platform_auth": {"platform": "qq", "user_id": "1", "is_admin": True}}) is True
    for task_context in (None, {}, {"platform_auth": None}, {"platform_auth": {"is_admin": False}},
                         {"platform_auth": {"is_admin": "true"}}, {"platform_auth": {"is_admin": 1}}, {"is_admin": True}):
        assert platform_is_admin_from_task_context(task_context) is False


def test_plugin_registers_tool_and_only_qq_orchestrator_allows_it():
    from engine.context import EngineContext
    from engine.faces.prompt_sections import PromptSectionRegistry
    from engine.faces.tool_visibility import ToolVisibilityRegistry
    from engine.hooks import HookRegistry
    from engine.hooks.loader import load_single_plugin

    class _Tools:
        def __init__(self):
            self.registered: dict[str, tuple] = {}

        def register_builtin_tool(self, name, description, input_schema, func, *, pure=False):
            self.registered[name] = (description, input_schema, func)
            return lambda: self.registered.pop(name, None)

    hooks = HookRegistry()
    ctx = EngineContext(hooks=hooks)
    tools = _Tools()
    ctx.contributions.mount("tool_visibility", ToolVisibilityRegistry())
    ctx.contributions.mount("prompt_sections", PromptSectionRegistry())
    ctx.contributions.mount("tools", tools)
    load_single_plugin(hooks, REPO_ROOT / "plugins", "draw", context=ctx, process="engine")
    description, schema, func = tools.registered[TOOL_NAME]
    # 加载器把插件包作为独立的模块对象导入，所以按名字和内容比较，不按对象身份比较。
    assert func.__name__ == save_draw_character.__name__ and inspect.iscoroutinefunction(func)
    assert schema == INPUT_SCHEMA and schema["required"] == ["name"] and "管理员" in description

    allowing = []
    for path in sorted((REPO_ROOT / "config" / "nodes").glob("*.yaml")):
        node = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        access = node.get("tool_access") if isinstance(node.get("tool_access"), dict) else {}
        if TOOL_NAME in (access.get("allow") or []):
            allowing.append(node.get("id"))
    assert allowing == ["qq.orchestrator"]


def test_tool_context_carries_admin_flag_through_registry(tmp_path: Path):
    """经真实 ToolRegistry 执行：管理员标记从 ToolContext 到工具函数，返回值是统一的 ok/data/error 形态。"""
    import sys

    from toolbox.context import ToolContext
    from toolbox.registry import ToolRegistry

    root = _workspace(tmp_path)
    saved_path = list(sys.path)
    saved_tools = {k: v for k, v in sys.modules.items() if k == "tools" or k.startswith("tools.")}
    try:
        registry = ToolRegistry(workspace_root=root, tools_dir=root / "tools")
        registry.register_builtin_tool(TOOL_NAME, "d", INPUT_SCHEMA, save_draw_character)

        def run(is_admin: bool) -> dict:
            ctx = ToolContext(supervisor_url="test", session_id="s", run_id="r", worker_id="w", workspace_root=root,
                              http=None, registry=registry, platform_user_id="42", platform_is_admin=is_admin)
            return asyncio.run(registry.execute(name=TOOL_NAME, arguments={"name": "新人"}, ctx=ctx, _authorized=True))

        refused = run(False)
        assert refused["ok"] is False and refused["data"]["result"].startswith("ERROR:") and not _private(root).exists()
        assert ToolContext(supervisor_url="t", session_id="s", run_id="r", worker_id="w", workspace_root=root,
                           http=None, registry=registry).platform_is_admin is False
        accepted = run(True)
        assert accepted["ok"] is True and [c["name"] for c in admin.list_characters(root)] == ["艾丽丝", "新人"]
    finally:
        sys.path[:] = saved_path
        for key in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            sys.modules.pop(key, None)
        sys.modules.update(saved_tools)
