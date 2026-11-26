"""Channel schemes: the single place that turns data/config.yaml into a concrete LLM channel.

data/config.yaml::

    channels:
      - {id: main, name: 主模型, type: gemini, base_url: ..., api_key: ..., model: ...}
      - {id: vision, ...}                 # role channels: one per role, edited on the model page
      - {id: fallback-1, ...}
      - {id: image, ..., enabled: false}  # enabled: false = the role is off, its settings are kept
      - {id: draw, ...}                   # other channels, picked by node_models / draw presets
    fallbacks:                 # fallback channels, tried in order when the primary request fails
      - {channel: fallback-1}
    node_models:               # per-node choice made in the WebUI; empty channel = main
      draw.novelai_planner: {channel: draw}

Each role owns the channel whose id is the role name (``ROLE_CHANNELS``). A missing
or disabled role channel means: compact / summary / memory / vision use the main
channel, image / image_gpt are unavailable.

Every reader (engine, fallback chain, roles, vision, supervisor header, chat
commands) resolves through this module. ``migrate`` converts earlier layouts
(top-level provider blocks, ``provider:``, ``active_channel`` + shared schemes
referenced by ``system_models`` / fallbacks, model fields inside node YAML) once;
nothing reads an old layout.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

# Options a channel (or a fallback entry) may carry; passed to the provider as provider_options.
OPTION_KEYS = ("reasoning_field", "reasoning_effort", "max_tokens", "max_completion_tokens", "thought_summary", "sticky_session")

MAIN = "main"
# Role channel id -> label. Fallback channels are listed in ``fallbacks``.
ROLE_CHANNELS = {
    MAIN: "主模型",
    "vision": "识图模型",
    "compact": "上下文压缩模型",
    "summary": "轮次摘要模型",
    "memory": "记忆提取模型",
    "image": "Gemini 生图",
    "image_gpt": "GPT 生图",
}
SYSTEM_MODEL_SLOT_NAMES = tuple(k for k in ROLE_CHANNELS if k != MAIN)
# System node id -> role.
SYSTEM_NODE_MODEL_SLOTS = {
    "system.compactor": "compact",
    "system.turn_summarizer": "summary",
    "system.memory_extractor": "memory",
}


class ChannelError(ValueError):
    """The configuration does not name a usable channel."""


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    type: str
    base_url: str
    api_key: str
    model: str
    options: dict = field(default_factory=dict)
    enabled: bool = True


# ---------------------------------------------------------------------------
#  Reading
# ---------------------------------------------------------------------------

def config_path(root: Path | str) -> Path:
    return Path(root) / "data" / "config.yaml"


def load_config(root: Path | str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(config_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_dotenv(root: Path | str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = (Path(root) / ".env").read_text(encoding="utf-8").splitlines()
    except Exception:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if value or key.strip() not in values:
            values[key.strip()] = value
    return values


_ENV_REF = re.compile(r"^\$(?:ENV)?\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_ref(value: Any, root: Path | str | None = None) -> str:
    """``${NAME}`` / ``$ENV{NAME}`` -> environment value (then the workspace .env); other text verbatim."""
    text = str(value or "").strip()
    match = _ENV_REF.match(text)
    if not match:
        return text
    name = match.group(1)
    resolved = os.environ.get(name, "").strip()
    if not resolved and root is not None:
        resolved = read_dotenv(root).get(name, "").strip()
    return resolved


def channel_list(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = cfg.get("channels") if isinstance(cfg, Mapping) else None
    return [c for c in (raw or []) if isinstance(c, dict) and str(c.get("id") or "").strip()]


def get_channel(cfg: Mapping[str, Any], channel_id: str = "", *, model: str = "",
                root: Path | str | None = None) -> Channel:
    """The named channel (empty = main); ``model`` overrides the channel's model."""
    cid = str(channel_id or "").strip() or MAIN
    raw = next((c for c in channel_list(cfg) if str(c["id"]).strip() == cid), None)
    if raw is None:
        raise ChannelError("还没有配置主模型" if cid == MAIN else f"渠道不存在：{cid}")
    return Channel(
        id=cid,
        name=str(raw.get("name") or cid),
        type=str(raw.get("type") or "").strip().lower(),
        base_url=resolve_ref(raw.get("base_url"), root).rstrip("/"),
        api_key=resolve_ref(raw.get("api_key"), root),
        model=str(model or "").strip() or resolve_ref(raw.get("model"), root),
        options={k: raw[k] for k in OPTION_KEYS if k in raw},
        enabled=raw.get("enabled") is not False,
    )


