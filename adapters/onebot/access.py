"""QQ access control: group/private allow-lists and approval admins.

Pure functions without nonebot imports so they can be unit-tested; the
NoneBot rules in the adapter entry only adapt events to these checks.

Rules (all allow-lists, an empty list never opens access):
- group: only ``ALLOWED_GROUPS``;
- private: admins, ``ALLOWED_PRIVATE_USERS``, or friends when
  ``ALLOW_PRIVATE_FRIENDS`` is on;
- approval: only ``ADMIN_QQ_USERS``.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from . import config


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def is_group_allowed(group_id: Any, allowed_groups: Optional[Iterable[int]] = None) -> bool:
    """Whether a group may use Clonoth; an empty allow-list allows no group."""
    gid = _as_int(group_id)
    groups = config.ALLOWED_GROUPS if allowed_groups is None else allowed_groups
    return gid is not None and gid in set(groups)


def is_admin_user(user_id: Any, admin_users: Optional[Iterable[int]] = None) -> bool:
    """Whether a QQ user may approve/deny Clonoth approvals."""
    uid = _as_int(user_id)
    admins = config.ADMIN_QQ_USERS if admin_users is None else admin_users
    return uid is not None and uid in set(admins)


def is_private_allowed(
    user_id: Any,
    sub_type: Any = "",
    *,
    admin_users: Optional[Iterable[int]] = None,
    allowed_private_users: Optional[Iterable[int]] = None,
    allow_friends: Optional[bool] = None,
) -> bool:
    """Whether a private chat may use Clonoth."""
    uid = _as_int(user_id)
    if uid is None:
        return False
    if is_admin_user(uid, admin_users):
        return True
    allowed = config.ALLOWED_PRIVATE_USERS if allowed_private_users is None else allowed_private_users
    if uid in set(allowed):
        return True
    friends = config.ALLOW_PRIVATE_FRIENDS if allow_friends is None else allow_friends
    return bool(friends) and str(sub_type or "").strip().lower() == "friend"
