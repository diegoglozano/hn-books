from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.db import initialize


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    path = tmp_path / "library.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    # The historical fixtures exercise the old baseline without paid API calls.
    monkeypatch.setenv("EXTRACTION_BACKEND", "heuristic")
    get_settings.cache_clear()
    value = Settings(database_path=path, metadata_interval=0, frontend_path=tmp_path / "no-ui")
    initialize(path)
    yield value
    get_settings.cache_clear()
