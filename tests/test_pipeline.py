import hashlib
import json
import sys
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.clients import MetadataClient, RemoteClient
from app.db import connect
from app.main import create_app
from app.models import MentionSpan, RunMetrics
from app.pipeline import extract_and_resolve, fetch_thread, refresh_aggregates
from app.ranking import aggregate_tags
from app.resolution import author_matches, resolve_book

THREAD = {
    "id": 100,
    "type": "story",
    "title": "Ask HN: What are you reading?",
    "by": "reader",
    "time": 1700000000,
    "score": 120,
    "descendants": 4,
    "kids": [101, 102],
}
ITEMS = {
    100: THREAD,
    101: {
        "id": 101,
        "type": "comment",
        "parent": 100,
        "by": "alice",
        "time": 1700000001,
        "text": "I highly recommend DDIA for distributed systems and databases.",
        "kids": [103],
    },
    102: {"id": 102, "type": "comment", "parent": 100, "deleted": True, "kids": [104]},
    103: {
        "id": 103,
        "type": "comment",
        "parent": 101,
        "by": "bob",
        "time": 1700000002,
        "text": "Designing Data Intensive Applications is excellent for databases.",
    },
    104: {
        "id": 104,
        "type": "comment",
        "parent": 102,
        "by": "eve",
        "time": 1700000003,
        "text": 'I recommend "A Very Unknown Book".',
    },
}
DOC: dict[str, Any] = {
    "key": "/works/OL123W",
    "title": "Designing Data-Intensive Applications",
    "author_name": ["Martin Kleppmann"],
    "first_publish_year": 2017,
    "cover_i": 123,
    "isbn": ["1449373321", "9781449373320"],
    "subject": ["Databases"],
}
WORK = {
    "title": DOC["title"],
    "description": {"value": "A book about data systems."},
    "subjects": ["Databases"],
}


def make_remote(settings, requests, docs=None, broken=None):
    def handle(request):
        requests.append(str(request.url))
        if "/item/" in request.url.path:
            item_id = int(request.url.path.rsplit("/", 1)[-1].split(".")[0])
            return httpx.Response(200, json=None if item_id == broken else ITEMS[item_id])
        if request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "docs": docs
                    if docs is not None
                    else ([DOC] if "Designing" in request.url.params.get("title", "") else [])
                },
            )
        if request.url.path == "/works/OL123W.json":
            return httpx.Response(200, json=WORK)
        raise AssertionError(f"Unexpected network request: {request.url}")

    return RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))


def test_complete_tree_idempotency_and_offline_reprocessing(settings):
    requests = []
    remote = make_remote(settings, requests)
    with connect(settings.database_path) as conn:
        first = RunMetrics()
        fetch_thread(conn, remote, 100, first)
        assert first.comments_fetched == 4
        assert first.new_comments == 4
        assert conn.execute("SELECT COUNT(*) FROM book_mentions").fetchone()[0] == 0
        assert conn.execute("SELECT raw_json FROM hn_comments WHERE id=102").fetchone()
        assert conn.execute("SELECT parent_id FROM hn_comments WHERE id=104").fetchone()[0] == 102
        extract_and_resolve(conn, MetadataClient(remote, conn, first), first)
        refresh_aggregates(conn)
        assert first.books_resolved == 2
        assert first.unresolved_mentions == 1
        book = conn.execute("SELECT * FROM books").fetchone()
        assert book["independent_recommenders"] == 2
        assert book["mention_count"] == 2
        assert json.loads(book["metadata_json"])["source"] == "openlibrary"
        assert aggregate_tags(conn, book["id"])[0]["mention_count"] == 2
        second = RunMetrics()
        fetch_thread(conn, remote, 100, second)
        extract_and_resolve(conn, MetadataClient(remote, conn, second), second)
        assert second.new_comments == 0
        assert second.metadata_lookups == 0
        before = len(requests)
        extract_and_resolve(
            conn, MetadataClient(remote, conn, second, offline=True), second, force=True
        )
        refresh_aggregates(conn)
        assert len(requests) == before
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM book_mentions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM books_fts").fetchone()[0] == 1


def test_failed_child_does_not_discard_raw_data(settings):
    remote = make_remote(settings, [], broken=103)
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        fetch_thread(conn, remote, 100, metrics)
        assert metrics.failures == 1
        assert conn.execute("SELECT COUNT(*) FROM hn_comments").fetchone()[0] == 3


