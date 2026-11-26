"""绘图管理面板的数据层：settings / 预设 / 角色库 / 提示词模板 / 布局 / 图片，按 workspace_root 读写。

规则：
- 读：私有文件优先，缺失回退 *.example.*（plugins/draw/paths.drawtools_file）。
- 写：只写私有文件（settings.yaml / character_tags.yaml / prompts/novelai/<name>.md），原子替换；
  example 模板永不改动。“恢复默认”= 删除私有文件。
- API key 不回显：settings 视图只给 api_key_configured；保存时 api_key 留空表示不改，
  clear_api_key=true 才清空。
- 提示词模板里的 content 块（planner.content_template）保存时校验三个占位符齐全。
- 场景 Agent 预设（settings.yaml 的 agent 段）只经 get_agent_view / save_agent 读写，key 同样不回显：
  预设的 api_key 留空表示沿用已保存的，clear_api_key=true 才清空。
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

import clonoth_channels as chn
import clonoth_model_list

from . import characters as character_rules
from . import presets as preset_rules
from .agent import llm
from .agent.config import (DEFAULT_PLANNER_CFG, PROMPT_FILES, iter_presets, legacy_override_size, load_draw_settings,
                           resolve_api_key, selected_preset)
from .paths import DRAWTOOLS_REL, drawtools_file
from .planner.assembly import build_chain_preview
from .planner.frame import CONTENT_TEMPLATE, missing_content_placeholders
from .planner.layout import layout_for_storage, normalize_layout
from .planner.tool import create_submit_scene_plan_tool, default_field_descriptions, list_field_descriptions

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ATTACHMENTS_REL = Path("data") / "attachments" / "novelai"
SETTINGS_SECTIONS = ("api", "generation", "storage", "params", "llm", "planner", "character_tags")
MASKED_KEYS = {"api_key"}


class AdminError(ValueError):
    def __init__(self, message: str, *, status: int = 400, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details


# ---- file helpers ----

def _private_path(root: Path, rel: str) -> Path:
    return Path(root) / DRAWTOOLS_REL / rel


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


LOCK_TIMEOUT_SEC = 10.0
LOCK_STALE_SEC = 60.0


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """“读出来、改、写回去”期间独占一个文件。

    面板保存跑在 supervisor 进程，QQ 录入角色跑在 engine 进程，所以用锁文件（独占创建）而不是进程内的锁。
    持有者崩溃留下的锁文件过 LOCK_STALE_SEC 后视为作废。
    """
    lock = path.with_name(f".{path.name}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SEC
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError) as error:
            # Windows 上锁文件正在被删除时会报 PermissionError，等一下重试；其它系统上它表示目录不可写，直接报出来。
            if isinstance(error, PermissionError) and os.name != "nt":
                raise
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SEC:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise AdminError(f"{path.name} 正在被别处写入，请稍后再试", status=409)
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _read_yaml(root: Path, rel: str) -> dict[str, Any]:
    path = drawtools_file(root, rel)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AdminError(f"解析 {rel} 失败：{exc}", status=500)
    return data if isinstance(data, dict) else {}


def _write_yaml(root: Path, rel: str, data: Mapping[str, Any]) -> None:
    _atomic_write(_private_path(root, rel), yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False))


# ---- settings ----

def _mask(section: Any) -> Any:
    if not isinstance(section, Mapping):
        return section
    return {k: ("" if k in MASKED_KEYS else v) for k, v in section.items()}


def get_settings_view(root: Path) -> dict[str, Any]:
    """面板视图：合并默认后的 settings，api.api_key 不回显，附 api_key_configured / is_private。"""
    merged = load_draw_settings(Path(root))
    raw_private = _private_path(root, "settings.yaml").is_file()
    api = dict(merged.get("api") or {})
    configured = bool(resolve_api_key(merged, Path(root)))
    out = {k: (_mask(v) if k == "api" else v) for k, v in merged.items()}
    out["api"] = _mask(api)
    out["api_key_configured"] = configured
    out["is_private"] = raw_private
    return out


def update_settings(root: Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    """按段合并写入私有 settings.yaml；planner.content_template 校验占位符；layout 稀疏存储。"""
    root = Path(root)
    raw = _read_yaml(root, "settings.yaml")
    clear_key = bool(patch.get("clear_api_key"))
    for section in SETTINGS_SECTIONS:
        if section not in patch:
            continue
        incoming = patch[section]
        if not isinstance(incoming, Mapping):
            raise AdminError(f"{section} 必须是对象")
        current = dict(raw.get(section) or {}) if isinstance(raw.get(section), Mapping) else {}
        for key, value in incoming.items():
            if section == "api" and key in MASKED_KEYS:
                if isinstance(value, str) and value.strip():
                    current[key] = value.strip()
                continue
            current[key] = value
        if section == "planner":
            current.pop("recent_dialogue_turns", None)  # 已废弃，保存时从文件里去掉
            if "content_template" in current:
                template = str(current.get("content_template") or "")
                if template.strip():
                    missing = missing_content_placeholders(template)
                    if missing:
                        raise AdminError("content 模板缺少必需占位符：" + ", ".join(missing), details={"missing": missing})
                else:
                    current.pop("content_template", None)
            if "layout" in current:
                current["layout"] = layout_for_storage(current.get("layout"))
            if "field_descriptions" in current:
                fd = current.get("field_descriptions")
                current["field_descriptions"] = {str(k): str(v) for k, v in fd.items()} if isinstance(fd, Mapping) else {}
            current = {k: v for k, v in current.items() if v is not None or k == "layout"}
        raw[section] = current
    if clear_key:
        api = dict(raw.get("api") or {})
        api["api_key"] = ""
        raw["api"] = api
    _write_yaml(root, "settings.yaml", raw)
    return get_settings_view(root)


# ---- scene agent (planner model) ----

def _channel_choices(root: Path) -> list[dict[str, str]]:
    cfg = chn.load_config(root)
    return [{"id": str(c["id"]), "name": str(c.get("name") or c["id"]), "type": str(c.get("type") or ""),
             "model": str(c.get("model") or "")} for c in chn.channel_list(cfg)]


def get_agent_view(root: Path) -> dict[str, Any]:
    """场景 Agent 页的数据：预设、可选的渠道方案、当前生效的是预设还是节点自身的渠道。"""
    root = Path(root)
    agent = load_draw_settings(root)["agent"]
    current = llm.current_preset(agent)
    channels = _channel_choices(root)
    known = {c["id"] for c in channels}
    if not current["channel"]:
        effective = {"source": "node", "reason": "未选择渠道方案"}
    elif current["channel"] not in known:
        effective = {"source": "node", "reason": f"渠道方案「{current['channel']}」不存在"}
    else:
        effective = {"source": "preset"}
    return {
        "current_preset_id": agent["current_preset_id"],
        "presets": agent["presets"],
        "planner_max_retries": agent["planner_max_retries"],
        "channels": channels,
        "providers": llm.list_providers(),
        "reasoning_efforts": [e for e in llm.REASONING_EFFORTS if e],
        "effective": effective,
        "is_private": _private_path(root, "settings.yaml").is_file(),
    }


def save_agent(root: Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    """写入 agent 段。patch 里出现的键才改：presets（整表）、current_preset_id、planner_max_retries。"""
    root = Path(root)
    agent = load_draw_settings(root)["agent"]
    presets = agent["presets"]
    if "presets" in patch:
        incoming = patch.get("presets")
        if not isinstance(incoming, list) or not incoming:
            raise AdminError("至少要保留一套预设")
        if len(incoming) > llm.MAX_PRESETS:
            raise AdminError(f"预设最多 {llm.MAX_PRESETS} 套")
        known = {c["id"] for c in _channel_choices(root)}
        ids: set[str] = set()
        names: set[str] = set()
        presets = []
        for item in incoming:
            if not isinstance(item, Mapping):
                raise AdminError("预设必须是对象")
            name = str(item.get("name") or "").strip()
            if not name:
                raise AdminError("预设名称不能为空")
            if len(name) > llm.MAX_NAME_LENGTH:
                raise AdminError(f"预设名称最长 {llm.MAX_NAME_LENGTH} 个字符")
            if name in names:
                raise AdminError(f"已存在同名预设“{name}”")
            preset_id = str(item.get("id") or "").strip()
            if not preset_id:
                raise AdminError("预设缺少 id")
            if len(preset_id) > llm.MAX_NAME_LENGTH:
                raise AdminError("预设 id 过长")
            if preset_id in ids:
                raise AdminError(f"预设 id 重复：{preset_id}")
            preset = llm.normalize_preset(item)
            if preset["channel"] and preset["channel"] not in known:
                raise AdminError(f"渠道方案不存在：{preset['channel']}")
            ids.add(preset_id)
            names.add(name)
            presets.append(preset)
    preset_ids = {p["id"] for p in presets}
    current_id = agent["current_preset_id"]
    if "current_preset_id" in patch:
        current_id = str(patch.get("current_preset_id") or "").strip()
        if current_id not in preset_ids:
            raise AdminError(f"预设不存在：{current_id or '（空）'}")
    elif current_id not in preset_ids:
        current_id = presets[0]["id"]
    retries = agent["planner_max_retries"]
    if "planner_max_retries" in patch:
        value = patch.get("planner_max_retries")
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer()
        if not valid or not 0 <= int(value) <= llm.MAX_PLANNER_MAX_RETRIES:
            raise AdminError(f"失败重试次数须为 0–{llm.MAX_PLANNER_MAX_RETRIES} 的整数")
        retries = int(value)
    raw = _read_yaml(root, "settings.yaml")
    raw["agent"] = {"current_preset_id": current_id, "presets": presets, "planner_max_retries": retries}
    _write_yaml(root, "settings.yaml", raw)
    return get_agent_view(root)


def _agent_draft(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """“拉取模型 / 测试连接”用页面上还没保存的表单，渠道参数取自所选方案。"""
    item = body.get("preset")
    if not isinstance(item, Mapping):
        raise AdminError("缺少 preset")
    preset = llm.normalize_preset(item)
    if not preset["channel"]:
        raise AdminError("先选择一个渠道方案")
    try:
        return llm.resolve_preset(preset, root)
    except ValueError as error:
        raise AdminError(str(error))


async def pull_agent_models(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    preset = _agent_draft(root, body)
    try:
        models = await clonoth_model_list.pull_models(preset["provider"], preset["base_url"], preset["api_key"])
    except clonoth_model_list.ModelListError as error:
        raise AdminError(error.message, status=502, details={"status_code": error.status})
    return {"channel": preset["channel"], "models": models}


async def check_agent_connection(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    preset = _agent_draft(root, body)
    try:
        return await llm.check_connection(preset)
    except (ValueError, RuntimeError) as error:
        raise AdminError(str(error))


# ---- presets ----

def list_presets(root: Path) -> dict[str, Any]:
    """预设按出图时实际使用的样子给出（补齐默认值、清洗过），selected_preset_id 是实际生效的那一套。"""
    settings = load_draw_settings(Path(root))
    params = settings.get("params") or {}
    return {"selected_preset_id": str(selected_preset(settings)["id"]), "presets": iter_presets(settings),
            "size_options": dict(params.get("size_options") or {}), "options": preset_rules.panel_options()}


def save_presets(root: Path, presets: list[Mapping[str, Any]], selected_preset_id: str | None = None) -> dict[str, Any]:
    if not isinstance(presets, list) or not presets:
        raise AdminError("至少保留一个预设")
    legacy_size = legacy_override_size(load_draw_settings(Path(root)))
    ids: set[str] = set()
    seen: set[str] = set()  # 出图时按 id 找预设不分大小写
    cleaned: list[dict[str, Any]] = []
    for item in presets:
        try:
            preset = preset_rules.clean_preset(item, strict=True, legacy_size=legacy_size)
        except preset_rules.PresetError as error:
            raise AdminError(str(error))
        pid = preset["id"]
        if pid.lower() in seen:
            raise AdminError(f"预设 id 重复：{pid}")
        seen.add(pid.lower())
        ids.add(pid)
        cleaned.append(preset)
    selected = str(selected_preset_id or "").strip()
    raw = _read_yaml(Path(root), "settings.yaml")
    params = dict(raw.get("params") or {})
    if selected:
        if selected not in ids:
            raise AdminError(f"预设不存在：{selected}")
        params["selected_preset_id"] = selected
    elif str(params.get("selected_preset_id") or "") not in ids:
        params["selected_preset_id"] = cleaned[0]["id"]
    params["presets"] = cleaned
    raw["params"] = params
    _write_yaml(Path(root), "settings.yaml", raw)
    return list_presets(root)


_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def export_preset(body: Mapping[str, Any]) -> dict[str, Any]:
    """表单里的一套预设（不必已保存）→ 小白X 参数预设文件的内容和建议文件名。"""
    try:
        data = preset_rules.export_preset(body.get("preset"))
    except preset_rules.PresetError as error:
        raise AdminError(str(error))
    return {"filename": (_FILENAME_UNSAFE.sub("_", data["name"]).strip(" .") or "preset") + ".json", "data": data}


def import_preset(body: Mapping[str, Any]) -> dict[str, Any]:
    """小白X 参数预设文件的内容 → 一套新预设和导入说明。不写盘，由面板加进列表后一起保存。"""
    try:
        preset, notes = preset_rules.import_preset(body.get("data"))
    except preset_rules.PresetError as error:
        raise AdminError(str(error))
    return {"preset": preset, "notes": notes}


# ---- characters ----

def _character_source(root: Path, body: Mapping[str, Any] | None = None) -> Any:
    """面板带了草稿（characters 数组）就用草稿，否则用已保存的角色库。"""
    if body is not None and isinstance(body.get("characters"), list):
        return {"groups": body.get("groups"), "characters": body.get("characters")}
    return _read_yaml(Path(root), "character_tags.yaml")


def get_character_library(root: Path) -> dict[str, Any]:
    """面板视图：清洗后的分组和角色，加上内容指纹 revision（保存时带回）。"""
    library = character_rules.clean_library(_read_yaml(Path(root), "character_tags.yaml"))
    return {
        **library,
        "revision": character_rules.revision(library),
        "is_private": _private_path(root, "character_tags.yaml").is_file(),
        "type_presets": list(character_rules.TYPE_PRESETS),
        "limits": {"groups": character_rules.MAX_GROUPS, "group_name": character_rules.MAX_GROUP_NAME_LENGTH},
    }


def list_characters(root: Path) -> list[dict[str, Any]]:
    return get_character_library(root)["characters"]


def save_character_library(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """整表保存分组和角色。body.revision 和盘上的对不上（别处改过，比如 QQ 里录入了角色）就拒绝，不覆盖。"""
    root = Path(root)
    if not isinstance(body.get("characters"), list):
        raise AdminError("characters 必须是数组")
    with _file_lock(_private_path(root, "character_tags.yaml")):
        raw = _read_yaml(root, "character_tags.yaml")
        current = character_rules.clean_library(raw)
        expected = str(body.get("revision") or "").strip()
        if expected and expected != character_rules.revision(current):
            raise AdminError("角色库已经在别处改过（比如 QQ 里录入了角色），这次没有保存。请刷新页面后再改。", status=409)
        groups = body["groups"] if "groups" in body else current["groups"]
        try:
            library = character_rules.clean_library({"groups": groups, "characters": body["characters"]}, strict=True)
        except character_rules.CharacterError as error:
            raise AdminError(str(error))
        raw["groups"], raw["characters"] = library["groups"], library["characters"]
        _write_yaml(root, "character_tags.yaml", raw)
    return get_character_library(root)


def record_character(root: Path, fields: Mapping[str, Any], *, if_exists: str = "error", group: str = "") -> dict[str, Any]:
    """录入一个角色并写盘（QQ 里的 save_draw_character 工具用），返回 {character, created, active}。

    和面板保存用同一把文件锁；写盘后库的 revision 变了，面板上还没保存的旧草稿会被 409 挡住，不会把这次录入冲掉。
    角色已存在而 if_exists 不是 update 时报 409，details.character 是库里现有的那一条。
    """
    root = Path(root)
    with _file_lock(_private_path(root, "character_tags.yaml")):
        raw = _read_yaml(root, "character_tags.yaml")
        try:
            result = character_rules.upsert_character(raw, fields, if_exists=if_exists, group_name=group)
        except character_rules.CharacterExists as error:
            raise AdminError(str(error), status=409, details={"character": error.character})
        except character_rules.CharacterError as error:
            raise AdminError(str(error))
        raw["groups"], raw["characters"] = result["library"]["groups"], result["library"]["characters"]
        _write_yaml(root, "character_tags.yaml", raw)
    return {"character": result["character"], "created": result["created"], "active": result["active"]}


def export_characters(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """导出成小白X 能导入的角色标签文件：{filename, data}。scope：all / group / selection。"""
    try:
        return character_rules.export_library(
            _character_source(root, body), scope=str(body.get("scope") or "all"),
            group_id=str(body.get("group_id") or ""), ids=body.get("ids"))
    except character_rules.CharacterError as error:
        raise AdminError(str(error))


def import_characters(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """把角色标签文件并进面板草稿（或已保存的角色库），返回合并结果和说明。不写盘，面板保存后才生效。"""
    try:
        merged = character_rules.merge_import(
            _character_source(root, body), body.get("data"),
            conflict=str(body.get("conflict") or "replace"), as_disabled=bool(body.get("as_disabled")))
    except character_rules.CharacterError as error:
        raise AdminError(str(error))
    return {**merged["library"], "summary": merged["summary"], "notes": merged["notes"]}


# ---- prompts ----

def _example_text(root: Path, rel: str) -> str:
    private = _private_path(root, rel)
    example = private.with_name(f"{private.stem}.example{private.suffix}")
    return example.read_text(encoding="utf-8") if example.is_file() else ""


def get_prompts(root: Path) -> dict[str, Any]:
    """{opening|guide|guide_v5|rules: {content, default, is_default}, content: {...同形, default=CONTENT_TEMPLATE},
    guide_key: 当前默认预设实际用的指南键（guide / guide_v5）}"""
    from .agent.config import guide_key_for_preset

    root = Path(root)
    out: dict[str, Any] = {}
    for key, rel in PROMPT_FILES.items():
        private = _private_path(root, rel)
        default = _example_text(root, rel)
        content = private.read_text(encoding="utf-8") if private.is_file() else default
        out[key] = {"content": content, "default": default, "is_default": not private.is_file()}
    settings = load_draw_settings(root)
    planner = settings.get("planner") or {}
    custom = str(planner.get("content_template") or "")
    out["content"] = {"content": custom or CONTENT_TEMPLATE, "default": CONTENT_TEMPLATE, "is_default": not custom.strip()}
    out["guide_key"] = guide_key_for_preset(selected_preset(settings))
    return out


def save_prompt(root: Path, key: str, content: str) -> dict[str, Any]:
    root = Path(root)
    if key == "content":
        update_settings(root, {"planner": {"content_template": content}})
        return get_prompts(root)["content"]
    rel = PROMPT_FILES.get(key)
    if rel is None:
        raise AdminError(f"未知模板：{key}", status=404)
    if not str(content or "").strip():
        raise AdminError("模板内容不能为空；要恢复默认请用恢复默认")
    _atomic_write(_private_path(root, rel), str(content))
    return get_prompts(root)[key]


def reset_prompt(root: Path, key: str) -> dict[str, Any]:
    root = Path(root)
    if key == "content":
        update_settings(root, {"planner": {"content_template": ""}})
        return get_prompts(root)["content"]
    rel = PROMPT_FILES.get(key)
    if rel is None:
        raise AdminError(f"未知模板：{key}", status=404)
    _private_path(root, rel).unlink(missing_ok=True)
    return get_prompts(root)[key]


# ---- layout / fields / preview ----

def get_layout(root: Path) -> dict[str, Any]:
    planner = load_draw_settings(Path(root)).get("planner") or {}
    normalized = normalize_layout(planner.get("layout"))
    return {"layout": normalized.layout, "notes": list(getattr(normalized, "notes", []) or []),
            "is_default": layout_for_storage(planner.get("layout")) is None}


def save_layout(root: Path, layout: Any) -> dict[str, Any]:
    update_settings(Path(root), {"planner": {"layout": layout}})
    return get_layout(root)


def get_field_descriptions(root: Path) -> dict[str, Any]:
    from .agent.config import load_planner_config

    planner = load_draw_settings(Path(root)).get("planner") or {}
    overrides = planner.get("field_descriptions") if isinstance(planner.get("field_descriptions"), Mapping) else {}
    profile = load_planner_config(Path(root)).profile
    defaults = default_field_descriptions(profile)
    tool = create_submit_scene_plan_tool(profile=profile, descriptions=overrides or None)
    return {"fields": list_field_descriptions(tool), "defaults": defaults, "overrides": dict(overrides)}


def preview_chain(root: Path, *, layout: Any = None, texts_override: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    from .agent.config import guide_key_for_preset, load_planner_config
    from .planner.frame import build_frame_text, build_guide_section, build_limits_line, build_rules_section, build_content_block

    cfg = load_planner_config(Path(root))
    planner = cfg.planner
    prompts = get_prompts(root)
    override = dict(texts_override or {})
    opening = override.get("opening", prompts["opening"]["content"])
    # 预览用当前默认预设对应的那份指南（V5 模型是 guide_v5）；面板的草稿统一用 guide 键传，指的就是当前生效的那份。
    guide_key = guide_key_for_preset(cfg.preset)
    guide = override.get("guide", override.get(guide_key, prompts[guide_key]["content"]))
    rules = override.get("rules", prompts["rules"]["content"])
    content_tpl = override.get("content", prompts["content"]["content"])
    max_chars = preset_rules.effective_max_characters(cfg.preset)
    texts = {
        "opening": opening,
        "frame": build_frame_text(cfg.profile),
        "content": build_content_block(content_tpl, character_info="", draw_request="{{drawRequest}}"),
        "guide": build_guide_section(guide, cfg.profile),
        "rules": build_rules_section(rules),
        "limits": build_limits_line(cfg.preset.get("max_images", 0), max_chars, planner.get("max_plan_images", 0)),
    }
    tool = create_submit_scene_plan_tool(
        profile=cfg.profile, descriptions=planner.get("field_descriptions") or None,
        max_images=cfg.preset.get("max_images", 0), max_plan_images=planner.get("max_plan_images", 0),
        max_characters_per_image=max_chars,
    )
    return build_chain_preview(layout if layout is not None else planner.get("layout"), texts,
                               tool=tool, field_descriptions=list_field_descriptions(tool))


# ---- images ----

def list_images(root: Path, *, limit: int = 200) -> dict[str, Any]:
    root = Path(root)
    folder = root / ATTACHMENTS_REL
    items: list[dict[str, Any]] = []
    total = 0
    if folder.is_dir():
        for p in folder.iterdir():
            if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            st = p.stat()
            total += st.st_size
            items.append({"name": p.name, "path": (ATTACHMENTS_REL / p.name).as_posix(),
                          "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"images": items[:limit], "count": len(items), "total_bytes": total}


def delete_images(root: Path, names: list[str]) -> dict[str, Any]:
    root = Path(root)
    folder = (root / ATTACHMENTS_REL).resolve()
    deleted: list[str] = []
    for raw in names or []:
        name = Path(str(raw).replace("\\", "/")).name
        target = (folder / name).resolve()
        if target.parent != folder or target.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        try:
            target.unlink()
            deleted.append(name)
        except FileNotFoundError:
            continue
    return {"deleted": deleted}


def cleanup_images(root: Path) -> dict[str, Any]:
    """按 storage 策略清理：先过期，再按容量从最旧删。"""
    settings = load_draw_settings(Path(root))
    storage = settings.get("storage") or {}
    folder = Path(root) / ATTACHMENTS_REL
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES] if folder.is_dir() else []
    retention = float(storage.get("retention_days", 7) or 0)
    max_bytes = float(storage.get("max_total_mb", 2048) or 0) * 1024 * 1024
    now = time.time()
    deleted: list[str] = []

    def rm(p: Path) -> int:
        size = p.stat().st_size
        p.unlink(missing_ok=True)
        deleted.append(p.name)
        return size

    if retention > 0:
        cutoff = now - retention * 86400
        for p in list(files):
            if p.stat().st_mtime < cutoff:
                rm(p)
                files.remove(p)
    if max_bytes > 0:
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > max_bytes:
            total -= rm(files.pop(0))
    return {"deleted": deleted, "deleted_count": len(deleted)}


# ---- quick test ----

async def quick_test(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """面板快速测试：直接用一条场景 + 可选角色生成一张图，不经过规划模型。"""
    from .agent.config import load_planner_config
    from .backends import novelai

    cfg = load_planner_config(Path(root))
    scene = str(body.get("scene") or "").strip()
    if not scene:
        raise AdminError("scene 不能为空")
    chars = body.get("characters") if isinstance(body.get("characters"), list) else []
    task = {"index": 1, "title": str(body.get("title") or "快速测试")[:14], "orientation": body.get("orientation") or "portrait",
            "scene": scene, "chars": [c for c in chars if isinstance(c, Mapping)], "seed": body.get("seed")}
    preset = cfg.preset_for(body.get("preset"))
    # 没选尺寸覆盖、没填 seed 时都按预设来。
    compiled = novelai.compile_task(task, preset=preset, known_characters=cfg.characters,
                                    seed=body.get("seed"), override_size=body.get("override_size"))
    if body.get("dry_run"):
        return {"ok": True, "dry_run": True, "payload": compiled["payload"], "prompt": compiled["prompt"],
                "negative_prompt": compiled["negative_prompt"], "character_prompts": compiled["character_prompts"]}
    try:
        image = await novelai.generate_image(compiled, api_key=cfg.api_key, base_url=cfg.base_url,
                                             workspace_root=Path(root), settings=cfg.settings)
    except novelai.NovelAIError as error:
        raise AdminError(error.message, status=502, details={"code": error.code, "status_code": error.status_code})
    return {"ok": True, "path": image["path"], "seed": image["seed"], "width": image["width"], "height": image["height"],
            "prompt": image["prompt"], "negative_prompt": image["negative_prompt"], "character_prompts": image["character_prompts"]}
