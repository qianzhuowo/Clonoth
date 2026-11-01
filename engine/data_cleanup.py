#!/usr/bin/env python3
"""data_cleanup.py — Periodic cleanup for Clonoth data directory.

Crontab every 6h. Pure Python, no LLM calls.

1. events.jsonl rotation (>50MB → keep 3 backups)
2. temp files: chat_dump/chunk/compact/messages, temp_summary (>24h)
3. artifacts (>24h)
4. attachments (>24h)
5. QQ/NapCat internal media/cache files (>7d, conservative; NapCat/temp kept)
6. OneBot adapter caches data/cache/onebot_* (>24h)
7. stocktool quote cache data/stocktool/cache (>24h)
"""

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

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

# QQ/NapCat internal cache. QQ may run in its own mount namespace (HOME=/app);
# the host-side timer then reaches it through /proc/<qq-pid>/root/... .
# Only media files or files under cache-like directories are removed, never
# login state or account databases. CLONOTH_QQ_CACHE_ROOTS adds roots
# (comma/newline separated); CLONOTH_QQ_CACHE_MAX_AGE_SECONDS<=0 disables.
QQ_CACHE_MAX_AGE = float(os.getenv("CLONOTH_QQ_CACHE_MAX_AGE_SECONDS", str(7 * 24 * 3600)))
QQ_CACHE_ROOTS_RAW = os.getenv("CLONOTH_QQ_CACHE_ROOTS", "")
QQ_CACHE_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v",
}
QQ_CACHE_DIR_KEYWORDS = {
    "cache", "cache_data", "code cache", "gpucache", "blob_storage",
    "tmp", "temp", "download", "downloads", "thumb", "thumbnail", "thumbnails",
    # NTQQ account media caches: <qq>/Image and <qq>/Video.
    "image", "images", "pic", "pics", "video", "videos", "emoji", "face", "record",
}
# Runtime working directories that must never be removed even when empty.
# NapCat writes every outgoing image/emoji to NapCat/temp/<uuid> and empties it
# afterwards; removing it makes image sends fail with ENOENT and, through
# /proc/<pid>/root, can detach a bind mount in the container namespace.
QQ_CACHE_PROTECTED_DIR_PARTS = (
    ("napcat", "temp"),
)

# OneBot adapter caches in data/cache/ (e.g. onebot_reply_attachments.json, an
# index of already-stored attachments). Same age as attachments.
ONEBOT_CACHE_MAX_AGE = float(os.getenv("CLONOTH_ONEBOT_CACHE_MAX_AGE_SECONDS", str(24 * 3600)))
ONEBOT_CACHE_GLOB = "onebot_*"

# stocktool quote/parse cache; stale entries are re-fetched.
STOCKTOOL_CACHE_MAX_AGE = float(os.getenv("CLONOTH_STOCKTOOL_CACHE_MAX_AGE_SECONDS", str(24 * 3600)))

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


# ── Tool-result artifact cleanup ──────────────
def purge_expired_tool_result_artifacts():
    """Delete stale task-scoped tool result artifact directories."""
    # [AutoC 2026-06-08] Why: supervisor normally deletes tool_results/{task_id}
    # when a task reaches a terminal state, but abnormal exits can skip that path.
    # How: scan the task directories and remove any directory whose newest file is
    # older than ARTIFACT_MAX_AGE. Purpose: raw large tool outputs cannot remain on
    # disk indefinitely after crashes or forced process stops.
    root = DATA_DIR / "artifacts" / "tool_results"
    if not root.exists():
        return
    cutoff = time.time() - ARTIFACT_MAX_AGE
    deleted = 0
    for task_dir in list(root.iterdir()):
        if not task_dir.is_dir():
            continue
        try:
            newest = max((p.stat().st_mtime for p in task_dir.rglob("*") if p.is_file()), default=task_dir.stat().st_mtime)
            if newest < cutoff:
                import shutil
                shutil.rmtree(task_dir)
                deleted += 1
        except Exception:
            pass
    logging.info("[tool_results] deleted %d stale task dirs", deleted)


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


