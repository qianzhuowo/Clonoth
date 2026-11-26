from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

import clonoth_channels as chn

from .types import (
    AppConfigPublic,
    ChannelSummary,
    MimoAudioConfigPublic,
    MimoAudioConfigSecret,
    MimoAudioConfigUpdateIn,
)


def _resolve_env_value(value: str) -> str:
    return chn.resolve_ref(value)


def _redact_api_key(api_key: str) -> tuple[bool, str]:
    k = (api_key or "").strip()
    if not k:
        return False, ""
    if k.startswith("${") and k.endswith("}") and len(k) > 3:
        return True, f"<env:{k[2:-1].strip()}>"
    if len(k) <= 4:
        return True, "****"
    return True, "****" + k[-4:]


_CHANNEL_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


class ConfigStore:
    """YAML-backed store for data/config.yaml (channels, fallback order, node choices, audio).

    The layout is described in ``clonoth_channels``; reading an older file migrates
    it once and writes it back. api_key values are secrets (data/config.yaml is ignored by git).
    """

    def __init__(self, *, path: Path):
        self.path = path
        # data/config.yaml -> workspace root (node YAML files migrate with the config).
        self.root = path.parent.parent
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    # ---- raw file ----

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except Exception:
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            if not bak.exists():
                self.path.replace(bak)
            return {"version": 1}
        return data if isinstance(data, dict) else {"version": 1}

    def _save_raw(self, data: dict[str, Any]) -> None:
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def reload(self) -> dict[str, Any]:
        with self._lock:
            existed = self.path.exists()
            data, changed = chn.migrate(self._load_raw(), root=self.root)
            if changed or not existed:
                self._save_raw(data)
            return data

    # ---- public summary ----

    def get_public(self) -> AppConfigPublic:
        with self._lock:
            data = self._load_raw()
            summaries = [
                ChannelSummary(id=str(c["id"]), name=str(c.get("name") or c["id"]),
                               type=str(c.get("type") or ""), model=chn.resolve_ref(c.get("model"), self.root))
                for c in chn.channel_list(data)
            ]
            main_model = next((s.model for s in summaries if s.id == chn.MAIN), "")
            return AppConfigPublic(version=int(data.get("version") or 1), main_model=main_model,
                                   channels=summaries, mimo_audio=self.get_mimo_audio_public())

    def _vision(self, caps: dict[str, Any], model: str) -> bool:
        from engine.vision import vision_from_capabilities
        return vision_from_capabilities(caps, model)

    def get_channels_public(self) -> dict[str, Any]:
        """Channels (each with its role), the fallback order and node choices; api_keys redacted."""
        with self._lock:
            data = self._load_raw()
            caps = data.get("model_capabilities") if isinstance(data.get("model_capabilities"), dict) else {}
            channels = []
            for c in chn.channel_list(data):
                present, redacted = _redact_api_key(str(c.get("api_key") or ""))
                model = chn.resolve_ref(c.get("model"), self.root)
                base_url = str(c.get("base_url") or "")
                kind = str(c.get("type") or "")
                cid = str(c["id"])
                channels.append({
                    "id": cid, "name": str(c.get("name") or cid), "type": kind,
                    "role": chn.role_of(data, cid), "enabled": c.get("enabled") is not False,
                    "base_url": base_url, "base_url_resolved": chn.resolve_ref(base_url, self.root),
                    "model": model, "api_key_present": present, "api_key_redacted": redacted,
                    "vision": self._vision(caps, model),
                    "type_hint": chn.type_mismatch(kind, chn.resolve_ref(base_url, self.root)),
                })
            return {
                "channels": channels,
                "fallbacks": chn.fallback_ids(data),
                "roles": dict(chn.ROLE_CHANNELS),
                "node_models": {k: self._ref_public(data, "node_models", k)
                                for k in (data.get("node_models") or {})},
                "model_capabilities": caps,
            }

    @staticmethod
    def _ref_public(data: dict[str, Any], scope: str, key: str) -> dict[str, str]:
        block = data.get(scope) if isinstance(data.get(scope), dict) else {}
        entry = block.get(key) if isinstance(block.get(key), dict) else {}
        return {"channel": str(entry.get("channel") or ""), "model": str(entry.get("model") or "")}

    # ---- channels ----

    def get_channel_secret(self, channel_id: str) -> chn.Channel:
        with self._lock:
            return chn.get_channel(self._load_raw(), channel_id, root=self.root)

    def upsert_channel(self, channel_id: str, *, name: str | None = None, type: str | None = None,
                       base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                       enabled: bool | None = None, clear_api_key: bool = False,
                       copy_key_from: str | None = None) -> dict[str, Any]:
        cid = (channel_id or "").strip()
        if not cid or not set(cid) <= _CHANNEL_ID_CHARS or len(cid) > 64:
            raise ValueError("渠道 id 只能用字母、数字、_ . -，最长 64 位")
        with self._lock:
            data = self._load_raw()
            channels = [dict(c) for c in chn.channel_list(data)]
            entry = next((c for c in channels if str(c["id"]) == cid), None)
            if entry is None:
                if not (type or "").strip():
                    raise ValueError("新渠道必须选择格式")
                entry = {"id": cid, "name": chn.ROLE_CHANNELS.get(cid, cid), "type": "",
                         "base_url": "", "api_key": "", "model": ""}
                channels.append(entry)
            for key, value in (("name", name), ("type", type), ("base_url", base_url), ("model", model)):
                if value is not None:
                    entry[key] = value.strip().lower() if key == "type" else value.strip()
            if api_key is not None and api_key.strip():
                entry["api_key"] = api_key.strip()
            elif clear_api_key:
                entry["api_key"] = ""
            elif copy_key_from and not str(entry.get("api_key") or "").strip():
                src = next((c for c in channels if str(c["id"]) == copy_key_from.strip()), None)
                if src is None:
                    raise ValueError(f"渠道不存在：{copy_key_from}")
                entry["api_key"] = src.get("api_key") or ""
            if enabled is not None:
                if cid == chn.MAIN and not enabled:
                    raise ValueError("主模型不能关闭")
                if enabled:
                    entry.pop("enabled", None)
                else:
                    entry["enabled"] = False
            entry["name"] = entry.get("name") or chn.ROLE_CHANNELS.get(cid, cid)
            data["channels"] = channels
            self._save_raw(data)
            return self.get_channels_public()

    def delete_channel(self, channel_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._load_raw()
            if channel_id == chn.MAIN:
                raise ValueError("主模型不能删除")
            used = chn.references(data, channel_id)
            if used:
                raise ValueError(f"渠道「{channel_id}」正在使用：{'、'.join(used)}；请先改掉这些引用")
            data["channels"] = [c for c in chn.channel_list(data) if str(c["id"]) != channel_id]
            if "fallbacks" in data:
                data["fallbacks"] = [e for e in (data.get("fallbacks") or [])
                                     if not (isinstance(e, dict) and e.get("channel") == channel_id)]
            self._save_raw(data)
            return self.get_channels_public()

    def set_main_model(self, model: str) -> str:
        """Change the main model (chat commands); returns its channel id."""
        model = (model or "").strip()
        if not model:
            raise ValueError("model is required")
        with self._lock:
            data = self._load_raw()
            channels = [dict(c) for c in chn.channel_list(data)]
            main = next((c for c in channels if str(c["id"]) == chn.MAIN), None)
            if main is None:
                raise ValueError("还没有配置主模型")
            main["model"] = model
            data["channels"] = channels
            self._save_raw(data)
            return chn.MAIN

    # ---- fallbacks / capabilities ----

    def update_fallbacks(self, channel_ids: list[str]) -> dict[str, Any]:
        """Set the fallback order. Options already on an entry are kept."""
        with self._lock:
            data = self._load_raw()
            ids = {str(c["id"]) for c in chn.channel_list(data)}
            old = {str(e.get("channel")): e for e in (data.get("fallbacks") or []) if isinstance(e, dict)}
            chain: list[dict[str, Any]] = []
            for cid in channel_ids:
                cid = str(cid or "").strip()
                if cid not in ids:
                    raise ValueError(f"备用链路引用了不存在的渠道：{cid or '（空）'}")
                if cid in chn.ROLE_CHANNELS:
                    raise ValueError(f"「{chn.ROLE_CHANNELS[cid]}」不能当作备用链路")
                chain.append({**old.get(cid, {}), "channel": cid})
            data["fallbacks"] = chain
            self._save_raw(data)
            return self.get_channels_public()

    def set_model_vision(self, model: str, vision: bool) -> dict[str, Any]:
        model = (model or "").strip()
        if not model:
            raise ValueError("model is required")
        with self._lock:
            data = self._load_raw()
            caps = data.get("model_capabilities") if isinstance(data.get("model_capabilities"), dict) else {}
            entry = caps.get(model) if isinstance(caps.get(model), dict) else {}
            entry["vision"] = bool(vision)
            caps[model] = entry
            data["model_capabilities"] = caps
            self._save_raw(data)
            return self.get_channels_public()

    # ---- node models ----

    def update_node_model(self, node_id: str, *, channel: str | None = None, model: str | None = None) -> dict[str, str]:
        with self._lock:
            data = self._load_raw()
            block = data.get("node_models") if isinstance(data.get("node_models"), dict) else {}
            entry = dict(block.get(node_id)) if isinstance(block.get(node_id), dict) else {}
            if channel is not None:
                cid = channel.strip()
                if cid and cid not in {str(c["id"]) for c in chn.channel_list(data)}:
                    raise ValueError(f"渠道不存在：{cid}")
                entry["channel"] = cid
            if model is not None:
                entry["model"] = model.strip()
            entry = {k: v for k, v in entry.items() if v}
            if entry:
                block[node_id] = entry
            else:
                block.pop(node_id, None)
            data["node_models"] = block
            self._save_raw(data)
            return {"node_id": node_id, **self._ref_public(data, "node_models", node_id)}

    # ---- mimo audio ----

    def _mimo_raw(self) -> MimoAudioConfigSecret:
        raw = self._load_raw().get("mimo_audio")
        try:
            return MimoAudioConfigSecret.model_validate(raw if isinstance(raw, dict) else {})
        except Exception:
            return MimoAudioConfigSecret()

    def get_mimo_audio_secret(self) -> MimoAudioConfigSecret:
        with self._lock:
            cfg = self._mimo_raw()
            cfg.base_url = _resolve_env_value(cfg.base_url) or "https://api.xiaomimimo.com"
            cfg.api_key = _resolve_env_value(cfg.api_key)
            cfg.asr_path = _resolve_env_value(cfg.asr_path) or "/v1/chat/completions"
            cfg.asr_model = _resolve_env_value(cfg.asr_model) or "mimo-v2.5-asr"
            cfg.asr_language = _resolve_env_value(cfg.asr_language) or "auto"
            cfg.tts_path = _resolve_env_value(cfg.tts_path) or "/v1/chat/completions"
            cfg.tts_model = _resolve_env_value(cfg.tts_model) or "mimo-v2.5-tts"
            cfg.tts_voice = _resolve_env_value(cfg.tts_voice) or "mimo_default"
            cfg.tts_format = _resolve_env_value(cfg.tts_format) or "wav"
            cfg.timeout = _resolve_env_value(cfg.timeout) or "60"
            cfg.ffmpeg_path = _resolve_env_value(cfg.ffmpeg_path) or os.getenv("FFMPEG_PATH", "").strip()
            return cfg

    def get_mimo_audio_public(self) -> MimoAudioConfigPublic:
        with self._lock:
            cfg = self.get_mimo_audio_secret()
            present, redacted = _redact_api_key(cfg.api_key)
            return MimoAudioConfigPublic(
                base_url=cfg.base_url, api_key_present=present, api_key=redacted,
                asr_path=cfg.asr_path, asr_model=cfg.asr_model, asr_language=cfg.asr_language,
                tts_path=cfg.tts_path, tts_model=cfg.tts_model, tts_voice=cfg.tts_voice,
                tts_format=cfg.tts_format, timeout=cfg.timeout, ffmpeg_path=cfg.ffmpeg_path,
            )

    def update_mimo_audio(self, update: MimoAudioConfigUpdateIn) -> MimoAudioConfigPublic:
        with self._lock:
            data = self._load_raw()
            block = self._mimo_raw()
            for key in ("base_url", "api_key", "asr_path", "asr_model", "asr_language", "tts_path",
                        "tts_model", "tts_voice", "tts_format", "timeout", "ffmpeg_path"):
                value = getattr(update, key)
                if value is not None:
                    setattr(block, key, value.strip())
            data["mimo_audio"] = block.model_dump(mode="json")
            self._save_raw(data)
            return self.get_mimo_audio_public()
