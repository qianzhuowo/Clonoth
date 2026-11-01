import os
import time
from pathlib import Path

import engine.data_cleanup as dc

OLD = time.time() - 30 * 24 * 3600


def _touch(path: Path, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _age_dir(path: Path, mtime: float = OLD) -> None:
    os.utime(path, (mtime, mtime))


def test_qq_cache_removes_stale_media_but_keeps_state_and_napcat_temp(tmp_path, monkeypatch) -> None:
    root = tmp_path / "qq_root"
    stale_img = _touch(root / "acct" / "Image" / "a.dat")
    fresh_img = _touch(root / "acct" / "Image" / "b.dat", mtime=time.time())
    login_db = _touch(root / "acct" / "nt_db" / "login.db")
    napcat_temp = root / "NapCat" / "temp"
    napcat_temp.mkdir(parents=True)
    _age_dir(napcat_temp)
    stale_empty = root / "acct" / "Video" / "old"
    stale_empty.mkdir(parents=True)
    _age_dir(stale_empty)

    monkeypatch.setattr(dc, "_discover_qq_cache_roots", lambda: [root])
    dc.purge_qq_internal_cache()

    assert not stale_img.exists()
    assert fresh_img.exists()
    assert login_db.exists()
    assert napcat_temp.is_dir()
    assert not stale_empty.exists()


def test_qq_cache_refuses_unsafe_roots() -> None:
    for unsafe in ("/", "/app", "/root", "/proc", "/srv/data"):
        assert dc._is_safe_qq_cache_root(Path(unsafe)) is False
    assert dc._is_safe_qq_cache_root(Path("/app/.config/QQ")) is True


def test_qq_cache_disabled(tmp_path, monkeypatch) -> None:
    root = tmp_path / "qq_root"
    stale_img = _touch(root / "Image" / "a.jpg")
    monkeypatch.setattr(dc, "_discover_qq_cache_roots", lambda: [root])
    monkeypatch.setattr(dc, "QQ_CACHE_MAX_AGE", 0)
    dc.purge_qq_internal_cache()
    assert stale_img.exists()


def test_onebot_cache_only_touches_onebot_prefix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dc, "DATA_DIR", tmp_path)
    stale = _touch(tmp_path / "cache" / "onebot_reply_attachments.json")
    fresh = _touch(tmp_path / "cache" / "onebot_new.json", mtime=time.time())
    other = _touch(tmp_path / "cache" / "embeddings.bin")
    dc.purge_onebot_cache()
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_stocktool_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dc, "DATA_DIR", tmp_path)
    stale = _touch(tmp_path / "stocktool" / "cache" / "q.json")
    keep = _touch(tmp_path / "stocktool" / "watchlist.json")
    dc.purge_stocktool_cache()
    assert not stale.exists()
    assert keep.exists()
