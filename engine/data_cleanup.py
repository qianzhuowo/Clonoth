#!/usr/bin/env python3
"""data_cleanup.py — Periodic cleanup for Clonoth data directory.

Crontab every 6h. Pure Python, no LLM calls.

1. events.jsonl rotation (>50MB → keep 3 backups)
2. temp files: chat_dump/chunk/compact/messages, temp_summary (>24h)
3. artifacts (>24h)
4. attachments incl. gemini_image (>24h)
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Paths ──────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVENTS_FILE = DATA_DIR / "events.jsonl"
SIGNALS_FILE = DATA_DIR / "signals.jsonl"
LOG_FILE = DATA_DIR / "logs" / "cleanup.log"

# ── Thresholds ─────────────────────────────────
EVENTS_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
EVENTS_BACKUPS = 3

TEMP_MAX_AGE = 24 * 3600              # 24 h
ARTIFACT_MAX_AGE = 24 * 3600          # 24 h
ATTACH_MAX_AGE = 24 * 3600            # 24 h

# QQ/NapCat 内部缓存保守清理阈值。QQ Electron 运行时可能在独立 mount
# namespace 中使用 /app/.config/QQ；systemd timer 在宿主 namespace 运行时
# 需要通过 /proc/<qq-pid>/root/... 访问这些路径。
#
# NTQQ 常见缓存还包括账号目录下的 Image/Video，例如：
#   /app/.config/QQ/<qq号>/Image
#   /app/.config/QQ/<qq号>/Video
# 这些目录里的文件不一定都有标准扩展名，所以清理逻辑除扩展名外，
# 也会按父目录名 image/video/cache/tmp/download 等识别旧缓存文件。
QQ_CACHE_MAX_AGE = float(os.getenv("CLONOTH_QQ_CACHE_MAX_AGE_SECONDS", str(7 * 24 * 3600)))
QQ_CACHE_ROOTS_RAW = os.getenv("CLONOTH_QQ_CACHE_ROOTS", "")
QQ_CACHE_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v",
}
QQ_CACHE_DIR_KEYWORDS = {
    "cache", "cache_data", "code cache", "gpucache", "blob_storage",
    "tmp", "temp", "download", "downloads", "thumb", "thumbnail", "thumbnails",
    # Explicitly cover NTQQ account media cache directories: <qq号>/Image and <qq号>/Video.
    "image", "images", "pic", "pics", "video", "videos", "emoji", "face", "record",
}

# Persistent memory entry cleanup. Entries with constant=true are protected.
# For ordinary entries, use last_hit_at first, then updated_at, then created_at.
# This keeps recently activated memories even if they were created long ago.
MEMORY_ENTRY_MAX_AGE = float(os.getenv("CLONOTH_MEMORY_ENTRY_MAX_AGE_SECONDS", str(14 * 24 * 3600)))

TEMP_GLOBS = [
    "chat_dump_*", "chat_chunk_*", "chat_compact_*", "chat_messages_*",
]

# Child Session 隔离（Phase C）：过期 child session 的 JSONL 文件清理
# child_*.jsonl 超过此时间未修改则删除。与 runtime.yaml 中 child_session.ttl_hours 对齐。
CHILD_SESSION_MAX_AGE = 24 * 3600     # 24 h（与默认 TTL 一致）

# Phase D：node_contexts 目录清理。child session 已替代 snapshot 机制，
# 保留 48h 宽裕期后清理旧文件（比 child session TTL 长一倍，确保兼容期充分）。
NODE_CONTEXTS_MAX_AGE = 48 * 3600     # 48 h


def _log_init():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── 1. events.jsonl rotation ──────────────────
def rotate_events():
    if not EVENTS_FILE.exists():
        return
    sz = EVENTS_FILE.stat().st_size
    if sz < EVENTS_MAX_BYTES:
        logging.info("[events] %dKB < %dMB threshold, skip",
                     sz // 1024, EVENTS_MAX_BYTES // (1024 * 1024))
        return
    logging.info("[events] %dMB — rotating", sz // (1024 * 1024))
    for i in range(EVENTS_BACKUPS, 0, -1):
        p = DATA_DIR / f"events.jsonl.{i}"
        if i == EVENTS_BACKUPS and p.exists():
            p.unlink()
        elif p.exists():
            p.rename(DATA_DIR / f"events.jsonl.{i + 1}")
    EVENTS_FILE.rename(DATA_DIR / "events.jsonl.1")
    logging.info("[events] done")


# ── 1b. signals.jsonl rotation ────────────────
SIGNALS_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
SIGNALS_BACKUPS = 2

def rotate_signals():
    if not SIGNALS_FILE.exists():
        return
    sz = SIGNALS_FILE.stat().st_size
    if sz < SIGNALS_MAX_BYTES:
        logging.info("[signals] %dKB < %dMB threshold, skip",
                     sz // 1024, SIGNALS_MAX_BYTES // (1024 * 1024))
        return
    logging.info("[signals] %dMB — rotating", sz // (1024 * 1024))
    for i in range(SIGNALS_BACKUPS, 0, -1):
        p = DATA_DIR / f"signals.jsonl.{i}"
        if i == SIGNALS_BACKUPS and p.exists():
            p.unlink()
        elif p.exists():
            p.rename(DATA_DIR / f"signals.jsonl.{i + 1}")
    SIGNALS_FILE.rename(DATA_DIR / "signals.jsonl.1")
    logging.info("[signals] done")


# ── generic dir purge ─────────────────────────
def purge_dir(directory: Path, max_age: float, label: str, recursive=False):
    """Delete files older than max_age seconds. Remove empty sub-dirs if recursive."""
    if not directory.exists():
        return
    cutoff = time.time() - max_age
    deleted = 0

    targets = list(directory.rglob("*")) if recursive else list(directory.iterdir())
    for p in targets:
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            pass

    if recursive:
        for dirpath, _, _ in os.walk(str(directory), topdown=False):
            dp = Path(dirpath)
            if dp == directory:
                continue
            try:
                next(dp.iterdir())         # non-empty → skip
            except StopIteration:
                dp.rmdir()                 # empty → remove
            except Exception:
                pass

    logging.info("[%s] deleted %d files", label, deleted)


# ── QQ/NapCat internal cache cleanup ───────────
def _split_configured_roots(raw: str) -> list[Path]:
    roots: list[Path] = []
    for item in str(raw or "").replace("\n", ",").split(","):
        value = item.strip()
        if value:
            roots.append(Path(value))
    return roots


def _read_proc_strings(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except Exception:
        return []
    return [x.decode("utf-8", "replace") for x in data.split(b"\0") if x]


def _append_unique_path(paths: list[Path], path: Path) -> None:
    key = str(path)
    if not key or key == ".":
        return
    if all(str(x) != key for x in paths):
        paths.append(path)


def _discover_qq_cache_roots() -> list[Path]:
    """Return conservative candidate roots for NTQQ/NapCat cache cleanup.

    On the Debian server QQ is launched from /opt/QQ/qq but runs with HOME=/app and
    Chromium child processes use --user-data-dir=/app/.config/QQ.  When /app lives
    only in QQ's mount namespace, the host-side timer can still access it through
    /proc/<pid>/root/app/....
    """
    roots: list[Path] = []
    for p in _split_configured_roots(QQ_CACHE_ROOTS_RAW):
        _append_unique_path(roots, p)

    for p in (Path("/app/.config/QQ"), Path("/app/napcat"), Path("/root/.config/QQ")):
        _append_unique_path(roots, p)

    proc = Path("/proc")
    if not proc.exists():
        return roots

    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        cmdline = _read_proc_strings(pid_dir / "cmdline")
        environ = _read_proc_strings(pid_dir / "environ")
        haystack = "\n".join(cmdline + environ)
        if "/opt/QQ/qq" not in haystack and "--user-data-dir=" not in haystack and "napcat" not in haystack.lower():
            continue

        proc_root = pid_dir / "root"
        for index, arg in enumerate(cmdline):
            user_data_dir = ""
            if arg.startswith("--user-data-dir="):
                user_data_dir = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and index + 1 < len(cmdline):
                user_data_dir = cmdline[index + 1]
            if user_data_dir.startswith("/"):
                _append_unique_path(roots, proc_root / user_data_dir.lstrip("/"))

        home = ""
        for item in environ:
            if item.startswith("HOME="):
                home = item.split("=", 1)[1].strip()
                break
        if home.startswith("/"):
            _append_unique_path(roots, proc_root / home.lstrip("/") / ".config" / "QQ")
            _append_unique_path(roots, proc_root / home.lstrip("/") / "napcat")

    return roots


def _is_safe_qq_cache_root(root: Path) -> bool:
    text = str(root)
    if not text or text in {"/", "/app", "/root", "/proc"}:
        return False
    lowered = text.lower()
    return "qq" in lowered or "napcat" in lowered


def _is_cache_like_relative_path(rel: Path) -> bool:
    for part in rel.parts[:-1]:
        lowered = part.lower()
        compact = lowered.replace(" ", "")
        if any(keyword in lowered or keyword.replace(" ", "") in compact for keyword in QQ_CACHE_DIR_KEYWORDS):
            return True
    return False


def purge_qq_internal_cache():
    """Clean stale NTQQ/NapCat image/video/cache files.

    This deliberately does not wipe whole QQ data directories. It only removes old
    media files or files inside cache-like directories, including NTQQ account
    media caches such as <qq号>/Image and <qq号>/Video. This avoids deleting login
    state, account databases, and other durable QQ configuration.
    """
    if QQ_CACHE_MAX_AGE <= 0:
        logging.info("[qq_cache] disabled")
        return

    cutoff = time.time() - QQ_CACHE_MAX_AGE
    deleted = 0
    deleted_bytes = 0
    scanned_roots = 0
    roots = _discover_qq_cache_roots()

    for root in roots:
        try:
            if not _is_safe_qq_cache_root(root) or not root.exists() or not root.is_dir():
                continue
            scanned_roots += 1
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                    if st.st_mtime >= cutoff:
                        continue
                    rel = p.relative_to(root)
                    is_media = p.suffix.lower() in QQ_CACHE_MEDIA_EXTENSIONS
                    is_cache_file = _is_cache_like_relative_path(rel)
                    if not (is_media or is_cache_file):
                        continue
                    size = st.st_size
                    p.unlink()
                    deleted += 1
                    deleted_bytes += size
                except Exception:
                    continue

            # Remove empty cache/media subdirectories, but never the root itself.
            for dirpath, _, _ in os.walk(str(root), topdown=False):
                dp = Path(dirpath)
                if dp == root:
                    continue
                try:
                    rel = dp.relative_to(root)
                    if not _is_cache_like_relative_path(rel / "placeholder"):
                        continue
                    next(dp.iterdir())
                except StopIteration:
                    try:
                        dp.rmdir()
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception as exc:
            logging.info("[qq_cache] skipped %s: %s", root, exc)

    logging.info(
        "[qq_cache] scanned_roots=%d deleted=%d freed=%dKB max_age_hours=%.1f",
        scanned_roots,
        deleted,
        deleted_bytes // 1024,
        QQ_CACHE_MAX_AGE / 3600,
    )


# ── persistent memory entry cleanup ───────────
def _parse_iso_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _memory_entry_reference_ts(entry: dict[str, object]) -> float | None:
    for key in ("last_hit_at", "updated_at", "created_at"):
        ts = _parse_iso_timestamp(entry.get(key))
        if ts is not None:
            return ts
    return None


def purge_expired_memory_entries():
    """Remove stale non-constant memory entries from data/memory/**/*.yaml.

    Memory books are YAML files shaped like {book, entries}.  Conversation-scoped
    memory namespaces live under data/memory/<namespace>/*.yaml, while older/global
    books live directly under data/memory/*.yaml.  We scan both layouts.
    """
    if MEMORY_ENTRY_MAX_AGE <= 0:
        logging.info("[memory] entry cleanup disabled")
        return

    mem_root = DATA_DIR / "memory"
    if not mem_root.exists() or not mem_root.is_dir():
        return

    cutoff = time.time() - MEMORY_ENTRY_MAX_AGE
    scanned_books = 0
    deleted_entries = 0
    removed_empty_books = 0

    for book_path in sorted(mem_root.rglob("*.yaml")):
        try:
            raw = yaml.safe_load(book_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.info("[memory] skipped unreadable book %s: %s", book_path, exc)
            continue
        if not isinstance(raw, dict):
            continue
        entries = raw.get("entries")
        if not isinstance(entries, list):
            continue

        scanned_books += 1
        kept: list[object] = []
        changed = False
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            if bool(entry.get("constant", False)):
                kept.append(entry)
                continue
            ref_ts = _memory_entry_reference_ts(entry)
            # Entries without timestamps are legacy/manual data; keep them rather
            # than guessing. save_memory now writes created_at for new entries.
            if ref_ts is None or ref_ts >= cutoff:
                kept.append(entry)
                continue
            deleted_entries += 1
            changed = True

        if not changed:
            continue
        raw["entries"] = kept
        try:
            if kept:
                book_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
            else:
                book_path.unlink()
                removed_empty_books += 1
        except Exception as exc:
            logging.info("[memory] failed to update book %s: %s", book_path, exc)

    # Remove empty namespace directories after empty book deletion.
    for dirpath, _, _ in os.walk(str(mem_root), topdown=False):
        dp = Path(dirpath)
        if dp == mem_root:
            continue
        try:
            next(dp.iterdir())
        except StopIteration:
            try:
                dp.rmdir()
            except Exception:
                pass
        except Exception:
            pass

    logging.info(
        "[memory] scanned_books=%d deleted_entries=%d removed_empty_books=%d max_age_hours=%.1f",
        scanned_books,
        deleted_entries,
        removed_empty_books,
        MEMORY_ENTRY_MAX_AGE / 3600,
    )


# ── temp glob purge ───────────────────────────
def purge_temp_globs():
    cutoff = time.time() - TEMP_MAX_AGE
    deleted = 0
    for pat in TEMP_GLOBS:
        for p in DATA_DIR.glob(pat):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except Exception:
                pass
    logging.info("[temp] deleted %d files", deleted)


# ── Child Session JSONL cleanup ───────────────
def purge_expired_child_sessions():
    """清理过期的 child session JSONL 文件。

    Child Session 隔离（Phase C）：扫描 data/conversations/ 下所有 child_*.jsonl，
    按文件修改时间判断是否超过 CHILD_SESSION_MAX_AGE。超期则删除文件。
    映射表的清理由 supervisor 侧在 dispatch 时懒过期处理。
    """
    conv_dir = DATA_DIR / "conversations"
    if not conv_dir.exists():
        return
    cutoff = time.time() - CHILD_SESSION_MAX_AGE
    deleted = 0
    for p in conv_dir.glob("child_*.jsonl"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            pass
    logging.info("[child_sessions] deleted %d expired files", deleted)


# ── main ──────────────────────────────────────
def main():
    _log_init()
    logging.info("=== data cleanup start ===")

    tasks = [
        (rotate_events, {}),
        (rotate_signals, {}),
        # Phase D：node_contexts 已被 child session 替代，启用定期清理
        (purge_dir, dict(directory=DATA_DIR / "node_contexts",
                         max_age=NODE_CONTEXTS_MAX_AGE, label="node_contexts",
                         recursive=True)),
        (purge_expired_child_sessions, {}),
        (purge_temp_globs, {}),
        (purge_dir, dict(directory=DATA_DIR / "temp",
                         max_age=TEMP_MAX_AGE, label="temp", recursive=True)),
        (purge_dir, dict(directory=DATA_DIR / "temp_summary",
                         max_age=TEMP_MAX_AGE, label="temp_summary")),
        (purge_dir, dict(directory=DATA_DIR / "artifacts",
                         max_age=ARTIFACT_MAX_AGE, label="artifacts")),
        (purge_dir, dict(directory=DATA_DIR / "attachments",
                         max_age=ATTACH_MAX_AGE, label="attachments", recursive=True)),
        (purge_qq_internal_cache, {}),
        (purge_expired_memory_entries, {}),
    ]

    for fn, kw in tasks:
        try:
            fn(**kw)
        except Exception as e:
            logging.error("[%s] %s", fn.__name__, e)

    logging.info("=== data cleanup done ===")


if __name__ == "__main__":
    main()
