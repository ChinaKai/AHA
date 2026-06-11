from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import AHA_RUNTIME_ENV_KEYS


@pytest.fixture(autouse=True)
def isolate_test_aha_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests from writing runs into the developer's real AHA home."""
    for key in AHA_RUNTIME_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
