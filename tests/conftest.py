import pytest


@pytest.fixture(autouse=True)
def _isolated_compact_breaker(tmp_path, monkeypatch):
    """Keep the compact circuit-breaker state file out of the repo's data/ dir."""
    import engine.compact as compact

    monkeypatch.setattr(compact, "_BREAKER_PATH", tmp_path / "compact_breaker.json")