def prune_stats_db():
    """Prune timestamped local statistics older than 30 days, then reclaim pages.

    Only timestamp columns are eligible. Tables without one are left intact.
    SQLite handles either Unix seconds or ISO timestamps through its date parser.
    """
    db_path = DATA_DIR / "stats.db"
    if not db_path.is_file():
        return
    cutoff = time.time() - 30 * 24 * 3600
    with sqlite3.connect(db_path, timeout=30) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({quoted_table})")}
            timestamp = next((name for name in ("timestamp", "created_at", "ts") if name in columns), None)
            if timestamp is None:
                continue
            col = '"' + timestamp + '"'
            conn.execute(
                f"DELETE FROM {quoted_table} WHERE "
                f"CASE WHEN typeof({col}) IN ('integer', 'real') THEN {col} "
                f"ELSE unixepoch({col}) END < ?",
                (cutoff,),
            )
        conn.commit()
        conn.execute("VACUUM")
    logging.info("[stats_db] pruned old statistics and vacuumed %s", db_path)


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


def _is_protected_qq_dir(rel: Path) -> bool:
    """Return True if *rel* is (or is inside) a protected runtime directory."""
    parts = [part.lower() for part in rel.parts]
    for protected in QQ_CACHE_PROTECTED_DIR_PARTS:
        width = len(protected)
        for i in range(len(parts) - width + 1):
            if tuple(parts[i:i + width]) == protected:
                return True
    return False


def _remove_stale_empty_qq_dir(dp: Path, root: Path, cutoff: float) -> bool:
    """Remove an empty cache-like directory only when it is safe and stale.

    Empty directories are kept unless they have not been modified since
    *cutoff*: QQ/NapCat create many working directories once and expect them to
    keep existing even while empty.
    """
    if dp == root:
        return False
    try:
        rel = dp.relative_to(root)
        if not _is_cache_like_relative_path(rel / "placeholder"):
            return False
        if _is_protected_qq_dir(rel):
            return False
        if os.path.ismount(dp):
            return False
        if dp.stat().st_mtime >= cutoff:
            return False
        if any(dp.iterdir()):
            return False
        dp.rmdir()
        return True
    except Exception:
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

            # Remove stale empty cache/media subdirectories, never the root itself
            # and never protected runtime directories such as NapCat/temp.
            for dirpath, _, _ in os.walk(str(root), topdown=False):
                _remove_stale_empty_qq_dir(Path(dirpath), root, cutoff)
        except Exception as exc:
            logging.info("[qq_cache] skipped %s: %s", root, exc)

    logging.info(
        "[qq_cache] scanned_roots=%d deleted=%d freed=%dKB max_age_hours=%.1f",
        scanned_roots,
        deleted,
        deleted_bytes // 1024,
        QQ_CACHE_MAX_AGE / 3600,
    )


# ── OneBot / stocktool caches ─────────────────
def purge_onebot_cache():
    """Delete stale data/cache/onebot_* files; other data/cache users are untouched."""
    if ONEBOT_CACHE_MAX_AGE <= 0:
        logging.info("[onebot_cache] disabled")
        return
    directory = DATA_DIR / "cache"
    if not directory.is_dir():
        return
    cutoff = time.time() - ONEBOT_CACHE_MAX_AGE
    deleted = 0
    for p in directory.glob(ONEBOT_CACHE_GLOB):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            pass
    logging.info("[onebot_cache] deleted %d files", deleted)


def purge_stocktool_cache():
    if STOCKTOOL_CACHE_MAX_AGE <= 0:
        logging.info("[stocktool_cache] disabled")
        return
    purge_dir(DATA_DIR / "stocktool" / "cache", STOCKTOOL_CACHE_MAX_AGE,
              "stocktool_cache", recursive=True)


# ── main ──────────────────────────────────────
def main():
    _log_init()
    logging.info("=== data cleanup start ===")

    tasks = [
        (rotate_events, {}),
        (rotate_signals, {}),
        (prune_stats_db, {}),
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
        (purge_expired_tool_result_artifacts, {}),
        (purge_dir, dict(directory=DATA_DIR / "artifacts",
                         max_age=ARTIFACT_MAX_AGE, label="artifacts")),
        (purge_dir, dict(directory=DATA_DIR / "attachments",
                         max_age=ATTACH_MAX_AGE, label="attachments", recursive=True)),
        (purge_onebot_cache, {}),
        (purge_qq_internal_cache, {}),
        (purge_stocktool_cache, {}),
    ]

    for fn, kw in tasks:
        try:
            fn(**kw)
        except Exception as e:
            logging.error("[%s] %s", fn.__name__, e)

    logging.info("=== data cleanup done ===")


if __name__ == "__main__":
    main()
