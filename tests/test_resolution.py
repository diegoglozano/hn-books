import json
from pathlib import Path

import httpx
import pytest

from app.clients import MetadataClient, RemoteClient
from app.db import connect
from app.identity import identity_digest, identity_reviews
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


REVIEW_CASES = json.loads(
    (Path(__file__).parent / "fixtures/reviewed_identity_cases.json").read_text()
)


@pytest.mark.parametrize("case", REVIEW_CASES, ids=lambda case: case["mention"]["title"])
def test_source_linked_identity_reviews(case):
    selected, _, evidence = select_candidate(MentionSpan(**case["mention"]), case["docs"])
    assert (selected["key"] if selected else None) == case["expected_work"]
    if case["expected_work"] is None:
        assert evidence[0]["excluded_by_review"]
        assert evidence[0]["identity_review"]["sources"]


def test_reviewed_aliases_are_specific_and_coauthors_still_require_each_identity():
    from app.resolution import author_matches, author_parts

    assert author_matches("Katherine Addison", ["Sarah Monette"])
    assert author_matches("Fyodor Dostoevsky", ["Фёдор Михайлович Достоевский"])
    assert author_matches("Haruki Murakami", ["村上春樹"])
    assert author_matches("Cixin Liu", ["刘慈欣"])
    assert not author_matches("Haruki Murakami", ["Ryū Murakami"])
    assert not author_matches("Fyodor Dostoevsky", ["Andreĭ Mikhaĭlovich Dostoevskiĭ"])
    assert author_parts("Mark F. Bear, Barry W. Connors, and Michael A. Paradiso") == [
        "Mark F. Bear",
        "Barry W. Connors",
        "Michael A. Paradiso",
    ]
    assert author_parts("Herbert, Frank") == ["Herbert, Frank"]
    assert author_matches(
        "Mark F. Bear, Barry W. Connors, and Michael A. Paradiso",
        ["Mark F. Bear", "Barry W. Connors", "Michael A. Paradiso"],
    )
    assert not author_matches(
        "Mark F. Bear, Barry W. Connors, and Michael A. Paradiso",
        ["Mark F. Bear", "Barry W. Connors"],
    )


@pytest.mark.parametrize(
    "author,work_link,expected",
    [
        ("James S. A. Corey", None, False),
        ("James S. A. Corey", "/works/OL20185071W", True),
        ("Hallie Lambert", None, True),
        ("James S. A. Corey and Hallie Lambert", None, True),
    ],
)
def test_expanse_review_keeps_explicit_comic_identities_usable(author, work_link, expected):
    graphic = doc(
        key="/works/OL20185071W",
        title="The Expanse",
        authors=["James S. A. Corey", "Hallie Lambert", "Georgia Lee"],
        subject=["Comics & graphic novels"],
    )
    span = MentionSpan(
        raw="The Expanse", title="The Expanse", author=author, work_id=work_link, confidence=0.99
    )
    assert (select_candidate(span, [graphic])[0] is not None) == expected


def test_a_reviewed_short_title_does_not_identify_other_series_volumes():
    span = MentionSpan(
        raw="The Dark Forest", title="The Dark Forest", author="Cixin Liu", confidence=0.99
    )
    assert (
        select_candidate(
            span, [doc(key="/works/OL1W", title="The Three-Body Problem", authors=["Cixin Liu"])]
        )[0]
        is None
    )
    assert (
        select_candidate(span, [doc(key="/works/OL15029001W", title="1Q84", authors=["村上春樹"])])[
            0
        ]
        is None
    )
    one_q = MentionSpan(raw="1Q84", title="1Q84", author="Haruki Murakami", confidence=0.99)
    assert (
        select_candidate(
            one_q, [doc(key="/works/OL17534240W", title="1Q84 [3/3]", authors=["村上春樹"])]
        )[0]
        is None
    )


def test_reviewed_subtitle_lookup_and_contributors_survive_cached_reprocessing(settings):
    span = MentionSpan(
        raw="Thinking In Systems: A Primer",
        title="Thinking in Systems: A Primer",
        author="Donella Meadows",
        confidence=0.99,
        require_author=True,
    )
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "docs": []
                    if request.url.params["title"] == span.title
                    else [
                        doc(
                            key="/works/OL3737036W",
                            title="Thinking in systems",
                            authors=["Donella H. Meadows", "Diana Wright"],
                            isbn=["9781603580557"],
                        )
                    ]
                },
            )
        if request.url.path == "/works/OL3737036W.json":
            return httpx.Response(
                200,
                json={
                    "title": "Thinking in systems",
                    "authors": [
                        {"author": {"key": "/authors/OL1A"}},
                        {"author": {"key": "/authors/OL2A"}},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "name": "Donella H. Meadows"
                if request.url.path.endswith("OL1A.json")
                else "Diana Wright"
            },
        )

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with connect(settings.database_path) as conn:
        metadata = MetadataClient(remote, conn, RunMetrics())
        book_id, _, _ = resolve_book(conn, span, metadata)
        assert book_id is not None
        book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        assert json.loads(book["authors"]) == ["Donella H. Meadows"]
        assert json.loads(book["isbn_13"]) == ["9781603580557"]
        saved = json.loads(book["metadata_json"])
        assert saved["identity_review"]["sources"]
        assert saved["work"]["authors"][1]["author"]["key"] == "/authors/OL2A"
        assert saved["search"]["author_name"] == ["Donella H. Meadows", "Diana Wright"]
        assert saved["identity_review_digest"] == identity_digest()
        previous_calls = len(calls)
        assert (
            resolve_book(conn, span, MetadataClient(remote, conn, RunMetrics(), offline=True))[0]
            == book_id
        )
        assert len(calls) == previous_calls
    remote.close()


def test_reviewed_work_checks_provider_title_and_author_before_publication(settings):
    span = MentionSpan(
        raw="The Goldfinch", title="The Goldfinch", author="Donna Tartt", confidence=0.99
    )

    def handle(request):
        if request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        doc(
                            key="/works/OL16809803W", title="The Goldfinch", authors=["Donna Tartt"]
                        )
                    ]
                },
            )
        return httpx.Response(200, json={"title": "A different work"})

    remote = RemoteClient(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with connect(settings.database_path) as conn:
        assert resolve_book(conn, span, MetadataClient(remote, conn, RunMetrics()))[0] is None
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    remote.close()


def test_identity_review_data_has_unique_keys_provenance_and_cache_version():
    reviews = identity_reviews()
    assert len({r["work_id"] for r in reviews["works"]}) == len(reviews["works"])
    for group in ("authors", "works", "implicit_exclusions"):
        assert all(
            r["sources"] and all(s.startswith("https://") for s in r["sources"])
            for r in reviews[group]
        )
    from app.pipeline import PROCESSOR_VERSION

    assert identity_digest()[:12] in PROCESSOR_VERSION
