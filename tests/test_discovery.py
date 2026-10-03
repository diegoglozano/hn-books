import argparse
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from app.clients import RemoteClient
from app.config import library_config
from app.db import connect
from app.discovery import ThreadCandidate, discover_reading_threads
from app.ingest import backfill_period


def hit(item_id: int, timestamp: int, title: str = "Ask HN: What are you reading?") -> dict:
    return {"objectID": str(item_id), "created_at_i": timestamp, "title": title, "num_comments": 5}


def result(
    hits: list[dict], total: int | None = None, pages: int = 1, page_size: int = 100
) -> dict:
    return {
        "hits": hits,
        "nbHits": len(hits) if total is None else total,
        "nbPages": pages,
        "hitsPerPage": page_size,
        "exhaustiveNbHits": True,
    }


def test_date_bounds_title_scope_deduplication_and_pagination(settings, monkeypatch):
    monkeypatch.setitem(library_config(), "backfill_queries", ["reading", "books"])
    settings.hn_thread_ids = "999"
    calls = []

    def handle(request):
        params = request.url.params
        assert params["tags"] == "ask_hn"
        assert params["restrictSearchableAttributes"] == "title"
        assert params["numericFilters"] == "created_at_i>=100,created_at_i<200"
        calls.append((params["query"], int(params["page"])))
        # Defensive filtering enforces both boundaries even if the upstream returns a stray hit.
        hits = [hit(2, 150), hit(99, 200)] if params["page"] == "0" else [hit(1, 100)]
        return httpx.Response(200, json=result(hits, total=3, pages=2, page_size=2))

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    candidates = discover_reading_threads(remote, 100, 200)
    assert [candidate.id for candidate in candidates] == [1, 2]
    assert 999 not in [candidate.id for candidate in candidates]
    assert len(calls) == 4


def test_capped_window_is_split_without_losing_boundary_items(settings, monkeypatch):
    monkeypatch.setitem(library_config(), "backfill_queries", ["reading"])
    calls = []

    def handle(request):
        window = request.url.params["numericFilters"]
        calls.append(window)
        if window == "created_at_i>=100,created_at_i<200":
            data = result([hit(2, 150)], total=200, pages=1)
        elif window == "created_at_i>=100,created_at_i<150":
            data = result([hit(1, 149)])
        else:
            assert window == "created_at_i>=150,created_at_i<200"
            data = result([hit(2, 150), hit(3, 199)])
        return httpx.Response(200, json=data)

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    assert [c.id for c in discover_reading_threads(remote, 100, 200)] == [1, 2, 3]
    assert len(calls) == 3


def test_all_stories_scope_and_empty_response(settings, monkeypatch):
    monkeypatch.setitem(library_config(), "backfill_queries", ["reading"])

    def handle(request):
        assert request.url.params["tags"] == "story"
        return httpx.Response(200, json=result([], pages=0))

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    assert discover_reading_threads(remote, 100, 200, scope="stories") == []


@pytest.mark.parametrize("response", [result([], total=1), result([], total=200, pages=1)])
def test_incomplete_or_unsplittable_results_fail_explicitly(settings, monkeypatch, response):
    monkeypatch.setitem(library_config(), "backfill_queries", ["reading"])
    remote = RemoteClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=response),
            )
        ),
    )
    with pytest.raises(RuntimeError):
        discover_reading_threads(remote, 100, 101)


@pytest.mark.parametrize(
    "years,since,until,expected_start,expected_end",
    [
        (None, None, None, "2021-09-30", "2026-09-30"),
        (3, None, None, "2023-09-30", "2026-09-30"),
        (1, None, date(2024, 2, 29), "2023-02-28", "2024-02-29"),
        (None, date(2020, 1, 1), date(2025, 1, 1), "2020-01-01", "2025-01-01"),
    ],
)
def test_backfill_period(years, since, until, expected_start, expected_end):
    args = argparse.Namespace(years=years, since=since, until=until)
    start, end = backfill_period(args, now=datetime(2026, 9, 30, tzinfo=UTC))
    assert datetime.fromtimestamp(start, UTC).date().isoformat() == expected_start
    assert datetime.fromtimestamp(end, UTC).date().isoformat() == expected_end


def test_backfill_period_rejects_inverted_dates():
    args = argparse.Namespace(years=None, since=date(2026, 10, 1), until=date(2026, 9, 1))
    with pytest.raises(ValueError, match="earlier"):
        backfill_period(args)


def test_dry_run_does_not_create_database(settings, monkeypatch, capsys):
    from app import ingest

    settings.database_path.unlink()
    monkeypatch.setattr("sys.argv", ["ingest", "backfill", "--years", "3", "--dry-run"])
    monkeypatch.setattr(ingest, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest, "discover_reading_threads", lambda *args, **kwargs: [])
    assert ingest.main() == 0
    assert '"threads_discovered": 0' in capsys.readouterr().out
    assert not settings.database_path.exists()


def test_non_exhaustive_count_flag_alone_does_not_trigger_unbounded_splitting(
    settings, monkeypatch
):
    monkeypatch.setitem(library_config(), "backfill_queries", ["reading"])
    data = result([hit(1, 100)])
    data["exhaustiveNbHits"] = False
    remote = RemoteClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=data),
            )
        ),
    )
    assert [c.id for c in discover_reading_threads(remote, 100, 101)] == [1]


