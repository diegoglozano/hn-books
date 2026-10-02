import json
from pathlib import Path

import httpx
import pytest

from app.clients import MetadataClient, RemoteClient
from app.db import connect
from app.models import MentionSpan, RunMetrics
from app.resolution import RESOLUTION_VERSION, resolve_book, select_candidate, unique_authors

SNAPSHOTS = json.loads((Path(__file__).parent / "fixtures/metadata_duplicates.json").read_text())


@pytest.mark.parametrize("snapshot", SNAPSHOTS, ids=lambda item: item["title"])
def test_current_catalog_duplicates_select_the_original_book(snapshot):
    span = MentionSpan(
        raw=snapshot["title"],
        title=snapshot["title"],
        author=snapshot["author"],
        confidence=0.99,
        require_author=True,
    )
    selected, confidence, evidence = select_candidate(span, snapshot["docs"])
    assert selected is not None
    assert selected["key"] == snapshot["expected_work"]
    assert confidence == 1
    assert len(evidence) == len(snapshot["docs"])
    # Catalog ordering cannot change the choice among duplicate work IDs.
    assert select_candidate(span, list(reversed(snapshot["docs"])))[0] == selected


def doc(key="/works/OL1W", title="Dune", authors=None, **extra):
    return {"key": key, "title": title, "author_name": authors or ["Frank Herbert"], **extra}


@pytest.mark.parametrize(
    "author,docs",
    [
        (None, [doc(), doc(key="/works/OL2W")]),
        ("Herbert", [doc(), doc(key="/works/OL2W", authors=["Brian Herbert"])]),
        (
            "Frank Herbert",
            [
                doc(authors=["Frank Herbert", "Person One"]),
                doc(key="/works/OL2W", authors=["Frank Herbert", "Person Two"]),
            ],
        ),
        ("Frank Herbert", [doc(), doc(key="/works/OL2W", subject=["Comics & graphic novels"])]),
        (
            "Frank Herbert",
            [doc(title="Dune: Part One"), doc(key="/works/OL2W", title="Dune: Part Two")],
        ),
    ],
)
def test_different_authors_collaborators_formats_and_parts_remain_ambiguous(author, docs):
    span = MentionSpan(raw="Dune", title="Dune", author=author, confidence=0.99)
    assert select_candidate(span, docs)[0] is None


def test_literal_title_wins_over_colon_title_without_weakening_subtitle_matching():
    span = MentionSpan(raw="Dune", title="Dune", author="Frank Herbert", confidence=0.99)
    original = doc()
    adaptation = doc(key="/works/OL2W", title="Dune: A Graphic Novel", isbn=["a", "b"])
    selected = select_candidate(span, [adaptation, original])[0]
    assert selected is not None
    assert selected["key"] == original["key"]
    # A genuine metadata subtitle is still accepted when no full-title match exists.
    assert select_candidate(span, [doc(title="Dune: Science Fiction Novel")])[0] is not None
    assert select_candidate(span, [doc(title="Children of Dune")])[0] is None


def test_duplicate_author_names_cannot_change_identity_or_appear_twice():
    assert unique_authors(["Neil Postman", " Neil Postman ", "Postman, Neil", "NEIL POSTMAN"]) == [
        "Neil Postman"
    ]
    assert unique_authors(["Ann VanderMeer", "Jeff VanderMeer"]) == [
        "Ann VanderMeer",
        "Jeff VanderMeer",
    ]
    span = MentionSpan(raw="Dune", title="Dune", author="Frank Herbert", confidence=0.99)
    selected, _, _ = select_candidate(
        span, [doc(authors=["Frank Herbert", "Herbert, Frank"]), doc(key="/works/OL2W")]
    )
    assert selected is not None
    assert selected["authors"] == ["Frank Herbert"]


def test_stale_canonical_record_is_reverified_and_updated_in_place(settings):
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/search.json":
            return httpx.Response(200, json={"docs": [doc()]})
        if request.url.path == "/works/OL1W.json":
            return httpx.Response(
                200,
                json={
                    "title": "Dune",
                    "authors": [
                        {"author": {"key": "/authors/OL1A"}},
                        {"author": {"key": "/authors/OL2A"}},
                    ],
                },
            )
        return httpx.Response(200, json={"name": "Frank Herbert"})

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    span = MentionSpan(
        raw="Dune", title="Dune", author="Frank Herbert", confidence=0.99, require_author=True
    )
    with connect(settings.database_path) as conn:
        conn.execute("""INSERT INTO books(canonical_title,normalized_title,authors,
            openlibrary_id,created_at,updated_at)
            VALUES ('Dune','dune','["Frank Herbert", "Frank Herbert"]',
            '/works/OL1W','original','old')""")
        existing_id = conn.execute("SELECT id FROM books").fetchone()[0]
        result = resolve_book(conn, span, MetadataClient(remote, conn, RunMetrics()))
        assert result[0] == existing_id
        book = conn.execute("SELECT * FROM books").fetchone()
        assert json.loads(book["authors"]) == ["Frank Herbert"]
        assert json.loads(book["metadata_json"])["resolution_version"] == RESOLUTION_VERSION
        assert book["created_at"] == "original"
        assert len(requests) == 4
        assert (
            resolve_book(conn, span, MetadataClient(remote, conn, RunMetrics(), offline=True))[0]
            == existing_id
        )
        assert len(requests) == 4
    remote.close()


@pytest.mark.parametrize(
    "local_title,local_authors",
    [
        ("Dune: A Graphic Novel", ["Frank Herbert"]),
        ("Dune", ["Frank Herbert", "Another Contributor"]),
    ],
)
def test_local_adaptation_cannot_hide_an_exact_title_and_author_catalog_match(
    settings, local_title, local_authors
):
    def handle(request):
        if request.url.path == "/search.json":
            return httpx.Response(200, json={"docs": [doc()]})
        assert request.url.path == "/works/OL1W.json"
        return httpx.Response(200, json={"title": "Dune"})

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    span = MentionSpan(raw="Dune", title="Dune", author="Frank Herbert", confidence=0.99)
    with connect(settings.database_path) as conn:
        conn.execute(
            """INSERT INTO books(canonical_title,normalized_title,authors,
            openlibrary_id,metadata_json,created_at,updated_at)
            VALUES (?,?,?,'/works/OL2W',?,'old','old')""",
            (
                local_title,
                local_title.lower(),
                json.dumps(local_authors),
                json.dumps({"resolution_version": RESOLUTION_VERSION}),
            ),
        )
        book_id, _, _ = resolve_book(conn, span, MetadataClient(remote, conn, RunMetrics()))
        assert book_id is not None
        assert (
            conn.execute("SELECT openlibrary_id FROM books WHERE id=?", (book_id,)).fetchone()[0]
            == "/works/OL1W"
        )
    remote.close()
