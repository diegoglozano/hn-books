import argparse
from datetime import UTC, date, datetime

import httpx
import pytest

from app.clients import RemoteClient
from app.config import library_config
from app.discovery import discover_reading_threads
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
