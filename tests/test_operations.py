import json

import pytest

from app.db import connect
from app.operations import tracked_run, writer_lock


def test_lock_prevents_overlapping_writers(settings):
    with (
        writer_lock(settings),
        pytest.raises(RuntimeError, match="already running"),
        writer_lock(settings),
    ):
        pass
    with writer_lock(settings):
        pass


def test_run_persists_failure_and_metrics(settings):
    with pytest.raises(ValueError, match="broken"), tracked_run(settings, "test") as metrics:
        metrics.comments_fetched = 3
        raise ValueError("broken")
    with connect(settings.database_path) as conn:
        row = conn.execute("SELECT * FROM ingestion_runs").fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "broken"
        assert json.loads(row["metrics_json"])["comments_fetched"] == 3
        assert row["finished_at"] is not None