def _ref_of(entry: Any) -> tuple[str, str]:
    if not isinstance(entry, Mapping):
        return "", ""
    return str(entry.get("channel") or "").strip(), str(entry.get("model") or "").strip()


def slot_channel(cfg: Mapping[str, Any], slot: str, *, root: Path | str | None = None) -> Channel | None:
    """The role's own channel; None when it is missing or switched off."""
    if not any(str(c["id"]).strip() == slot for c in channel_list(cfg)):
        return None
    ch = get_channel(cfg, slot, root=root)
    return ch if ch.enabled else None


def node_channel(cfg: Mapping[str, Any], node_id: str, *, root: Path | str | None = None) -> Channel:
    """Channel for a node: its role channel or node_models entry, otherwise main."""
    slot = SYSTEM_NODE_MODEL_SLOTS.get(node_id, "")
    if slot:
        chosen = slot_channel(cfg, slot, root=root)
        if chosen is not None:
            return chosen
    nodes = cfg.get("node_models") if isinstance(cfg.get("node_models"), Mapping) else {}
    channel, model = _ref_of(nodes.get(node_id))
    return get_channel(cfg, channel, model=model, root=root)


def apply_session_override(cfg: Mapping[str, Any], channel: Channel, override: Mapping[str, Any] | None,
                           *, root: Path | str | None = None) -> Channel:
    """Session override: ``channel`` picks another scheme; model/base_url/api_key/provider replace fields."""
    ov = override if isinstance(override, Mapping) else {}
    picked = str(ov.get("channel") or "").strip()
    if picked:
        channel = get_channel(cfg, picked, root=root)
    changes: dict[str, Any] = {}
    for key, attr in (("provider", "type"), ("provider_type", "type"), ("model", "model"),
                      ("base_url", "base_url"), ("api_key", "api_key")):
        value = str(ov.get(key) or "").strip()
        if value:
            changes[attr] = value.lower() if attr == "type" else value
    return replace(channel, **changes) if changes else channel


def fallback_ids(cfg: Mapping[str, Any]) -> list[str]:
    return [cid for cid, _model in (_ref_of(e) for e in cfg.get("fallbacks") or []) if cid]


def fallback_channels(cfg: Mapping[str, Any], *, root: Path | str | None = None) -> list[Channel]:
    """The fallback chain; missing or switched-off channels are skipped."""
    out: list[Channel] = []
    for entry in cfg.get("fallbacks") or []:
        channel_id, _model = _ref_of(entry)
        try:
            ch = get_channel(cfg, channel_id, root=root)
        except ChannelError:
            continue
        if not ch.enabled:
            continue
        extra = {k: entry[k] for k in OPTION_KEYS if k in entry}
        out.append(replace(ch, options={**ch.options, **extra}) if extra else ch)
    return out


def role_of(cfg: Mapping[str, Any], channel_id: str) -> str:
    """``main`` / a role name / ``fallback`` / ``other``."""
    if channel_id in ROLE_CHANNELS:
        return channel_id
    return "fallback" if channel_id in fallback_ids(cfg) else "other"


def references(cfg: Mapping[str, Any], channel_id: str) -> list[str]:
    """Node choices that use ``channel_id`` (deletion guard)."""
    block = cfg.get("node_models") if isinstance(cfg.get("node_models"), Mapping) else {}
    return [f"节点模型 {name}" for name, entry in block.items() if _ref_of(entry)[0] == channel_id]


# ---------------------------------------------------------------------------
#  Channel type vs. address
# ---------------------------------------------------------------------------

def _url_path(base_url: str) -> str:
    try:
        return urlsplit(base_url or "").path.rstrip("/").lower()
    except ValueError:
        return ""


def infer_type(declared: str, base_url: str) -> str:
    """Type for a migrated channel: a Gemini-labelled ``.../v1`` address speaks the OpenAI format."""
    kind = str(declared or "").strip().lower()
    path = _url_path(base_url)
    if kind.startswith("gemini") and path.endswith("/v1"):
        return "openai"
    if not kind:
        return "gemini" if path.endswith("/v1beta") else "openai"
    return kind


def type_mismatch(kind: str, base_url: str) -> str:
    """A form hint when the address format and the chosen type disagree; empty when they fit."""
    kind = str(kind or "").strip().lower()
    path = _url_path(base_url)
    if kind.startswith("gemini") and path.endswith("/v1"):
        return "地址以 /v1 结尾，通常是 OpenAI 格式；Gemini 类型需要原生接口（/v1beta 或根地址）。"
    if kind in ("openai", "openai-responses", "deepseek") and path.endswith("/v1beta"):
        return "地址以 /v1beta 结尾，是 Gemini 原生接口；请把类型改成 Gemini。"
    return ""