def test_title_filter_excludes_prefix_only_matches(settings, monkeypatch):
    monkeypatch.setitem(library_config(), "backfill_queries", ["book"])
    data = result(
        [
            hit(1, 100, "Ask HN: Bookmark manager?"),
            hit(2, 101, "Ask HN: Books to read?"),
            hit(3, 102, "Ask HN: README templates?"),
        ]
    )
    remote = RemoteClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=data),
            )
        ),
    )
    assert [c.id for c in discover_reading_threads(remote, 100, 200)] == [2]


def test_engaged_reading_collection_filters_titles_and_comment_boundary(settings, monkeypatch):
    monkeypatch.setitem(library_config(), "reading_thread_queries", ["reading"])
    titles = [
        "Ask HN: What are you reading?",
        "Ask HN: Which book are you reading these days?",
        "Ask HN: What books are you currently reading?",
        "Ask HN: What are good blogs that you enjoy reading?",
        "Ask HN: Are you tired of reading ChatGPT headlines?",
        "Ask HN: Reading workflow?",
    ]
    hits = [hit(i + 1, 110 + i, title) | {"num_comments": 101} for i, title in enumerate(titles)]
    hits += [hit(7, 120) | {"num_comments": 100}, hit(8, 130)]
    hits += [hit(9, 140) | {"num_comments": None}, hit(10, 200) | {"num_comments": 500}]

    def handle(request):
        assert request.url.params["numericFilters"] == (
            "created_at_i>=100,created_at_i<200,num_comments>=101"
        )
        assert request.url.params["restrictSearchableAttributes"] == "title"
        return httpx.Response(200, json=result(hits))

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    candidates = discover_reading_threads(remote, 100, 200, collection="reading", min_comments=101)
    assert [c.id for c in candidates] == [1, 2, 3]


def test_comment_filter_survives_capped_window_splitting(settings, monkeypatch):
    monkeypatch.setitem(library_config(), "reading_thread_queries", ["reading"])

    def handle(request):
        numeric = request.url.params["numericFilters"]
        assert numeric.endswith(",num_comments>=101")
        if numeric.startswith("created_at_i>=100,created_at_i<200"):
            data = result([], total=200, pages=1)
        elif numeric.startswith("created_at_i>=100,created_at_i<150"):
            data = result([hit(1, 149) | {"num_comments": 101}])
        else:
            assert numeric.startswith("created_at_i>=150,created_at_i<200")
            data = result([hit(2, 150) | {"num_comments": 102}])
        return httpx.Response(200, json=data)

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    assert [
        c.id
        for c in discover_reading_threads(remote, 100, 200, collection="reading", min_comments=101)
    ] == [1, 2]


def test_negative_comment_threshold_is_rejected(settings):
    remote = RemoteClient(settings)
    try:
        with pytest.raises(ValueError, match="cannot be negative"):
            discover_reading_threads(remote, 100, 200, min_comments=-1)
    finally:
        remote.close()


@pytest.mark.parametrize(
    "command,flags,collection,minimum,order,expected_ids",
    [
        ("reading", [], "reading", 101, "newest", [3, 2, 1]),
        ("backfill", [], "discussions", 0, "oldest", [1, 2, 3]),
        (
            "reading",
            ["--min-comments", "200", "--order", "oldest"],
            "reading",
            200,
            "oldest",
            [1, 2, 3],
        ),
    ],
)
def test_dry_run_reports_selected_order_and_skips_completed_threads(
    settings, monkeypatch, capsys, command, flags, collection, minimum, order, expected_ids
):
    from app import ingest

    calls = []
    candidates = [
        ThreadCandidate(i, "What are you reading?", 100 + i, 100 + i) for i in range(1, 5)
    ]

    def discover(remote, start, end, **kwargs):
        calls.append(kwargs)
        return candidates

    monkeypatch.setattr(ingest, "discover_reading_threads", discover)
    monkeypatch.setattr(ingest, "completed_threads", lambda conn: {4})
    monkeypatch.setattr(ingest, "get_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["ingest", command, "--years", "5", "--dry-run", *flags])
    assert ingest.main() == 0
    body = json.loads(capsys.readouterr().out)
    assert calls == [{"scope": "ask-hn", "collection": collection, "min_comments": minimum}]
    assert body["min_comments"] == minimum and body["order"] == order
    assert body["threads_pending"] == body["threads_to_process"] == 3
    assert body["threads_skipped"] == 1
    assert [c["id"] for c in body["preview"]] == expected_ids
    assert body["estimated_comments"] == 306
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0


def test_reading_dry_run_limit_reports_only_the_next_batch(settings, monkeypatch, capsys):
    from app import ingest

    monkeypatch.setattr(ingest, "get_settings", lambda: settings)
    monkeypatch.setattr(
        ingest,
        "discover_reading_threads",
        lambda *a, **kw: [
            ThreadCandidate(1, "What are you reading?", 100, 101),
            ThreadCandidate(2, "What are you reading?", 200, 150),
        ],
    )
    monkeypatch.setattr("sys.argv", ["ingest", "reading", "--limit", "1", "--dry-run"])
    assert ingest.main() == 0
    body = json.loads(capsys.readouterr().out)
    assert body["threads_pending"] == 2 and body["threads_to_process"] == 1
    assert body["estimated_comments"] == 150
    assert [c["id"] for c in body["preview"]] == [2]
