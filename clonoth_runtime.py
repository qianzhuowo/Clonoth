from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import httpx
import yaml


DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "version": 1,
    "engine": {
        "max_steps": 32,
        "streaming": True,
        "history_limit": 80,
        "poll_interval_sec": 1.0,
        "approval_poll_interval_sec": 0.5,
        "http": {"client_timeout_sec": 60.0},
        "supervisor": {
            "health_timeout_sec": 2.0,
            "wait_poll_interval_sec": 0.5,
        },
        "tool_trace": {
            # [AutoC 2026-06-08] Why: tool outputs now preserve full raw text in
            # task-scoped artifacts while only bounded snippets enter the model
            # context. How: make the default runtime config match the checked-in
            # runtime.yaml dict form but keep loader compatibility with old integer
            # values. Purpose: deployments without config/runtime.yaml still get the
            # same per-tool truncation and readback behavior.
            "artifact_enabled": True,
            "max_inline_chars": {
                "default": 32000,
                "read_file": 80000,
                "grep": 50000,
                "list_dir": 50000,
                "execute_command": 24000,
                "media": 8000,
            },
            "max_inline_chars_per_step": 120000,
            "head_tail_tools": ["execute_command"],
            "tail_ratio": 0.25,
            "max_progress_arg_chars": 320,
        },
        "compact": {
            "threshold_chars": 800000,
            "keep_recent": 6,
        },
        "retry": {
            "max_retries": 3,
            "initial_delay_sec": 1.0,
            "max_delay_sec": 30.0,
            "backoff_multiplier": 2.0,
        },
        "model": "",
    },
    "providers": {
        "openai": {
            "timeout_sec": 60.0,
            # [AutoC 2026-06-01] Why: provider_options previously existed only on
            # node YAML, so provider-wide defaults had no documented global home.
            # How: add an empty options dict under the OpenAI provider defaults.
            # Purpose: runner can merge providers.<type>.options with node-level
            # provider_options while keeping old runtime.yaml files compatible.
            "options": {},
        }
    },
    "meta": {
        "execute_command": {
            "default_timeout_sec": 90.0,
            "max_output_chars": 12000,
            # [AutoC 2026-06-27] Why: long execute_command calls should not block
            # the LLM loop until their hard timeout. How: keep a runtime-configured
            # adaptive async threshold under the existing execute_command meta block.
            # Purpose: deployments without config/runtime.yaml get the same default
            # background-upgrade behavior as checked-in runtime.yaml.
            "async_upgrade": {"enabled": True, "threshold_sec": 60.0},
        },
        "git": {"diff_max_chars": 600000},
        "search": {"max_file_size_bytes": 3000000, "max_matches": 100},
    },
    "tools": {
        "command": {
            "default_timeout_sec": 60.0,
        }
    },
    "skills": {
        "max_budget_chars": 0,
    },
    "routing": {
        "entry_node_id": "bootstrap.shell_orchestrator",
    },
    # [deprecated] shell section 仅供 adapters/shell CLI/TUI 使用。
    # entry_node_id 已迁移到 routing.entry_node_id。
    "shell": {
        "http": {"client_timeout_sec": 10.0},
        "supervisor": {
            "health_timeout_sec": 2.0,
            "wait_poll_interval_sec": 0.5,
        },
        "events_poll_interval_sec": 0.5,
    },
    "supervisor": {
        "process_manager": {
            "stop_wait_timeout_sec": 5.0,
            "engine_workers": 2,
            "spawn_onebot_adapter": False,
            "spawn_napcat": False,
            "spawn_discord_adapter": False,
            "discord_entry_node": "ereuna_main",
            "discord_bridge_port": 8768,
            "discord_history_len": 15,
            "discord_log_channel": "",
            "discord_superusers": "",
            "discord_proxy": "",
            "discord_retry_delay": 10,
            "discord_voice_input": True,
            "discord_voice_reply": True,
            "discord_voice_reply_on_voice_input": True,
            "discord_tts_format": "wav",
            "discord_tts_voice": "",
            "discord_tts_instruction": "",
            "discord_delete_audio_after_use": True,
            "tts_backend": "ryza_onnx",
            "local_onnx_tts_path": "",
            "local_onnx_tts_provider": "hybrid",
            "local_onnx_tts_validate_hashes": False,
            "local_onnx_tts_default_preset": "normal_cn",
            "local_onnx_tts_seed": 1234,
            "local_onnx_tts_idle_unload_seconds": 30,
            "onebot_host": "127.0.0.1",
            "onebot_port": 18080,
            "onebot_entry_node": "qq.orchestrator",
            "napcat_quick_login": "",
            "onebot_owner_id": "",
            "onebot_owner_name": "",
            "onebot_voice_input": True,
            "onebot_voice_reply": True,
            "onebot_voice_reply_on_voice_input": True,
            "onebot_voice_reply_auto_on_text": True,
            "onebot_voice_reply_private": True,
            "onebot_voice_reply_group": True,
            "onebot_delete_audio_after_use": True,
            "onebot_tts_format": "wav",
            "onebot_tts_voice": "",
            "onebot_tts_instruction": "",
        },
    },
}