# ---------------------------------------------------------------------------
#  One-time migration of the previous layout
# ---------------------------------------------------------------------------

_LAYOUT_KEYS = frozenset({
    "version", "provider", "fallbacks", "mimo_audio", "system_models", "model_capabilities",
    "channels", "active_channel", "node_models",
})
_NODE_MODEL_FIELDS = ("provider", "model", "api_key", "base_url")
_NODE_MODEL_LINE = re.compile(r"^(?:provider|model|api_key|base_url)[ \t]*:.*(?:\r?\n|$)", re.M)


def _is_provider_block(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("base_url", "api_key", "model"))


class _Channels:
    def __init__(self, channels: list[dict[str, Any]], root: Path | str | None):
        self.items = channels
        self.root = root

    def _unique(self, hint: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", hint).strip("-") or "channel"
        taken = {str(c["id"]) for c in self.items}
        cid, n = base, 2
        while cid in taken:
            cid, n = f"{base}-{n}", n + 1
        return cid

    def add(self, *, hint: str, name: str, kind: str, base_url: str, api_key: str, model: str = "",
            options: Mapping[str, Any] | None = None) -> str:
        cid = self._unique(hint)
        entry = {"id": cid, "name": name or cid, "type": infer_type(kind, resolve_ref(base_url, self.root)),
                 "base_url": base_url, "api_key": api_key, "model": model}
        entry.update({k: v for k, v in (options or {}).items() if k in OPTION_KEYS})
        self.items.append(entry)
        return cid

    def find_or_add(self, *, hint: str, name: str, kind: str, base_url: str, api_key: str, model: str = "") -> str:
        resolved_type = infer_type(kind, resolve_ref(base_url, self.root))

        def family(kind: Any) -> str:
            return "gemini" if str(kind or "").startswith("gemini") else str(kind or "")

        for c in self.items:
            same = (family(c.get("type")), c.get("base_url"), c.get("api_key")) == (family(resolved_type), base_url, api_key)
            if same:
                return str(c["id"])
        return self.add(hint=hint, name=name, kind=kind, base_url=base_url, api_key=api_key, model=model)


def migrate(data: Mapping[str, Any], *, root: Path | str | None = None) -> tuple[dict[str, Any], bool]:
    """Convert the previous layout to channel schemes. Returns (new data, changed).

    With ``root`` given, model fields inside config/nodes/*.yaml move to node_models
    and are removed from the node files.
    """
    data = dict(data)
    changed = False
    builder = _Channels([dict(c) for c in channel_list(data)], root)

    legacy_active = ""
    if "provider" in data:
        legacy_active = str(data.pop("provider") or "").strip().lower()
        changed = True

    blocks: dict[str, dict[str, Any]] = {}
    block_ids: dict[str, str] = {}
    for key in [k for k, v in data.items() if k not in _LAYOUT_KEYS and _is_provider_block(v)]:
        block = data.pop(key)
        changed = True
        blocks[key] = block
        if not str(block.get("api_key") or "").strip():
            continue  # a default block that was never filled in
        block_ids[key] = builder.add(
            hint=key, name="主渠道" if key == legacy_active else key, kind=key, base_url=str(block.get("base_url") or ""),
            api_key=str(block.get("api_key") or ""), model=str(block.get("model") or ""), options=block,
        )

    def for_provider(provider: str) -> str:
        p = str(provider or "").strip().lower()
        if p in block_ids:
            return block_ids[p]
        if p.startswith("gemini"):
            return next((cid for k, cid in block_ids.items() if k.startswith("gemini")), "")
        return ""

    def parent_block(provider: str) -> dict[str, Any]:
        p = str(provider or "").strip().lower()
        if p in blocks:
            return blocks[p]
        if p.startswith("gemini"):
            return next((b for k, b in blocks.items() if k.startswith("gemini")), {})
        return {}

    def ref_for(entry: Mapping[str, Any], *, hint: str, name: str) -> str:
        """Channel for a legacy fallback / slot entry that names a provider and maybe its own endpoint."""
        provider = str(entry.get("provider") or "").strip().lower()
        own_url = str(entry.get("base_url") or "").strip()
        own_key = str(entry.get("api_key") or "").strip()
        if not (own_url or own_key):
            return for_provider(provider)
        parent = parent_block(provider or legacy_active)
        kind = provider or legacy_active
        return builder.find_or_add(
            hint=hint, name=name, kind=kind,
            base_url=own_url or str(parent.get("base_url") or ""),
            api_key=own_key or str(parent.get("api_key") or ""),
        )

    if blocks and not str(data.get("active_channel") or "").strip():
        if builder.items:
            data["active_channel"] = for_provider(legacy_active) or str(builder.items[0]["id"])
            changed = True

    fallbacks = data.get("fallbacks")
    if isinstance(fallbacks, list):
        out: list[Any] = []
        for i, entry in enumerate(fallbacks, start=1):
            if not isinstance(entry, dict) or not any(k in entry for k in ("provider", "base_url", "api_key")):
                out.append(entry)
                continue
            changed = True
            new = {"channel": ref_for(entry, hint=f"fallback-{i}", name=f"备用渠道 {i}"),
                   "model": str(entry.get("model") or "")}
            new.update({k: v for k, v in entry.items() if k in OPTION_KEYS})
            target = next((c for c in builder.items if c.get("id") == new["channel"]), None)
            if target is not None and not str(target.get("model") or "").strip() and new["model"]:
                target["model"] = new["model"]  # a scheme made for a fallback is usable on its own
            out.append(new)
        data["fallbacks"] = out

    slots = data.get("system_models")
    if isinstance(slots, dict):
        new_slots: dict[str, Any] = {}
        for slot, entry in slots.items():
            if not isinstance(entry, dict) or not any(k in entry for k in ("provider", "base_url", "api_key")):
                new_slots[slot] = entry
                continue
            changed = True
            ref = {"channel": ref_for(entry, hint=slot, name=f"{slot} 渠道"), "model": str(entry.get("model") or "")}
            new_slots[slot] = {k: v for k, v in ref.items() if v}
        data["system_models"] = new_slots

    if root is not None:
        node_models = dict(data.get("node_models") or {})
        if _migrate_node_files(Path(root), builder, node_models, for_provider):
            data["node_models"] = node_models
            changed = True

    if root is not None and _migrate_draw_presets(Path(root), builder):
        changed = True

    if builder.items != channel_list(data):
        data["channels"] = builder.items
        changed = True
    if _to_roles(data, root):
        changed = True
    if not changed:
        return data, False

    ordered: dict[str, Any] = {"version": data.pop("version", 1)}
    if "channels" in data:
        ordered["channels"] = data.pop("channels")
    ordered.update(data)
    return ordered, True


def _to_roles(data: dict[str, Any], root: Path | str | None) -> bool:
    """``active_channel`` + shared schemes referenced by ``system_models`` / fallbacks -> one channel per role.

    The active scheme becomes ``main``; a role or fallback that pointed at a scheme gets its
    own copy (with the model it named), so editing one role never changes another.
    """
    fallbacks = [e for e in (data.get("fallbacks") or []) if isinstance(e, dict)]
    if not ("active_channel" in data or "system_models" in data or any("model" in e for e in fallbacks)):
        return False
    chans = [dict(c) for c in channel_list(data)]
    ids = {str(c["id"]) for c in chans}

    def unique(base: str) -> str:
        cid, n = base, 2
        while cid in ids:
            cid, n = f"{base}-{n}", n + 1
        ids.add(cid)
        return cid

    renames: dict[str, str] = {}

    def rename(old: str, new: str) -> None:
        for c in chans:
            if c["id"] == old:
                c["id"] = new
        ids.discard(old)
        ids.add(new)
        renames.update({k: new for k, v in renames.items() if v == old})
        renames[old] = new

    def find(cid: str) -> dict[str, Any] | None:
        cid = renames.get(cid, cid)
        return next((c for c in chans if c["id"] == cid), None)

    def copy_as(src: dict[str, Any], new_id: str, name: str, model: str) -> None:
        c = {**src, "id": new_id, "name": name}
        if model:
            c["model"] = model
        c.pop("enabled", None)
        chans.append(c)

    active = str(data.pop("active_channel", "") or "").strip()
    if active not in ids:
        active = chans[0]["id"] if chans else ""
    if active and active != MAIN:
        if MAIN in ids:
            rename(MAIN, unique("main-old"))
        rename(active, MAIN)
    for c in chans:
        if c["id"] == MAIN:
            c["name"] = ROLE_CHANNELS[MAIN]

    slots = data.pop("system_models", None)
    for slot, entry in (slots.items() if isinstance(slots, Mapping) else ()):
        if slot not in SYSTEM_MODEL_SLOT_NAMES:
            continue
        channel, model = _ref_of(entry)
        if not (channel or model):
            continue
        src = find(channel or MAIN)
        if src is None:
            continue
        if src["id"] == slot:
            if model:
                src["model"] = model
            continue
        if slot in ids:
            rename(slot, unique(f"{slot}-old"))
        copy_as(src, slot, ROLE_CHANNELS[slot], model)
        ids.add(slot)

    chain: list[dict[str, Any]] = []
    taken: set[str] = set()
    for i, entry in enumerate(fallbacks, start=1):
        channel, model = _ref_of(entry)
        src = find(channel or MAIN)
        if src is None:
            continue
        options = {k: v for k, v in entry.items() if k in OPTION_KEYS}
        if src["id"] not in ROLE_CHANNELS and src["id"] not in taken and not references(data, src["id"]):
            cid = src["id"]  # the scheme only served this fallback: it becomes that fallback's channel
            if model:
                src["model"] = model
        else:
            cid = unique(f"fallback-{i}")
            copy_as(src, cid, f"备用链路 {i}", model)
        taken.add(cid)
        chain.append({"channel": cid, **options})
    if "fallbacks" in data:
        data["fallbacks"] = chain

    nodes = data.get("node_models")
    if isinstance(nodes, dict):
        for entry in nodes.values():
            if isinstance(entry, dict) and entry.get("channel") in renames:
                entry["channel"] = renames[entry["channel"]]
    if root is not None and renames:
        _rename_draw_preset_channels(Path(root), renames)
    data["channels"] = chans
    return True


def _rename_draw_preset_channels(root: Path, renames: Mapping[str, str]) -> None:
    path = root.joinpath(*_DRAW_SETTINGS)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return
    agent = raw.get("agent") if isinstance(raw, dict) else None
    presets = agent.get("presets") if isinstance(agent, dict) else None
    touched = False
    for preset in presets if isinstance(presets, list) else []:
        if isinstance(preset, dict) and preset.get("channel") in renames:
            preset["channel"] = renames[preset["channel"]]
            touched = True
    if touched:
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _migrate_node_files(root: Path, builder: _Channels, node_models: dict[str, Any], for_provider: Any) -> bool:
    nodes_dir = root / "config" / "nodes"
    if not nodes_dir.is_dir():
        return False
    touched = False
    for path in sorted(nodes_dir.glob("*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(text)
        except Exception:
            continue
        if not isinstance(raw, dict) or not any(k in raw for k in _NODE_MODEL_FIELDS):
            continue
        node_id = str(raw.get("id") or path.stem.removesuffix(".local")).strip()
        values = {k: resolve_ref(raw.get(k), root) for k in _NODE_MODEL_FIELDS}
        if any(values.values()) and node_id not in node_models:
            if values["base_url"] or values["api_key"]:
                draw = "DRAW_TAG" in text
                channel = builder.find_or_add(
                    hint="draw" if draw else node_id, name="绘图分析" if draw else f"{node_id} 专用",
                    kind=values["provider"], base_url=values["base_url"], api_key=values["api_key"],
                    model=values["model"],
                )
            else:
                channel = for_provider(values["provider"])
            ref = {"channel": channel, "model": values["model"]}
            node_models[node_id] = {k: v for k, v in ref.items() if v}
        path.write_text(_NODE_MODEL_LINE.sub("", text), encoding="utf-8")
        touched = True
    return touched


_DRAW_SETTINGS = ("tools", "drawtools", "settings.yaml")


def _migrate_draw_presets(root: Path, builder: _Channels) -> bool:
    """Scene-agent presets carried their own provider/base_url/api_key; point them at a channel scheme."""
    path = root.joinpath(*_DRAW_SETTINGS)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    agent = raw.get("agent") if isinstance(raw, dict) else None
    presets = agent.get("presets") if isinstance(agent, dict) else None
    if not isinstance(presets, list):
        return False
    touched = False
    for preset in presets:
        if not isinstance(preset, dict) or not any(k in preset for k in ("provider", "base_url", "api_key")):
            continue
        touched = True
        url, key = str(preset.get("base_url") or "").strip(), str(preset.get("api_key") or "").strip()
        if key and "channel" not in preset:
            name = str(preset.get("name") or "")
            preset["channel"] = builder.find_or_add(
                hint=f"draw-agent-{preset.get('id') or 'preset'}", name=f"场景 Agent {name}".strip(),
                kind=str(preset.get("provider") or ""), base_url=url, api_key=key,
                model=str(preset.get("model") or ""))
        for k in ("provider", "base_url", "api_key", "model_list_auth", "copy_key_from", "clear_api_key"):
            preset.pop(k, None)
    if touched:
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return touched
