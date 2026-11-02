"""QQ user profiles: how the model should address users.

Profiles only change display names and tone hints; they never grant
permissions (admins come from ``ADMIN_QQ_USERS`` only). The profile file holds
real QQ ids, so it stays out of git.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from . import config

logger = logging.getLogger(__name__)

_PROFILE_FIELDS = {
    "display_name": ("display_name", "name"),
    "address_as": ("address_as", "call_as"),
    "title": ("title", "role"),
    "note": ("note", "style_note"),
}


def sanitize_name(name: str, max_len: int = 32) -> str:
    """One-line name without brackets, so it cannot break the input format."""
    name = (name or "").replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")").strip()
    return (name[:max_len] + "…") if len(name) > max_len else (name or "未知成员")


def load_profiles(path_text: str | Path | None, workspace: str | Path | None = None) -> Dict[str, Dict[str, str]]:
    """Load ``{qq: {display_name, address_as, title, note}}`` from JSON/YAML."""
    path_text = str(path_text or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (Path(workspace or config.CLONOTH_WORKSPACE) / path).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("failed to load QQ user profiles: %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    users = data.get("users") if isinstance(data.get("users"), dict) else data
    profiles: Dict[str, Dict[str, str]] = {}
    for raw_uid, raw_profile in users.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_profile, dict):
            continue
        profile = {}
        for field, keys in _PROFILE_FIELDS.items():
            value = next((str(raw_profile.get(k) or "").strip() for k in keys if raw_profile.get(k)), "")
            if value:
                profile[field] = value
        profiles[uid] = profile
    return profiles


class UserProfiles:
    """Loaded profiles plus the name lookups the adapter needs."""

    def __init__(self, profiles: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self.profiles: Dict[str, Dict[str, str]] = dict(profiles or {})

    def reload(self, path_text: str | Path | None = None, workspace: str | Path | None = None) -> None:
        self.profiles = load_profiles(config.USER_PROFILES_PATH if path_text is None else path_text, workspace)

    def profile(self, user_id: Any) -> Dict[str, str]:
        return dict(self.profiles.get(str(user_id or "").strip()) or {})

    def display_name(self, user_id: Any) -> str:
        name = str(self.profile(user_id).get("display_name") or "").strip()
        return sanitize_name(name) if name else ""

    def aliases(self, user_id: Any) -> List[str]:
        """Names usable in ``[at:...]`` and proactive-send target lookup."""
        profile = self.profile(user_id)
        return [profile[k] for k in ("display_name", "address_as", "title") if profile.get(k)]

    def name_index(self) -> Iterable[Tuple[str, List[str]]]:
        """``(uid, names)`` pairs for :meth:`AnonMap.resolve_at_alias_to_real`."""
        for uid in list(self.profiles):
            yield uid, [self.display_name(uid), *self.aliases(uid)]

    def sender_display_name(self, sender: Any, user_id: Any, anon_map: Any) -> str:
        """Profile name > group card > nickname > anonymous alias; never a raw QQ id."""
        profile_name = self.display_name(user_id)
        if profile_name:
            return profile_name
        card = (sender.get("card") if isinstance(sender, dict) else getattr(sender, "card", "")) or ""
        nickname = (sender.get("nickname") if isinstance(sender, dict) else getattr(sender, "nickname", "")) or ""
        if card or nickname:
            return sanitize_name(card or nickname)
        return anon_map.anonymize_user_id(user_id) if str(user_id or "").strip() else "未知成员"

    def identity_lines(self, user_id: Any, display_name: str, anon_map: Any, *, is_admin: bool) -> List[str]:
        """Identity/addressing hints shown to the model; permissions are decided in code."""
        profile = self.profile(user_id)
        lines = [
            f"稳定用户标识: {anon_map.anonymize_user_id(user_id)}",
            f"显示名: {sanitize_name(display_name)}",
        ]
        if profile.get("address_as"):
            lines.append(f"称呼要求: 请称呼该用户为「{sanitize_name(profile['address_as'])}」")
        if profile.get("title"):
            lines.append(f"身份标签: {sanitize_name(profile['title'])}")
        if profile.get("note"):
            lines.append(f"称呼/语气备注: {sanitize_name(profile['note'], max_len=80)}")
        lines.append(f"Clonoth 管理员: {'是' if is_admin else '否'}")
        lines.append("权限说明: 管理员权限仅由系统配置判定，以上称呼配置不授予任何权限。")
        return lines