def test_ambiguous_work_is_unresolved(settings):
    docs = [
        dict(DOC, key="/works/OL1W", title="Dune", author_name=["Frank Herbert"]),
        dict(DOC, key="/works/OL2W", title="Dune", author_name=["Someone Else"]),
    ]
    remote = make_remote(settings, [], docs=docs)
    with connect(settings.database_path) as conn:
        book_id, _, candidates = resolve_book(
            conn,
            MentionSpan(raw="Dune", title="Dune", confidence=0.8),
            MetadataClient(remote, conn, RunMetrics()),
        )
        assert book_id is None
        assert len(candidates) == 2
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_wrong_author_is_unresolved(settings):
    remote = make_remote(settings, [], docs=[DOC])
    with connect(settings.database_path) as conn:
        book_id, _, _ = resolve_book(
            conn,
            MentionSpan(raw="DDIA", title=DOC["title"], author="Someone Else", confidence=0.8),
            MetadataClient(remote, conn, RunMetrics()),
        )
        assert book_id is None


def test_coordinated_and_hyphenated_authors():
    assert author_matches("Ann and Jeff VanderMeer", ["Ann VanderMeer", "Jeff VanderMeer"])
    assert not author_matches("Ann and Jeff VanderMeer", ["Jeff VanderMeer"])
    assert author_matches("Arpaci-Dusseau", ["Remzi Arpaci-Dusseau", "Andrea Arpaci-Dusseau"])
    assert not author_matches("Arpaci-Dusseau", ["Another Dusseau"])
    assert not author_matches("Ann", ["Ann VanderMeer"])


def test_empty_author_search_retries_title_without_weakening_validation(settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={"docs": [] if "author" in request.url.params else [DOC]},
            )
        assert request.url.path == "/works/OL123W.json"
        return httpx.Response(200, json=WORK)

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with connect(settings.database_path) as conn:
        metadata = MetadataClient(remote, conn, RunMetrics())
        span = MentionSpan(raw=DOC["title"], title=DOC["title"], author="Kleppmann", confidence=0.9)
        book_id, _, _ = resolve_book(conn, span, metadata)
        assert book_id is not None
        assert len(requests) == 3
        wrong = span.model_copy(update={"author": "Someone Else"})
        assert resolve_book(conn, wrong, metadata)[0] is None
        # The title-only result and canonical work are permanently cached.
        assert len(requests) == 4


def test_scoped_reprocessing_only_changes_selected_thread(settings, monkeypatch):
    from app.db import store_raw_item
    from app.reprocess import main

    remote = make_remote(settings, [])
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        fetch_thread(conn, remote, 100, metrics)
        extract_and_resolve(conn, MetadataClient(remote, conn, metrics), metrics)
        store_raw_item(conn, dict(THREAD, id=200), 200)
        store_raw_item(conn, dict(ITEMS[101], id=201, parent=200), 200)
        conn.execute("UPDATE hn_comments SET processed_hash='untouched' WHERE id=201")
        conn.commit()
    monkeypatch.setattr(sys, "argv", ["reprocess", "mentions", "--thread", "100", "--offline"])
    assert main() == 0
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT processed_hash FROM hn_comments WHERE id=201").fetchone()[
            0
        ] == ("untouched")
        assert (
            conn.execute("SELECT COUNT(*) FROM book_mentions WHERE thread_id=200").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT mention_count FROM books").fetchone()[0] == 2


def test_refresh_deleted_comment_removes_derived_mentions(settings):
    remote = make_remote(settings, [])
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        fetch_thread(conn, remote, 100, metrics)
        metadata = MetadataClient(remote, conn, metrics)
        extract_and_resolve(conn, metadata, metrics)
        from app.db import store_raw_item

        store_raw_item(conn, dict(ITEMS[101], deleted=True), 100)
        conn.commit()
        extract_and_resolve(conn, metadata, metrics)
        refresh_aggregates(conn)
        assert conn.execute("SELECT mention_count FROM books").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM book_mentions WHERE comment_id=101").fetchone()[0]
            == 0
        )


