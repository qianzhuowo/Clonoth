"""QQ anonymisation and stable conversation keys.

- Real QQ/group ids are replaced with stable aliases (UserA, GroupB, ...)
  before text reaches the model; the alias map is persisted so aliases stay
  the same across restarts and can be reversed for ``[at:UserA]``.
- Conversation keys sent to the Supervisor are ``qq_group:<digest>`` /
  ``qq_private:<digest>`` (HMAC when a secret is set); the real key stays in
  this adapter. Memory isolation hashes this stable key again, so the memory
  namespace never contains a real id either.

The alias map is a de-anonymisation dictionary: keep it out of git.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from . import config

logger = logging.getLogger(__name__)

GROUP_KEY_PREFIX = "qq_group:"
PRIVATE_KEY_PREFIX = "qq_private:"
# QQ ids have at least 5 digits; shorter numbers are model inventions like qq=1.
_MIN_REAL_QQ_LEN = 5

ProfileNames = Callable[[], Iterable[Tuple[str, Iterable[str]]]]


def alias_from_index(prefix: str, index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (spreadsheet-style column names)."""
    letters = ""
    index = max(0, index)
    while True:
        index, rem = divmod(index, 26)
        letters = chr(ord("A") + rem) + letters
        if index == 0:
            break
        index -= 1
    return f"{prefix}{letters}"


def alias_to_index(prefix: str, alias: str) -> int:
    """Inverse of :func:`alias_from_index`; invalid aliases return -1."""
    if not alias.startswith(prefix):
        return -1
    letters = alias[len(prefix):]
    if not letters or not letters.isalpha() or not letters.isupper():
        return -1
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def is_plausible_qq_id(value: Any) -> bool:
    real = str(value or "").strip()
    return real.isdigit() and len(real) >= _MIN_REAL_QQ_LEN


def _id_from_key(conversation_key: str, prefix: str) -> Optional[int]:
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def group_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    return _id_from_key(conversation_key, GROUP_KEY_PREFIX)


def private_user_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    return _id_from_key(conversation_key, PRIVATE_KEY_PREFIX)