_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def runtime_config_path(workspace_root: Path) -> Path:
    return workspace_root / "config" / "runtime.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_runtime_config(workspace_root: Path) -> dict[str, Any]:
    paths = (runtime_config_path(workspace_root), workspace_root / "config" / "runtime.local.yaml")
    mtimes = tuple(path.stat().st_mtime_ns if path.exists() else -1 for path in paths)
    key = str(paths[0].resolve())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtimes:
        return copy.deepcopy(cached[1])

    merged = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    for path in paths:
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Runtime config must be a mapping: {path}")
            merged = _deep_merge(merged, loaded)
    _CACHE[key] = (mtimes, merged)
    return copy.deepcopy(merged)


def get_entry_node_id(cfg: dict[str, Any]) -> str:
    """从 runtime config 中获取入口节点 ID。

    优先级：routing.entry_node_id > shell.entry_node_id > 默认值。
    [AutoC 2026-08-01] entry_node_id 从 shell section 迁移到 routing section，
    此函数提供统一读取入口并兼容旧配置。
    """
    _default = "bootstrap.shell_orchestrator"
    val = get_str(cfg, "routing.entry_node_id", "").strip()
    if val:
        return val
    val = get_str(cfg, "shell.entry_node_id", "").strip()
    if val:
        return val
    return _default


def get_str(data: dict[str, Any], dotted_key: str, default: str = "") -> str:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return str(cur) if cur is not None else default