def test_reprocessing_removes_stale_common_word_matches_from_scores_and_search(settings):
    from app.db import store_raw_item

    false_comment = dict(
        ITEMS[101], text="It was about distributed systems. Highly recommended.", kids=[]
    )
    true_comment = dict(ITEMS[101], id=102, text="It by Stephen King. Highly recommended.", kids=[])
    doc = dict(DOC, title="It", author_name=["Stephen King"])

    def handle(request):
        if request.url.path == "/search.json":
            return httpx.Response(200, json={"docs": [doc]})
        return httpx.Response(200, json={"title": "It", "description": "A horror novel."})

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with connect(settings.database_path) as conn:
        store_raw_item(conn, THREAD, 100)
        store_raw_item(conn, false_comment, 100)
        store_raw_item(conn, true_comment, 100)
        conn.commit()
        metrics = RunMetrics()
        metadata = MetadataClient(remote, conn, metrics)
        extract_and_resolve(conn, metadata, metrics)
        book_id = conn.execute("SELECT id FROM books").fetchone()[0]
        # Simulate a resolved false mention persisted by the old processor.
        conn.execute(
            """INSERT INTO book_mentions(book_id,comment_id,thread_id,raw_mention,
            normalized_mention,context_text,extraction_confidence,resolution_confidence,
            recommendation_strength,sentiment,status,candidates_json,created_at)
            SELECT book_id,101,thread_id,'It','it',?,0.95,1,
            recommendation_strength,sentiment,status,candidates_json,created_at
            FROM book_mentions WHERE comment_id=102""",
            (false_comment["text"],),
        )
        raw = conn.execute("SELECT raw_json FROM hn_comments WHERE id=101").fetchone()[0]
        old_hash = hashlib.sha256(("3" + raw).encode()).hexdigest()
        conn.execute("UPDATE hn_comments SET processed_hash=? WHERE id=101", (old_hash,))
        refresh_aggregates(conn)
        assert conn.execute("SELECT mention_count FROM books").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM books_fts WHERE books_fts MATCH 'distributed'"
            ).fetchone()[0]
            == 1
        )
        extract_and_resolve(conn, MetadataClient(remote, conn, RunMetrics(), offline=True), metrics)
        refresh_aggregates(conn)
        assert (
            conn.execute("SELECT COUNT(*) FROM book_mentions WHERE comment_id=101").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT mention_count,recommendation_count FROM books").fetchone()[
            :
        ] == (
            1,
            1,
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM books_fts WHERE books_fts MATCH 'distributed'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM hn_comments").fetchone()[0] == 2
        # When no real mention remains, preserve metadata but clear library/search aggregates.
        store_raw_item(conn, dict(true_comment, text="I highly recommend it."), 100)
        conn.commit()
        extract_and_resolve(conn, MetadataClient(remote, conn, metrics, offline=True), metrics)
        refresh_aggregates(conn)
        assert conn.execute("SELECT mention_count,all_time_score FROM books").fetchone()[:] == (
            0,
            0,
        )
        assert conn.execute("SELECT COUNT(*) FROM books_fts").fetchone()[0] == 0
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/books").json()["total"] == 0
        assert client.get(f"/api/books/{book_id}/mentions").json()["total"] == 0
        assert client.get(f"/api/books/{book_id}").json()["canonical_title"] == "It"


def test_api_evidence_search_and_admin_protection(settings):
    remote = make_remote(settings, [])
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        fetch_thread(conn, remote, 100, metrics)
        extract_and_resolve(conn, MetadataClient(remote, conn, metrics), metrics)
        refresh_aggregates(conn)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/api/search", params={"q": "books about databases"})
        assert response.json()["total"] == 1
        book = response.json()["items"][0]
        detail = client.get(f"/api/books/{book['id']}").json()
        assert detail["timeline"][0]["month"] == "2023-11"
        mentions = client.get(f"/api/books/{book['id']}/mentions").json()
        assert mentions["items"][0]["hn_url"].endswith("id=101")
        assert mentions["items"][0]["author"] == "alice"
        assert client.get("/api/books?tag=databases").json()["total"] == 1
        assert client.get("/api/books?tag=history").json()["total"] == 0
        assert client.get("/api/search", params={"q": '" OR * NEAR(()'}).status_code == 200
        assert client.get("/api/books?page=0").status_code == 422
        assert client.get("/api/books/999").status_code == 404
        assert client.get("/api/admin/unresolved-mentions").status_code == 404
        assert client.get("/api/threads/100").json()["total"] == 4
    settings.admin_token = "test-token"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/admin/unresolved-mentions").status_code == 401
        response = client.get(
            "/api/admin/unresolved-mentions", headers={"Authorization": "Bearer test-token"}
        )
        assert response.json()["total"] == 1


def test_lowercase_quoted_phrase_is_preserved_unresolved(settings):
    remote = make_remote(settings, [], docs=[dict(DOC, title="Currently Reading")])
    from app.extraction import extract_mentions

    span = extract_mentions('It is on my "currently reading" shelf.')[0]
    assert span.confidence < 0.7
    with connect(settings.database_path) as conn:
        metrics = RunMetrics()
        book_id, _, _ = resolve_book(conn, span, MetadataClient(remote, conn, metrics))
        assert book_id is None
        assert metrics.metadata_lookups == 0


def test_discovery_pagination_deduplication_and_whitelist(settings):
    from app.config import library_config
    from app.pipeline import discover_threads

    pages = []
    settings.hn_thread_ids = "100, 200"

    def handle(request):
        assert request.url.params["tags"] == "ask_hn"
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "nbPages": 2,
                "hits": [
                    {"objectID": "100"},
                    {"objectID": str(300 + page)},
                ],
            },
        )

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    assert discover_threads(remote, backfill=True) == [100, 200, 300, 301]
    assert len(pages) == 2 * len(library_config()["discovery_queries"])
    pages.clear()
    assert discover_threads(remote) == [100, 200, 300]
    assert set(pages) == {0}
