"""角色库规则：清洗、启用判定、导入导出（对齐小白X shared/character-library.js；多形态留到第二期）。

存储（tools/drawtools/character_tags.yaml）：
  groups:     [{id, name, enabled}]
  characters: [{id, name, enabled, aliases, type, danbooru, appearance, negative_tags, outfits, dynamic_states, group_id}]
outfits / dynamic_states 的条目是 {name, tags, enabled}。

- 有效启用 = 角色 enabled 且所在分组 enabled；未分组恒为开。只有有效启用的角色参与在场匹配，
  停用的服装 / 动态标签条目不发给规划模型（active_characters）。
- 旧文件没有 id / enabled / groups：读取时补齐。id 由名字算出，同一份文件每次读到的 id 不变，保存后落盘。
- 角色名不分大小写唯一（在场匹配按名字和别名找角色）。
- 导入导出用小白X 的信封 novel-draw-characters（version 4），字段名在这里互转；
  启用状态是本机状态，不导出，导入时也不认。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

EXPORT_TYPE = "novel-draw-characters"
EXPORT_VERSION = 4
MAX_GROUPS = 50
MAX_GROUP_NAME_LENGTH = 32
MAX_IMPORT_CHARACTERS = 5000
UNGROUPED_NAME = "未分组"
DEFAULT_TYPE = "girl"
TYPE_PRESETS: tuple[str, ...] = ("girl", "woman", "boy", "man", "other", "no_humans")
IMPORT_CONFLICTS: tuple[str, ...] = ("replace", "skip")

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_FALSE_WORDS = {"false", "0", "off", "no"}
_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 导入文件里的字段名 → 本库字段名。小白X 的现行名字排在前面，旧名字（tags / costumes / clothes）只在现行名字缺失时生效。
IMPORT_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("aliases", "aliases"), ("type", "type"),
    ("appearance", "appearance"), ("tags", "appearance"),
    ("negativeTags", "negative_tags"), ("negative_tags", "negative_tags"),
    ("danbooruTag", "danbooru"), ("danbooru", "danbooru"),
    ("outfits", "outfits"), ("costumes", "outfits"), ("clothes", "outfits"),
    ("dynamicStates", "dynamic_states"), ("dynamic_states", "dynamic_states"),
)
_IMPORT_TEXT_KEYS = ("id", "name", "type", "appearance", "tags", "negativeTags", "negative_tags", "danbooruTag", "danbooru",
                     "groupId", "group_id")
_IMPORT_LIST_KEYS = ("aliases", "outfits", "costumes", "clothes", "dynamicStates", "dynamic_states")


class CharacterError(ValueError):
    """角色库内容不合规；消息直接给面板显示。"""


# ---- 基础清洗 ----

def _text(value: Any) -> str:
    if value is None or isinstance(value, (bool, Mapping, list, tuple)):
        return ""
    return str(value).strip()


def _tags(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value))


def _enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return value is not False and value != 0 if value is not None else True


def new_id(prefix: str = "char") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid4().hex[:4]}"


def _stable_id(prefix: str, name: str) -> str:
    return f"{prefix}-{hashlib.sha1(name.strip().lower().encode('utf-8')).hexdigest()[:10]}"


def _unique(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def clean_named_items(raw: Any) -> list[dict[str, Any]]:
    """服装 / 动态标签条目：{name, tags, enabled}；名称和标签都空的丢掉。"""
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, Mapping):
            name, tags, enabled = _text(item.get("name")), _tags(item.get("tags")), _enabled(item.get("enabled"))
        else:
            name, tags, enabled = "", _tags(item), True
        if name or tags:
            out.append({"name": name, "tags": tags, "enabled": enabled})
    return out


def clean_aliases(raw: Any, name: str = "") -> list[str]:
    """别名：去空、不分大小写去重，和正式名相同的不留。也接受逗号分隔的一行文本。"""
    if isinstance(raw, str):
        raw = re.split(r"[,，\n]", raw)
    seen = {name.strip().lower()}
    out: list[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        alias = _text(item)
        if alias and alias.lower() not in seen:
            seen.add(alias.lower())
            out.append(alias)
    return out


def clean_character(item: Mapping[str, Any]) -> dict[str, Any]:
    """单个角色的字段清洗；不查重、不补 id、不核对分组（那些由 clean_library 做）。"""
    name = _text(item.get("name"))
    return {
        "id": _text(item.get("id")),
        "name": name,
        "enabled": _enabled(item.get("enabled")),
        "aliases": clean_aliases(item.get("aliases"), name),
        "type": _text(item.get("type")) or DEFAULT_TYPE,
        "danbooru": _text(item.get("danbooru")),
        "appearance": _tags(item.get("appearance")),
        "negative_tags": _tags(item.get("negative_tags")),
        "outfits": clean_named_items(item.get("outfits")),
        "dynamic_states": clean_named_items(item.get("dynamic_states")),
        "group_id": _text(item.get("group_id")),
    }


# ---- 分组 ----

def group_name_problem(name: str, taken: set[str]) -> str:
    """分组名不能用的原因；能用时返回空串。taken 是已有分组名的小写集合。"""
    if not name:
        return "分组名称不能为空"
    if name == UNGROUPED_NAME:
        return f"分组不能叫“{UNGROUPED_NAME}”"
    if len(name) > MAX_GROUP_NAME_LENGTH:
        return f"分组名称最多 {MAX_GROUP_NAME_LENGTH} 个字：{name}"
    if name.lower() in taken:
        return f"分组名称重复：{name}"
    return ""


def clean_groups(raw: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(raw if isinstance(raw, (list, tuple)) else [], 1):
        if not isinstance(item, Mapping):
            if strict:
                raise CharacterError(f"第 {index} 个分组必须是对象")
            continue
        name = _text(item.get("name"))
        problem = group_name_problem(name, names)
        if problem:
            if strict:
                raise CharacterError(problem)
            name = name[:MAX_GROUP_NAME_LENGTH]
            if group_name_problem(name, names):
                continue
        if len(out) >= MAX_GROUPS:
            if strict:
                raise CharacterError(f"分组最多 {MAX_GROUPS} 个")
            break
        gid = _text(item.get("id"))
        if not _ID_RE.match(gid) or gid in ids:
            gid = _unique(_stable_id("grp", name), ids)
        ids.add(gid)
        names.add(name.lower())
        out.append({"id": gid, "name": name, "enabled": _enabled(item.get("enabled"))})
    return out


# ---- 整库 ----

def clean_library(raw: Any, *, strict: bool = False) -> dict[str, list[dict[str, Any]]]:
    """角色库的规范形态。strict=True 用于保存：不合规就报错；False 用于读取：能用的留下，其余跳过。"""
    data = raw if isinstance(raw, Mapping) else {}
    groups = clean_groups(data.get("groups"), strict=strict)
    group_ids = {g["id"] for g in groups}
    entries = data.get("characters")
    if entries is None:
        entries = []
    if not isinstance(entries, (list, tuple)):
        if strict:
            raise CharacterError("characters 必须是数组")
        entries = []
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(entries, 1):
        if not isinstance(item, Mapping):
            if strict:
                raise CharacterError(f"第 {index} 个角色必须是对象")
            continue
        character = clean_character(item)
        if not character["name"]:
            if strict:
                raise CharacterError(f"第 {index} 个角色缺少名称")
            continue
        key = character["name"].lower()
        if key in names:
            if strict:
                raise CharacterError(f"角色名重复：{character['name']}")
            continue
        names.add(key)
        if not _ID_RE.match(character["id"]) or character["id"] in ids:
            character["id"] = _unique(_stable_id("char", character["name"]), ids)
        ids.add(character["id"])
        if character["group_id"] not in group_ids:
            character["group_id"] = ""
        out.append(character)
    return {"groups": groups, "characters": out}


def revision(library: Mapping[str, Any]) -> str:
    """库内容的指纹：面板保存时带回，内容在别处变过就拒绝，避免整表保存冲掉别处的改动。"""
    text = json.dumps({"groups": library.get("groups") or [], "characters": library.get("characters") or []},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_effectively_enabled(character: Mapping[str, Any], groups: Any) -> bool:
    if not _enabled(character.get("enabled")):
        return False
    gid = _text(character.get("group_id"))
    return not any(isinstance(g, Mapping) and g.get("id") == gid and not _enabled(g.get("enabled")) for g in groups or []) if gid else True


def active_characters(library: Any) -> list[dict[str, Any]]:
    """参与出图的角色：只留有效启用的角色和启用的条目，去掉面板专用字段（id / enabled / group_id）。"""
    lib = clean_library(library)
    out: list[dict[str, Any]] = []
    for character in lib["characters"]:
        if not is_effectively_enabled(character, lib["groups"]):
            continue
        out.append({
            "name": character["name"], "aliases": list(character["aliases"]), "type": character["type"],
            "danbooru": character["danbooru"], "appearance": character["appearance"], "negative_tags": character["negative_tags"],
            "outfits": [{"name": i["name"], "tags": i["tags"]} for i in character["outfits"] if i["enabled"]],
            "dynamic_states": [{"name": i["name"], "tags": i["tags"]} for i in character["dynamic_states"] if i["enabled"]],
        })
    return out


# ---- 导出 ----

def _to_export(character: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": character["id"], "name": character["name"], "aliases": list(character["aliases"]), "type": character["type"],
        "appearance": character["appearance"], "negativeTags": character["negative_tags"], "danbooruTag": character["danbooru"],
        "outfits": [{"name": i["name"], "tags": i["tags"]} for i in character["outfits"]],
        "dynamicStates": [{"name": i["name"], "tags": i["tags"]} for i in character["dynamic_states"]],
    }
    if character["group_id"]:
        out["groupId"] = character["group_id"]
    return out


def export_library(library: Any, *, scope: str = "all", group_id: str = "", ids: Any = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """导出成小白X 能导入的角色标签文件。scope：all / group（group_id 为空表示未分组）/ selection（ids）。"""
    lib = clean_library(library)
    groups, characters = lib["groups"], lib["characters"]
    filename = "character-tags.json"
    if scope == "all":
        chosen = characters
    elif scope == "group":
        gid = _text(group_id)
        group = next((g for g in groups if g["id"] == gid), None)
        if gid and group is None:
            raise CharacterError("要导出的分组不存在")
        chosen = [c for c in characters if c["group_id"] == gid]
        filename = f"character-tags-{_FILENAME_BAD.sub('_', group['name'] if group else UNGROUPED_NAME)}.json"
    elif scope == "selection":
        wanted = {_text(i) for i in ids} if isinstance(ids, (list, tuple, set)) else set()
        chosen = [c for c in characters if c["id"] in wanted]
        filename = "character-tags-selection.json"
    else:
        raise CharacterError("导出范围只能是 all / group / selection")
    if not chosen:
        raise CharacterError("没有可导出的角色")
    used = {c["group_id"] for c in chosen}
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"filename": filename, "data": {
        "type": EXPORT_TYPE, "version": EXPORT_VERSION, "exportedAt": stamp, "scope": scope,
        "groups": [{"id": g["id"], "name": g["name"]} for g in groups if scope == "all" or g["id"] in used],
        "characters": [_to_export(c) for c in chosen],
    }}


# ---- 导入 ----

def _bad_shape(item: Mapping[str, Any]) -> bool:
    for key in _IMPORT_TEXT_KEYS:
        value = item.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
            return True
    return any(item.get(key) is not None and not isinstance(item.get(key), list) for key in _IMPORT_LIST_KEYS)


def parse_import(data: Any) -> dict[str, Any]:
    """角色标签文件 → {groups, characters: [(角色, 文件里实际出现的字段)], skipped, notes}。不合规的条目跳过并说明。"""
    if not isinstance(data, Mapping) or data.get("type") != EXPORT_TYPE:
        raise CharacterError("不是角色标签文件（type 应为 novel-draw-characters）")
    entries = data.get("characters")
    if not isinstance(entries, list):
        raise CharacterError("文件里没有 characters 数组")
    if len(entries) > MAX_IMPORT_CHARACTERS:
        raise CharacterError(f"文件里有 {len(entries)} 个角色，超过上限 {MAX_IMPORT_CHARACTERS}")
    groups: list[dict[str, str]] = []
    for item in data.get("groups") if isinstance(data.get("groups"), list) else []:
        if isinstance(item, Mapping):
            gid, name = _text(item.get("id")), _text(item.get("name"))[:MAX_GROUP_NAME_LENGTH]
            if gid and name and name != UNGROUPED_NAME:
                groups.append({"id": gid, "name": name})
    characters: list[tuple[dict[str, Any], set[str]]] = []
    seen_ids: set[str] = set()
    bad = unnamed = with_forms = 0
    for item in entries:
        if not isinstance(item, Mapping) or _bad_shape(item):
            bad += 1
            continue
        source: dict[str, Any] = {}
        for key, target in IMPORT_FIELD_MAP:
            if key in item and target not in source:
                source[target] = item[key]
        fields = set(source)
        group_id = item.get("groupId") if "groupId" in item else item.get("group_id")
        character = clean_character({**source, "id": item.get("id"), "name": item.get("name"), "group_id": group_id})
        if not character["name"]:
            unnamed += 1
            continue
        if not _ID_RE.match(character["id"]) or character["id"] in seen_ids:
            character["id"] = ""
        else:
            seen_ids.add(character["id"])
        if isinstance(item.get("forms"), list) and item["forms"]:
            with_forms += 1
        characters.append((character, fields))
    notes: list[str] = []
    if bad:
        notes.append(f"{bad} 个条目格式不对，已跳过")
    if unnamed:
        notes.append(f"{unnamed} 个条目没有名称，已跳过")
    if with_forms:
        notes.append(f"{with_forms} 个角色带多个形态，只导入了第一形态")
    return {"groups": groups, "characters": characters, "skipped": bad + unnamed, "notes": notes}


def _replace_fields(local: dict[str, Any], incoming: Mapping[str, Any], fields: set[str]) -> None:
    """用文件里实际出现的字段覆盖本地角色；id、启用状态、分组不动，条目的停用状态按名称保留。"""
    local["name"] = incoming["name"]
    for key in fields:
        if key in ("outfits", "dynamic_states"):
            off = {i["name"] for i in local[key] if i["name"] and not i["enabled"]}
            local[key] = [{**i, "enabled": i["name"] not in off} for i in incoming[key]]
        else:
            local[key] = incoming[key]
    local["aliases"] = clean_aliases(local["aliases"], local["name"])


def merge_import(library: Any, data: Any, *, conflict: str = "replace", as_disabled: bool = False) -> dict[str, Any]:
    """把角色标签文件并进角色库，返回 {library, summary, notes}；不写盘。

    已有角色按 id 找，找不到再按名字找（名字在本库里唯一，所以同名一定算同一个角色）。
    conflict=replace：用文件里出现的字段覆盖；skip：已有的不动。新角色按文件里的分组名归位，没有就新建分组。
    """
    if conflict not in IMPORT_CONFLICTS:
        raise CharacterError("已有角色的处理方式只能是 replace 或 skip")
    parsed = parse_import(data)
    lib = clean_library(library)
    groups, characters = lib["groups"], lib["characters"]
    notes: list[str] = list(parsed["notes"])
    summary = {"added": 0, "replaced": 0, "skipped": int(parsed["skipped"]), "new_groups": 0}
    by_id = {c["id"]: c for c in characters}
    by_name = {c["name"].lower(): c for c in characters}
    group_by_name = {g["name"].lower(): g for g in groups}
    file_group_names = {g["id"]: g["name"] for g in parsed["groups"]}
    overflow = kept_names = 0

    def resolve_group(file_group_id: str) -> str:
        nonlocal overflow
        name = file_group_names.get(file_group_id, "")
        if not name:
            return ""
        found = group_by_name.get(name.lower())
        if found is not None:
            return found["id"]
        if len(groups) >= MAX_GROUPS:
            overflow += 1
            return ""
        taken = {g["id"] for g in groups}
        gid = file_group_id if _ID_RE.match(file_group_id) and file_group_id not in taken else _unique(_stable_id("grp", name), taken)
        group = {"id": gid, "name": name, "enabled": True}
        groups.append(group)
        group_by_name[name.lower()] = group
        summary["new_groups"] += 1
        return gid

    for incoming, fields in parsed["characters"]:
        same_name = by_name.get(incoming["name"].lower())
        local = by_id.get(incoming["id"]) if incoming["id"] else None
        if local is None:
            local = same_name
        elif same_name is not None and same_name is not local:
            # 按 id 找到了本地角色，但文件里的新名字已被另一个本地角色占用：内容照常覆盖，名字保留本地的。
            incoming = {**incoming, "name": local["name"]}
            kept_names += 1
        if local is not None:
            if conflict == "skip":
                summary["skipped"] += 1
                continue
            by_name.pop(local["name"].lower(), None)
            _replace_fields(local, incoming, fields)
            by_name[local["name"].lower()] = local
            summary["replaced"] += 1
            continue
        character = dict(incoming)
        if not character["id"] or character["id"] in by_id:
            character["id"] = _unique(new_id("char"), set(by_id))
        character["enabled"] = not as_disabled
        character["group_id"] = resolve_group(character["group_id"])
        characters.append(character)
        by_id[character["id"]] = character
        by_name[character["name"].lower()] = character
        summary["added"] += 1
    if overflow:
        notes.append(f"分组已达上限 {MAX_GROUPS} 个，{overflow} 个角色放进了“{UNGROUPED_NAME}”")
    if kept_names:
        notes.append(f"{kept_names} 个角色的新名字和别的角色重名，保留了原来的名字")
    return {"library": clean_library(lib, strict=True), "summary": summary, "notes": notes}


# ---- 录入单个角色（QQ 里的 save_draw_character 工具） ----

UPSERT_MODES: tuple[str, ...] = ("error", "update")
_UPSERT_FIELDS = ("aliases", "type", "danbooru", "appearance", "negative_tags", "outfits", "dynamic_states")


class CharacterExists(CharacterError):
    """要录入的角色已经在库里，而调用方没有要求更新；character 是库里现有的那一条。"""

    def __init__(self, message: str, character: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.character = dict(character)


def find_character(characters: Any, name: str) -> dict[str, Any] | None:
    """按正式名找，找不到再按别名找；都不分大小写（和在场匹配的口径一致）。"""
    key = _text(name).lower()
    if not key:
        return None
    entries = [c for c in characters or [] if isinstance(c, dict)]
    for character in entries:
        if _text(character.get("name")).lower() == key:
            return character
    for character in entries:
        if any(_text(alias).lower() == key for alias in character.get("aliases") or []):
            return character
    return None


def _merge_named(local: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """服装 / 动态标签按名称合并：同名的换成新标签（停用状态不动），其余追加。"""
    out = [dict(item) for item in local]
    by_name = {item["name"].lower(): item for item in out if item["name"]}
    for item in incoming:
        found = by_name.get(item["name"].lower()) if item["name"] else None
        if found is not None:
            found["tags"] = item["tags"]
            continue
        added = dict(item)
        out.append(added)
        if added["name"]:
            by_name[added["name"].lower()] = added
    return out


def upsert_character(library: Any, fields: Mapping[str, Any], *, if_exists: str = "error",
                     group_name: str = "") -> dict[str, Any]:
    """录入一个角色，返回 {library, character, created, active}；不写盘。

    库里没有（按正式名、别名都找不到）就新增。已经有：if_exists=error 抛 CharacterExists；
    update 只改 fields 里给出的字段，别名并入，服装 / 动态标签按名称合并，正式名、id、启用状态不动。
    group_name 非空时把角色放进这个分组，没有就新建。active 表示录入后这个角色是否参与出图。
    """
    if if_exists not in UPSERT_MODES:
        raise CharacterError("if_exists 只能是 error 或 update")
    if not isinstance(fields, Mapping):
        raise CharacterError("角色内容必须是对象")
    lib = clean_library(library)
    groups, characters = lib["groups"], lib["characters"]
    given = {key for key in _UPSERT_FIELDS if fields.get(key) is not None}
    incoming = clean_character({"name": fields.get("name"), **{key: fields[key] for key in given}})
    if not incoming["name"]:
        raise CharacterError("角色名称不能为空")
    local = find_character(characters, incoming["name"])
    created = local is None
    if local is not None and if_exists != "update":
        raise CharacterExists(f"角色「{local['name']}」已经在角色库里", local)
    # 名字和别名用来在聊天里认出角色，新别名不能指向别的角色。
    for alias in incoming["aliases"]:
        owner = find_character(characters, alias)
        if owner is not None and owner is not local:
            raise CharacterError(f"别名「{alias}」已经被角色「{owner['name']}」使用")
    if local is None:
        local = {**incoming, "id": _unique(new_id("char"), {c["id"] for c in characters}), "enabled": True, "group_id": ""}
        characters.append(local)
    else:
        for key in given:
            if key in ("outfits", "dynamic_states"):
                local[key] = _merge_named(local[key], incoming[key])
            elif key == "aliases":
                local[key] = clean_aliases([*local[key], *incoming[key]], local["name"])
            else:
                local[key] = incoming[key]
    wanted_group = _text(group_name)
    if wanted_group:
        group = next((g for g in groups if g["name"].lower() == wanted_group.lower()), None)
        if group is None:
            problem = group_name_problem(wanted_group, {g["name"].lower() for g in groups})
            if problem:
                raise CharacterError(problem)
            if len(groups) >= MAX_GROUPS:
                raise CharacterError(f"分组最多 {MAX_GROUPS} 个")
            group = {"id": _unique(_stable_id("grp", wanted_group), {g["id"] for g in groups}), "name": wanted_group, "enabled": True}
            groups.append(group)
        local["group_id"] = group["id"]
    cleaned = clean_library(lib, strict=True)
    saved = next(c for c in cleaned["characters"] if c["id"] == local["id"])
    return {"library": cleaned, "character": saved, "created": created,
            "active": is_effectively_enabled(saved, cleaned["groups"])}
