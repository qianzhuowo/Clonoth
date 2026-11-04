"""Migrate legacy Clonoth (OLD) QQ sessions and memory into this deployment.

Formats are compatible: both use data/sessions.json keyed by session_id,
data/conversations/<session_id>.jsonl with the same message fields,
data/memory/**/*.yaml memory books ({book, entries}) and the conv_<digest>
memory namespace, and the same onebot_anon_map.json (version 1).

What is migrated
- QQ sessions (channel qq_group / qq_private) that have a conversation file.
  Raw-id keys (qq_private:<number>) are converted to the stable digest key.
  The entry node is switched to the QQ entry node of this deployment.
- data/memory/** memory books (global and per-conversation namespaces).
- data/onebot_anon_map.json (alias table: old history refers to these aliases).
- config/qq_user_profiles.yaml (display-name profiles).

Agent/test/system sessions, events, signals, transcripts and caches are not
migrated. Default is a dry run; pass --apply to write. Every file that is
overwritten or removed is copied to <new-data>/_migrate_backup_<timestamp>/.
Stop the new supervisor and adapter before --apply (they rewrite sessions.json).

Usage:
  python scripts/migrate_legacy_data.py --old-root /path/old --new-root /path/new
  python scripts/migrate_legacy_data.py --old-root ... --new-root ... --on-conflict replace --apply
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

QQ_CHANNELS = {"qq_group", "qq_private"}
_RAW_KEY = re.compile(r"^(qq_group|qq_private):(\d+)$")
_DIGEST_KEY = re.compile(r"^(qq_group|qq_private):[0-9a-f]{24}$")


def stable_key(conversation_key: str, secret: str = "") -> str:
    """Same digest as adapters.onebot.anon.AnonMap.conversation_digest."""
    m = _RAW_KEY.match(conversation_key)
    if not m:
        return conversation_key
    raw = conversation_key.encode("utf-8")
    if secret:
        digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:24]
    else:
        digest = hashlib.sha256(raw).hexdigest()[:24]
    return f"{m.group(1)}:{digest}"


def _short(value: str) -> str:
    return (value or "")[:8]


class Migrator:
    def __init__(self, old_root: Path, new_root: Path, *, apply: bool, on_conflict: str,
                 entry_node: str, secret: str) -> None:
        self.old_data = old_root / "data"
        self.new_data = new_root / "data"
        self.old_config = old_root / "config"
        self.new_config = new_root / "config"
        self.apply = apply
        self.on_conflict = on_conflict
        self.entry_node = entry_node
        self.secret = secret
        self.backup_dir = self.new_data / f"_migrate_backup_{datetime.now():%Y%m%d-%H%M%S}"
        self.report: dict[str, int] = {}
        self.notes: list[str] = []

    # ---- helpers ---------------------------------------------------------
    def _count(self, key: str, n: int = 1) -> None:
        self.report[key] = self.report.get(key, 0) + n

    def _backup(self, path: Path) -> None:
        if not self.apply or not path.exists():
            return
        rel = path.relative_to(self.new_data.parent)
        dest = self.backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(path, dest)

    def _copy(self, src: Path, dest: Path) -> None:
        if not self.apply:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, data: Any) -> None:
        if not self.apply:
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ---- sessions --------------------------------------------------------
    def migrate_sessions(self) -> None:
        old_sessions = self._load_json(self.old_data / "sessions.json", {})
        new_path = self.new_data / "sessions.json"
        new_sessions = self._load_json(new_path, {})
        by_key = {str(v.get("conversation_key") or ""): sid for sid, v in new_sessions.items() if isinstance(v, dict)}
        changed = False
        for sid, entry in old_sessions.items():
            if not isinstance(entry, dict) or str(entry.get("channel") or "") not in QQ_CHANNELS:
                self._count("sessions_skipped_non_qq")
                continue
            old_key = str(entry.get("conversation_key") or "")
            key = stable_key(old_key, self.secret)
            if not _DIGEST_KEY.match(key):
                self._count("sessions_skipped_unknown_key")
                continue
            src = self.old_data / "conversations" / f"{sid}.jsonl"
            if not src.is_file():
                self._count("sessions_skipped_no_history")
                continue
            existing_sid = by_key.get(key)
            if existing_sid == sid:
                self._count("sessions_already_present")
                continue
            if existing_sid:
                if self.on_conflict != "replace":
                    self._count("sessions_conflict_skipped")
                    self.notes.append(f"conflict kept new session {_short(existing_sid)} for {key[:20]}...")
                    continue
                self._backup(self.new_data / "conversations" / f"{existing_sid}.jsonl")
                if self.apply:
                    (self.new_data / "conversations" / f"{existing_sid}.jsonl").unlink(missing_ok=True)
                new_sessions.pop(existing_sid, None)
                self._count("sessions_conflict_replaced")
            if sid in new_sessions:
                self._backup(self.new_data / "conversations" / f"{sid}.jsonl")
            migrated = dict(entry)
            migrated["conversation_key"] = key
            migrated["entry_node_id"] = self.entry_node
            migrated.setdefault("workspace", {})
            migrated["reset"] = False
            new_sessions[sid] = migrated
            by_key[key] = sid
            self._copy(src, self.new_data / "conversations" / f"{sid}.jsonl")
            self._count("sessions_migrated")
            self._count("history_bytes", src.stat().st_size)
            if key != old_key:
                self._count("sessions_key_converted")
            changed = True
        if changed:
            self._backup(new_path)
            self._write_json(new_path, new_sessions)

    # ---- memory ----------------------------------------------------------
    def migrate_memory(self) -> None:
        old_root = self.old_data / "memory"
        if not old_root.is_dir():
            return
        for src in sorted(old_root.rglob("*.yaml")):
            dest = self.new_data / "memory" / src.relative_to(old_root)
            if dest.exists():
                if src.read_bytes() == dest.read_bytes():
                    self._count("memory_identical")
                    continue
                if self.on_conflict != "replace":
                    self._count("memory_conflict_skipped")
                    self.notes.append(f"memory conflict kept new {dest.relative_to(self.new_data)}")
                    continue
                self._backup(dest)
                self._count("memory_conflict_replaced")
            self._copy(src, dest)
            self._count("memory_books_migrated")

    # ---- alias map and profiles -------------------------------------------
    def migrate_file(self, src: Path, dest: Path, label: str) -> None:
        if not src.is_file():
            return
        if dest.exists() and src.read_bytes() == dest.read_bytes():
            self._count(f"{label}_identical")
            return
        # Old history refers to the old aliases/profiles, so the old file wins.
        self._backup(dest)
        self._copy(src, dest)
        self._count(f"{label}_migrated")

    def run(self) -> dict[str, Any]:
        self.migrate_sessions()
        self.migrate_memory()
        self.migrate_file(self.old_data / "onebot_anon_map.json", self.new_data / "onebot_anon_map.json", "anon_map")
        self.migrate_file(self.old_config / "qq_user_profiles.yaml", self.new_config / "qq_user_profiles.yaml", "profiles")
        return {
            "mode": "apply" if self.apply else "dry-run",
            "backup_dir": str(self.backup_dir) if self.apply else "",
            "counts": dict(sorted(self.report.items())),
            "notes": self.notes,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-root", required=True, type=Path, help="legacy Clonoth install root")
    parser.add_argument("--new-root", required=True, type=Path, help="this deployment's root")
    parser.add_argument("--on-conflict", choices=("skip", "replace"), default="skip",
                        help="when the same conversation/memory book already exists in the new deployment")
    parser.add_argument("--entry-node", default="qq.orchestrator")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)
    for root in (args.old_root, args.new_root):
        if not (root / "data").is_dir():
            parser.error(f"{root} has no data/ directory")
    secret = os.environ.get("ONEBOT_CONVERSATION_HASH_SECRET", "")
    result = Migrator(args.old_root, args.new_root, apply=args.apply, on_conflict=args.on_conflict,
                      entry_node=args.entry_node, secret=secret).run()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
