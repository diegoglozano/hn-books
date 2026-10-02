import json
import logging
from threading import Event

import pytest

from app.db import connect
from app.models import RunMetrics
from app.operations import progress_phase, tracked_run, writer_lock


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


def test_heartbeat_reports_while_main_thread_waits_and_stops_after_phase(monkeypatch):
    monkeypatch.setattr("app.operations.PROGRESS_INTERVAL", 0.01)
    heartbeat = Event()
    records = []

    def report(event, *, extra):
        records.append((event, extra))
        if event == "processing_heartbeat":
            heartbeat.set()

    monkeypatch.setattr(logging.getLogger("app.operations"), "info", report)
    metrics = RunMetrics(comments_total=20, comments_processed=3)
    with progress_phase("luna_extraction_and_verification", metrics):
        assert heartbeat.wait(timeout=2)
    assert records[0][0] == "processing_phase_started"
    assert records[-1][0] == "processing_phase_finished"
    assert all(record[1]["metrics"]["comments_processed"] == 3 for record in records)
    assert all(
        record[1]["detail"]["phase"] == "luna_extraction_and_verification" for record in records
    )


def test_failed_phase_is_reported_without_swallowing_error(caplog):
    with (
        caplog.at_level("INFO"),
        pytest.raises(ValueError, match="failed"),
        progress_phase("preparing_staging"),
    ):
        raise ValueError("failed")
    assert [r.message for r in caplog.records] == [
        "processing_phase_started",
        "processing_phase_failed",
    ]
