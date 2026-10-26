import os
import time
from pathlib import Path

import engine.data_cleanup as dc


def _age(path: Path, seconds: float) -> None:
    ts = time.time() - seconds
    os.utime(path, (ts, ts))


def _run(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(dc, "QQ_CACHE_MAX_AGE", 3600.0)
    monkeypatch.setattr(dc, "_discover_qq_cache_roots", lambda: [root])
    dc.purge_qq_internal_cache()


def test_napcat_temp_survives_even_when_empty_and_stale(tmp_path, monkeypatch):
    root = tmp_path / "QQ"
    temp = root / "NapCat" / "temp"
    temp.mkdir(parents=True)
    old_file = temp / "143d6a94"
    old_file.write_bytes(b"x")
    _age(old_file, 7200)
    _age(temp, 7200)

    _run(monkeypatch, root)

    assert not old_file.exists()  # stale files inside temp are still cleaned
    assert temp.is_dir()          # but the working directory itself is kept


def test_empty_cache_dir_removed_only_when_stale(tmp_path, monkeypatch):
    root = tmp_path / "QQ"
    stale = root / "acct" / "Image" / "old"
    fresh = root / "acct" / "Image" / "new"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    _age(stale, 7200)

    _run(monkeypatch, root)

    assert not stale.exists()
    assert fresh.is_dir()


def test_non_cache_dirs_untouched(tmp_path, monkeypatch):
    root = tmp_path / "QQ"
    login = root / "nt_qq" / "global"
    login.mkdir(parents=True)
    _age(login, 7200)

    _run(monkeypatch, root)

    assert login.is_dir()


def test_is_protected_qq_dir_matching():
    assert dc._is_protected_qq_dir(Path("NapCat/temp"))
    assert dc._is_protected_qq_dir(Path("NapCat/Temp/sub"))
    assert not dc._is_protected_qq_dir(Path("acct/temp"))
    assert not dc._is_protected_qq_dir(Path("NapCat"))