def get_int(data: dict[str, Any], dotted_key: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = get_str(data, dotted_key, "")
    try:
        val = int(raw)
    except Exception:
        val = int(default)
    if min_value is not None:
        val = max(min_value, val)
    if max_value is not None:
        val = min(max_value, val)
    return val


def get_float(data: dict[str, Any], dotted_key: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = get_str(data, dotted_key, "")
    try:
        val = float(raw)
    except Exception:
        val = float(default)
    if min_value is not None:
        val = max(min_value, val)
    if max_value is not None:
        val = min(max_value, val)
    return val


def get_bool(data: dict[str, Any], dotted_key: str, default: bool = False) -> bool:
    raw = get_str(data, dotted_key, "")
    if not raw:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_yaml_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def load_text_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def resolve_env_ref(text: str) -> str:
    s = str(text or "")
    if s.startswith("$ENV{") and s.endswith("}"):
        return os.getenv(s[5:-1], "")
    if s.startswith("${") and s.endswith("}") and len(s) > 3:
        return os.getenv(s[2:-1], "")
    return s


# ---------------------------------------------------------------------------
#  System model slots (compact / summary / image / image_gpt)
# ---------------------------------------------------------------------------
# data/config.yaml:
#   system_models:
#     compact: { model: "...", base_url: "${...}", api_key: "${...}" }
#     summary: { model: "..." }      # url/key follow the node's channel
# Per field: config.yaml system_models.<slot> > CLONOTH_<SLOT>_<FIELD> env >
# caller default. Empty result means "keep the node's own value".
_SYSTEM_MODEL_ENV_PREFIX = {
    "compact": "CLONOTH_COMPACT",
    "summary": "CLONOTH_SUMMARY",
    "image": "CLONOTH_IMAGE",
    "image_gpt": "CLONOTH_IMAGE_GPT",
    "vision": "CLONOTH_VISION",
}
SYSTEM_MODEL_SLOT_NAMES = tuple(_SYSTEM_MODEL_ENV_PREFIX)

# System node id -> slot. Only nodes listed here are overlaid by the runner.
SYSTEM_NODE_MODEL_SLOTS = {
    "system.compactor": "compact",
    "system.turn_summarizer": "summary",
}


def resolve_system_model(
    workspace_root: Path,
    slot: str,
    *,
    default_model: str = "",
    default_base_url: str = "",
    default_api_key: str = "",
) -> tuple[str, str, str]:
    """Resolve (model, base_url, api_key) for a system slot."""
    slot = (slot or "").strip().lower()
    prefix = _SYSTEM_MODEL_ENV_PREFIX.get(slot, "")
    cfg = load_yaml_dict(workspace_root / "data" / "config.yaml") or {}
    sm = cfg.get("system_models")
    block = sm.get(slot) if isinstance(sm, dict) else None
    if not isinstance(block, dict):
        block = {}

    def _pick(field: str, env_suffix: str, fallback: str) -> str:
        v = resolve_env_ref(str(block.get(field) or "").strip())
        if v:
            return v
        if prefix:
            v = os.getenv(f"{prefix}_{env_suffix}", "").strip()
            if v:
                return v
        return fallback

    return (
        _pick("model", "MODEL", default_model),
        _pick("base_url", "BASE_URL", default_base_url),
        _pick("api_key", "API_KEY", default_api_key),
    )


async def fetch_llm_secret(http: httpx.AsyncClient, supervisor_url: str) -> dict[str, Any]:
    """从 supervisor 获取全局 LLM provider 配置（api_key/base_url/model）。"""
    r = await http.get(f"{supervisor_url.rstrip('/')}/v1/config/openai/secret")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}

# 旧名别名，兼容外部调用
fetch_openai_secret = fetch_llm_secret


def normalize_llm_secret(data: dict[str, Any]) -> tuple[str, str, str]:
    """从全局配置 dict 中提取 api_key/base_url/model，支持环境变量引用。"""
    if not isinstance(data, dict):
        return "", "", ""
    api_key = resolve_env_ref(str(data.get("api_key") or "").strip())
    base_url = resolve_env_ref(str(data.get("base_url") or "").strip())
    model = resolve_env_ref(str(data.get("model") or "gpt-4o-mini").strip()) or "gpt-4o-mini"
    return api_key, base_url, model

# 旧名别名，兼容外部调用
normalize_openai_secret = normalize_llm_secret


def load_policy_config(workspace_root: Path) -> dict[str, Any]:
    p = workspace_root / "data" / "policy.yaml"
    data = load_yaml_dict(p)
    return data if isinstance(data, dict) else {}


def parse_extra_roots(workspace_root: Path, raw: Any) -> list[Path]:
    items = raw if isinstance(raw, list) else []
    out: list[Path] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        p = Path(item)
        p = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
        out.append(p)
    return out


def classify_path(
    workspace_root: Path,
    extra_roots: list[Path],
    path_str: str,
    *,
    workspace: Path | None = None,
) -> tuple[Path | None, str, str]:
    """Resolve and classify a filesystem path.

    Returns (resolved, display_path, trust_level):
      - resolved: resolved Path, or None if invalid
      - display_path: workspace-relative posix for workspace paths,
                      absolute posix for external, or error message if invalid
      - trust_level: 'workspace' | 'trusted' | 'external'
          workspace = active working directory (highest trust)
          trusted   = workspace_root or extra_roots (known safe)
          external  = outside all known roots
    """
    # Resolve path relative to effective workspace (or workspace_root as fallback)
    resolve_base = (workspace or workspace_root).resolve()
    try:
        raw = Path(path_str)
        p = raw.resolve() if raw.is_absolute() else (resolve_base / path_str).resolve()
    except Exception as e:
        return None, f"invalid path: {e}", "external"

    # Tier 1: active workspace (highest trust)
    if workspace:
        ws = workspace.resolve()
        try:
            rel = p.relative_to(ws)
            return p, rel.as_posix(), "workspace"
        except ValueError:
            pass

    # Tier 1.5: workspace_root/data/ is system runtime storage (attachments,
    # memory, conversations, temp files). Treated as workspace-level trust even
    # when no active workspace is set, so SDK/frontend auto-approve applies.
    _ws_root = workspace_root.resolve()
    _data_dir = _ws_root / "data"
    try:
        p.relative_to(_data_dir)
        rel = p.relative_to(_ws_root)
        return p, rel.as_posix(), "workspace"
    except ValueError:
        pass

    # Tier 2: workspace_root + extra_roots (trusted)
    trusted_roots = [_ws_root]
    trusted_roots.extend(r.resolve() for r in extra_roots)
    for r in trusted_roots:
        try:
            rel = p.relative_to(r)
            display = rel.as_posix() if r == _ws_root else p.as_posix()
            return p, display, "trusted"
        except ValueError:
            continue

    # Tier 3: external
    if raw.is_absolute():
        return p, p.as_posix(), "external"

    # Relative path that escapes all roots — invalid
    return None, "path escapes workspace root", "external"


def strip_tool_trace_blocks(text: str) -> str:
    s = str(text or "")
    start = "[CLONOTH_TOOL_TRACE v2]"
    end = "[/CLONOTH_TOOL_TRACE]"
    while True:
        i = s.find(start)
        if i < 0:
            break
        j = s.find(end, i)
        if j < 0:
            s = s[:i].rstrip()
            break
        s = (s[:i] + s[j + len(end):]).strip()
    return s.strip()
