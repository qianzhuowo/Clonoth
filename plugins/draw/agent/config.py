"""绘图 agent 配置装载：settings / 预设 / 提示词模板 / 布局 / 角色库，按工作区定位。

不 import tools/drawtools（工具是独立子进程协议，引擎的 ToolRegistry 会把工作区 tools/ 插到 sys.path，
跨进程依赖会因 sys.path 顺序不同而炸）；这里自持一份默认值与小工具，与 tools/drawtools/common.py 保持同义。
文件按 workspace_root + plugins/draw/paths.drawtools_file 规则找（私有文件优先，缺失回退 *.example.*），
引擎进程与管理面板共用，离线测试可用 tmp 工作区。
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .. import characters as character_rules
from ..paths import drawtools_file
from ..planner.profile import PlannerProfile, novelai_profile_for_model
from ..presets import DEFAULT_PRESET, clean_preset, is_v5_model
from .llm import current_preset, normalize_agent_settings

logger = logging.getLogger(__name__)

# 与 tools/drawtools/common.py 的 DEFAULT_SETTINGS 同义；预设默认值 DEFAULT_PRESET 在 plugins/draw/presets.py。
DEFAULT_SETTINGS: dict[str, Any] = {
    "api": {"base_url": "https://image.novelai.net", "api_key_env": "NOVELAI_API_KEY", "api_key": "", "timeout_sec": 130},
    "generation": {"request_delay_sec": 0, "retry_wait_sec": 3, "retry_max_attempts": 5, "lock_timeout_sec": 3600},
    "storage": {"cleanup_enabled": True, "retention_days": 7, "max_total_mb": 2048, "cleanup_interval_sec": 3600},
    "params": {
        "selected_preset_id": "default-v45-full",
        "size_options": {"横图": {"width": 1216, "height": 832}, "竖图": {"width": 832, "height": 1216}, "方图": {"width": 1024, "height": 1024}},
        "presets": [],
    },
    "character_tags": {"path": "tools/drawtools/character_tags.yaml", "auto_search_unknown": True},
}


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for k, v in override.items():
            out[k] = deep_merge(out.get(k), v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


def legacy_override_size(settings: Mapping[str, Any]) -> Any:
    """旧的全局画幅设置 planner.override_size：只给还没写自己 override_size 的预设用。"""
    planner = settings.get("planner") if isinstance(settings.get("planner"), Mapping) else {}
    return planner.get("override_size") or "auto"


def iter_presets(settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """全部预设，已补齐默认值并清洗（不合法的值换成能用的），出图和面板读到的是同一份。"""
    params = settings.get("params") if isinstance(settings.get("params"), Mapping) else {}
    presets = params.get("presets") if isinstance(params.get("presets"), list) else []
    legacy = legacy_override_size(settings)
    return [clean_preset(deep_merge(DEFAULT_PRESET, p), strict=False, legacy_size=legacy)
            for p in presets if isinstance(p, Mapping)] or [clean_preset(DEFAULT_PRESET, strict=False, legacy_size=legacy)]


def _preset_keys(preset: Mapping[str, Any]) -> set[str]:
    aliases = preset.get("aliases") if isinstance(preset.get("aliases"), list) else []
    return {str(preset.get("id") or "").strip().lower(), str(preset.get("name") or "").strip().lower(),
            *(str(a).strip().lower() for a in aliases)} - {""}


def find_preset(settings: Mapping[str, Any], ref: str) -> dict[str, Any] | None:
    key = str(ref or "").strip().lower()
    if not key:
        return None
    return next((p for p in iter_presets(settings) if key in _preset_keys(p)), None)


def selected_preset(settings: Mapping[str, Any]) -> dict[str, Any]:
    params = settings.get("params") if isinstance(settings.get("params"), Mapping) else {}
    return find_preset(settings, str(params.get("selected_preset_id") or "")) or iter_presets(settings)[0]


def read_workspace_dotenv(workspace_root: Path) -> dict[str, str]:
    env_path = Path(workspace_root) / ".env"
    kv: dict[str, str] = {}
    try:
        if not env_path.exists():
            return kv
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip().strip("'\"")
    except OSError:
        return {}
    return kv


def resolve_api_key(settings: Mapping[str, Any], workspace_root: Path | None = None) -> str:
    """settings.yaml 显式 key 优先；空则环境变量；再空则工作区 .env。不记录、不输出 key 值。"""
    api = settings.get("api") if isinstance(settings.get("api"), Mapping) else {}
    key = str(api.get("api_key") or "").strip()
    if key:
        return key
    env_name = str(api.get("api_key_env") or "NOVELAI_API_KEY").strip()
    if not env_name:
        return ""
    value = os.environ.get(env_name, "").strip()
    if not value and workspace_root is not None:
        value = read_workspace_dotenv(workspace_root).get(env_name, "").strip()
    return value


def resolve_base_url(settings: Mapping[str, Any]) -> str:
    api = settings.get("api") if isinstance(settings.get("api"), Mapping) else {}
    return str(api.get("base_url") or "https://image.novelai.net").strip().rstrip("/")

PROMPT_FILES = {
    "opening": "prompts/novelai/top-system.md",
    "guide": "prompts/novelai/tag-guide.md",
    "guide_v5": "prompts/novelai/tag-guide-v5.md",
    "rules": "prompts/novelai/scene-rules.md",
}


def guide_key_for_preset(preset: Mapping[str, Any]) -> str:
    """这套预设的模型用哪份 TAG 指南：V5 模型用 guide_v5，其它用 guide。"""
    params = preset.get("params") if isinstance(preset.get("params"), Mapping) else {}
    return "guide_v5" if is_v5_model(params.get("model")) else "guide"

DEFAULT_PLANNER_CFG: dict[str, Any] = {
    "tool_protocol": "native",
    "max_attempts": 3,
    "max_custom_tool_calls": 5,
    "timeout_sec": 180,
    "max_plan_images": 4,
    "override_size": "auto",
    "send_each_image": True,
    "layout": None,
    "field_descriptions": {},
    "content_template": "",
}


def _read_text(workspace_root: Path, rel_path: str) -> str:
    path = drawtools_file(workspace_root, rel_path)
    if path is None:
        logger.warning("draw agent: 缺少 %s（私有文件与 example 模板都不存在）", rel_path)
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("draw agent: 读取 %s 失败: %s", rel_path, exc)
        return ""


def load_draw_settings(workspace_root: Path) -> dict[str, Any]:
    path = drawtools_file(workspace_root, "settings.yaml")
    raw: Any = {}
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("draw agent: 解析 settings 失败: %s", exc)
    settings = deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
    planner = settings.get("planner") if isinstance(settings.get("planner"), dict) else {}
    # 已废弃的字段：规划不再附带近期对话，旧配置里的值直接丢掉（下次保存即从文件消失）。
    planner.pop("recent_dialogue_turns", None)
    settings["planner"] = deep_merge(DEFAULT_PLANNER_CFG, planner)
    # 场景 Agent 预设（规划模型的接口配置）。没有预设时由上面的 planner.tool_protocol / timeout_sec 生成“默认”。
    settings["agent"] = normalize_agent_settings(settings.get("agent"), settings["planner"])
    return settings


def load_characters(workspace_root: Path) -> list[dict[str, Any]]:
    path = drawtools_file(workspace_root, "character_tags.yaml")
    if path is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("draw agent: 读取角色库失败: %s", exc)
        return []
    # 只留参与出图的：角色和所在分组都启用，停用的服装 / 动态标签条目去掉。
    return character_rules.active_characters(data)


@dataclass
class PlannerConfig:
    settings: dict[str, Any]
    planner: dict[str, Any]
    preset: dict[str, Any]
    profile: PlannerProfile
    texts: dict[str, str]
    characters: list[dict[str, Any]] = field(default_factory=list)
    workspace_root: Path | None = None
    agent: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_preset(self) -> dict[str, Any]:
        """当前选中的场景 Agent 预设（规划模型的接口、工具协议、超时）。"""
        return current_preset(self.agent)

    @property
    def api_key(self) -> str:
        return resolve_api_key(self.settings, self.workspace_root)

    @property
    def base_url(self) -> str:
        return resolve_base_url(self.settings)

    def preset_for(self, ref: Any) -> dict[str, Any]:
        """任务里的 preset 字段：找得到用它，找不到用当前默认预设。"""
        found = find_preset(self.settings, str(ref or "")) if ref else None
        return found or self.preset


def load_planner_config(workspace_root: Path | str) -> PlannerConfig:
    root = Path(workspace_root)
    settings = load_draw_settings(root)
    preset = selected_preset(settings)
    texts = {key: _read_text(root, rel) for key, rel in PROMPT_FILES.items()}
    # 规划用的指南跟着当前默认预设的模型走；cfg.texts["guide"] 就是生效的那份。
    texts["guide"] = texts[guide_key_for_preset(preset)]
    return PlannerConfig(
        settings=settings,
        planner=dict(settings["planner"]),
        preset=preset,
        profile=novelai_profile_for_model((preset.get("params") or {}).get("model")),
        texts=texts,
        characters=load_characters(root),
        workspace_root=root,
        agent=dict(settings["agent"]),
    )