class AnonMap:
    """Alias tables, stable/real conversation keys and throttled persistence."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        save_min_interval: Optional[float] = None,
        hash_secret: Optional[str] = None,
    ) -> None:
        self.path = Path(path or config.ANON_MAP_FILE)
        self.save_min_interval = (
            config.ANON_MAP_SAVE_MIN_INTERVAL if save_min_interval is None else max(0.0, save_min_interval)
        )
        self.hash_secret = config.CONVERSATION_HASH_SECRET if hash_secret is None else hash_secret
        self.users: Dict[str, str] = {}
        self.groups: Dict[str, str] = {}
        self.user_reverse: Dict[str, str] = {}
        self.group_reverse: Dict[str, str] = {}
        # Explicit counters: never infer the next alias from len(), so aliases
        # stay unique after dropping dirty entries.
        self.user_next = 0
        self.group_next = 0
        self.dirty = False
        self.last_saved_at = 0.0
        self.real_conversation_keys: Dict[str, str] = {}
        self.stable_conversation_keys: Dict[str, str] = {}
        self._save_task: Optional[asyncio.Task[Any]] = None
        self._lock = asyncio.Lock()

    # ---- persistence -------------------------------------------------
    def load(self) -> None:
        """Restore aliases and counters from disk; the file wins over memory."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load OneBot anon map: %s", self.path, exc_info=True)
            return
        if not isinstance(data, dict):
            return
        max_user = self._load_table(data.get("users"), "User", self.users, self.user_reverse, drop_short=True)
        max_group = self._load_table(data.get("groups"), "Group", self.groups, self.group_reverse, drop_short=False)
        self.user_next = max(0, self._saved_next(data.get("user_next"), max_user))
        self.group_next = max(0, self._saved_next(data.get("group_next"), max_group))
        logger.info("loaded OneBot anon map (users=%d groups=%d)", len(self.users), len(self.groups))

    def _load_table(
        self, items: Any, prefix: str, table: Dict[str, str], reverse: Dict[str, str], *, drop_short: bool
    ) -> int:
        max_index = -1
        dropped = 0
        if not isinstance(items, dict):
            return max_index
        for real, item in items.items():
            real = str(real or "").strip()
            alias = str((item or {}).get("alias") or "").strip() if isinstance(item, dict) else ""
            if not real or not alias:
                continue
            if drop_short and real.isdigit() and not is_plausible_qq_id(real):
                dropped += 1
                continue
            table[real] = alias
            reverse[alias] = real
            max_index = max(max_index, alias_to_index(prefix, alias))
        if dropped:
            logger.warning("dropped %d invalid short-qq entries from anon map", dropped)
            self.mark_dirty()
        return max_index

    @staticmethod
    def _saved_next(value: Any, max_index: int) -> int:
        try:
            return max(int(value or 0), max_index + 1)
        except (TypeError, ValueError):
            return max_index + 1

    def mark_dirty(self) -> None:
        """Schedule one trailing save; bursts within the window share it."""
        self.dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync context): the next explicit save writes it
        if self._save_task is not None and not self._save_task.done():
            return
        self._save_task = loop.create_task(self._save_throttled())

    async def _save_throttled(self) -> None:
        try:
            if self.save_min_interval > 0:
                await asyncio.sleep(self.save_min_interval)
            await self.save()
        finally:
            self._save_task = None

    async def save(self) -> None:
        """Atomically write the alias map (tmp file + replace) when dirty."""
        async with self._lock:
            if not self.dirty:
                return
            now = time.time()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "version": 1,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "user_next": self.user_next,
                    "group_next": self.group_next,
                    "users": {real: {"alias": alias, "last_seen": now} for real, alias in self.users.items()},
                    "groups": {real: {"alias": alias, "last_seen": now} for real, alias in self.groups.items()},
                }
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self.path)
                self.dirty = False
                self.last_saved_at = time.time()
            except Exception:
                logger.warning("failed to save OneBot anon map: %s", self.path, exc_info=True)

    async def flush(self) -> None:
        """Shutdown: cancel the pending timer and write immediately."""
        task, self._save_task = self._save_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.save()

    # ---- aliases -----------------------------------------------------
    def anonymize_user_id(self, user_id: Any) -> str:
        real = str(user_id or "").strip()
        if not real or (real.isdigit() and not is_plausible_qq_id(real)):
            return "UserUnknown"
        alias = self.users.get(real)
        if alias is None:
            alias = alias_from_index("User", self.user_next)
            self.user_next += 1
            self.users[real] = alias
            self.user_reverse[alias] = real
            self.mark_dirty()
        return alias

    def anonymize_group_id(self, group_id: Any) -> str:
        real = str(group_id or "").strip()
        if not real:
            return "GroupUnknown"
        alias = self.groups.get(real)
        if alias is None:
            alias = alias_from_index("Group", self.group_next)
            self.group_next += 1
            self.groups[real] = alias
            self.group_reverse[alias] = real
            self.mark_dirty()
        return alias

    def resolve_at_alias_to_real(self, token: str, profile_names: Optional[ProfileNames] = None) -> str:
        """Map a model-written ``[at:UserA]`` / display name back to a real QQ id.

        Returns "" when unresolved so the caller can fall back to plain text.
        """
        raw = str(token or "").strip().lstrip("@").strip()
        if not raw:
            return ""
        if raw.isdigit():
            return raw
        real = self.user_reverse.get(raw)
        if real:
            return real
        if profile_names is None:
            return ""
        norm = raw.casefold()
        try:
            for uid, names in profile_names():
                if any(name and str(name).strip().casefold() == norm for name in names):
                    return str(uid)
        except Exception:
            logger.debug("resolve at alias by profile failed for %r", raw, exc_info=True)
        return ""

    def anonymize_text(self, text: str) -> str:
        """Replace only ids already registered at collection points.

        No generic digit regex: amounts, codes and dates must stay intact.
        Longer ids first so a short id never cuts into a longer one.
        """
        if not text:
            return ""
        safe = str(text)
        for table in (self.groups, self.users):
            for real, alias in sorted(table.items(), key=lambda item: len(item[0]), reverse=True):
                safe = re.sub(rf"(?<!\d){re.escape(real)}(?!\d)", alias, safe)
        return safe

    # ---- conversation keys --------------------------------------------
    def conversation_digest(self, conversation_key: str) -> str:
        raw = str(conversation_key or "").strip().encode("utf-8")
        secret = (self.hash_secret or "").encode("utf-8")
        if secret:
            return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:24]
        return hashlib.sha256(raw).hexdigest()[:24]

    def stable_conversation_key(self, real_conversation_key: str) -> str:
        """``qq_group:<real>`` -> ``qq_group:<digest>`` and remember the mapping."""
        if real_conversation_key.startswith(GROUP_KEY_PREFIX):
            prefix = "qq_group"
            self.anonymize_group_id(real_conversation_key[len(GROUP_KEY_PREFIX):])
        elif real_conversation_key.startswith(PRIVATE_KEY_PREFIX):
            prefix = "qq_private"
            self.anonymize_user_id(real_conversation_key[len(PRIVATE_KEY_PREFIX):])
        else:
            prefix = "qq_unknown"
        stable = f"{prefix}:{self.conversation_digest(real_conversation_key)}"
        self.real_conversation_keys[stable] = real_conversation_key
        self.stable_conversation_keys[real_conversation_key] = stable
        return stable

    def real_conversation_key(self, conversation_key: str) -> str:
        return self.real_conversation_keys.get(conversation_key, conversation_key)

    def restore_conversation_keys(self, real_map: Any) -> None:
        """Load the stable -> real map persisted with the route state."""
        if not isinstance(real_map, dict):
            return
        for stable, real in real_map.items():
            stable, real = str(stable or ""), str(real or "")
            if stable and real:
                self.real_conversation_keys[stable] = real
                self.stable_conversation_keys[real] = stable

    def target_from_conversation_key(self, conversation_key: str) -> Optional[Dict[str, Any]]:
        """Stable or real key -> QQ send target ({type, group_id|user_id})."""
        real = self.real_conversation_key(conversation_key)
        group_id = group_id_from_conversation_key(real)
        if group_id is not None:
            return {"type": "group", "group_id": group_id}
        user_id = private_user_id_from_conversation_key(real)
        if user_id is not None:
            return {"type": "private", "user_id": user_id}
        return None
