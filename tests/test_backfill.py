import httpx

from app.backfill import completed_threads, ingest_backfill
from app.clients import RemoteClient
from app.db import connect
from app.models import RunMetrics
from app.pipeline import PROCESSOR_VERSION


def remote_for(settings, requests, missing_child=False, metadata_failure=False):
    def handle(request):
        path = request.url.path
        requests.append(path)
        if path.endswith("/item/100.json"):
            return httpx.Response(
                200,
                json={
                    "id": 100,
                    "type": "story",
                    "time": 100,
                    "title": "Ask HN: Reading",
                    "kids": [101],
                },
            )
        if path.endswith("/item/101.json"):
            return httpx.Response(
                200,
                json=None
                if missing_child
                else {
                    "id": 101,
                    "parent": 100,
                    "type": "comment",
                    "by": "reader",
                    "time": 101,
                    "text": 'I highly recommend "An Unknown Book".',
                },
            )
        if path == "/search.json":
            if metadata_failure:
                return httpx.Response(403)
            return httpx.Response(200, json={"docs": []})
        raise AssertionError(path)

    return RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))


def test_backfill_checkpoint_skips_completed_threads(settings):
    requests = []
    remote = remote_for(settings, requests)
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        ingest_backfill(conn, remote, [100], metrics)
        assert completed_threads(conn) == {100}
        assert metrics.threads_fetched == 1
        assert metrics.unresolved_mentions == 1
        assert len(requests) == 3
        second = RunMetrics()
        ingest_backfill(conn, remote, [100], second)
        assert len(requests) == 3
        assert second.threads_skipped == 1
        assert conn.execute("SELECT COUNT(*) FROM book_mentions").fetchone()[0] == 1
        ingest_backfill(conn, remote, [100], second, refresh=True)
        assert second.threads_fetched == 1


def test_partial_tree_is_retried_before_marking_complete(settings):
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        ingest_backfill(conn, remote_for(settings, [], missing_child=True), [100], metrics)
        assert metrics.failures == 1
        assert completed_threads(conn) == set()
        ingest_backfill(conn, remote_for(settings, []), [100], RunMetrics())
        assert completed_threads(conn) == {100}
        assert conn.execute("SELECT COUNT(*) FROM hn_comments").fetchone()[0] == 1


def test_metadata_failure_resumes_from_raw_without_refetching_hn(settings):
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        ingest_backfill(conn, remote_for(settings, [], metadata_failure=True), [100], metrics)
        assert metrics.failures == 1
        assert completed_threads(conn) == set()
        requests = []
        second = RunMetrics()
        ingest_backfill(conn, remote_for(settings, requests), [100], second)
        assert second.threads_reused_raw == 1
        assert requests == ["/search.json"]
        assert completed_threads(conn) == {100}


def test_limit_counts_pending_threads_and_continues_after_failure(settings, monkeypatch):
    from app import backfill

    with connect(settings.database_path) as conn:
        ingest_backfill(conn, remote_for(settings, []), [100], RunMetrics())
        attempted = []

        def fail_fetch(conn, remote, thread_id, metrics):
            attempted.append(thread_id)
            raise ValueError("missing thread")

        monkeypatch.setattr(backfill, "fetch_thread", fail_fetch)
        metrics = RunMetrics()
        ingest_backfill(conn, remote_for(settings, []), [100, 200, 300, 400], metrics, limit=2)
        assert attempted == [200, 300]
        assert metrics.threads_skipped == 1
        assert metrics.failures == 2


def test_old_processor_checkpoint_reuses_raw_and_reprocesses(settings):
    with connect(settings.database_path) as conn:
        ingest_backfill(conn, remote_for(settings, []), [100], RunMetrics())
        conn.execute("UPDATE thread_checkpoints SET processed_version='old'")
        conn.commit()
        requests = []
        metrics = RunMetrics()
        ingest_backfill(conn, remote_for(settings, requests), [100], metrics)
        assert metrics.threads_reused_raw == 1
        assert not requests
        assert (
            conn.execute("SELECT processed_version FROM thread_checkpoints").fetchone()[0]
            == PROCESSOR_VERSION
        )


def test_existing_database_upgrade_preserves_raw_records(settings):
    from app.db import initialize, store_raw_item

    with connect(settings.database_path) as conn:
        store_raw_item(
            conn, {"id": 100, "type": "story", "title": "Stored reading thread", "time": 100}, 100
        )
        conn.execute("DROP TABLE thread_checkpoints")
        conn.execute("PRAGMA user_version=1")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        assert (
            conn.execute("SELECT title FROM hn_threads WHERE id=100").fetchone()[0]
            == "Stored reading thread"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert completed_threads(conn) == set()
