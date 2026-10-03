from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.db import connect, store_raw_item
from app.main import create_app
from app.ranking import ensure_current_rankings, independence, score_book


def mention(author="alice", thread=1, created=1700000000, strength=0.95, sentiment=0.9):
    return {
        "author": author,
        "thread_id": thread,
        "created_at": created,
        "recommendation_strength": strength,
        "sentiment": sentiment,
        "context_text": "A great explanation of a difficult topic.",
        "thread_score": 100,
    }


def test_independence_and_repeat_resistance():
    repeated = [mention() for _ in range(20)]
    independent = [mention(author=f"user{i}", thread=i + 1) for i in range(20)]
    assert independence(repeated) == {"users": 1, "threads": 1, "dates": 1}
    assert score_book(repeated)["all_time_score"] == score_book([mention()])["all_time_score"]
    assert score_book(independent)["all_time_score"] > score_book(repeated)["all_time_score"] * 20
    assert score_book(repeated)["recommendation_count"] == 20


def test_strength_negative_neutral_and_time():
    assert score_book([mention(strength=0, sentiment=-0.8)])["all_time_score"] == 0
    assert score_book([mention(strength=0.2, sentiment=0)])["all_time_score"] == 0
    assert (
        score_book([mention()])["all_time_score"]
        > score_book([mention(strength=0.65)])["all_time_score"]
    )
    now = 1700000000
    fresh = score_book([mention(created=now)], now=now)
    old = score_book([mention(created=now - 365 * 86400)], now=now)
    assert fresh["all_time_score"] == old["all_time_score"]
    assert abs(old["recent_score"] / fresh["recent_score"] - 0.5) < 0.001


def test_unknown_authors_do_not_create_independent_users():
    assert independence([mention(author=None)])["users"] == 0
    assert score_book([])["all_time_score"] == 0


def test_prolific_reader_cannot_create_votes_across_threads_or_dates():
    repeated = [mention(thread=i + 1, created=1700000000 + i * 86400) for i in range(30)]
    latest = repeated[-1]
    scored = score_book(repeated, now=latest["created_at"])
    single = score_book([latest], now=latest["created_at"])
    assert scored["all_time_score"] == single["all_time_score"]
    assert scored["recent_score"] == single["recent_score"]
    assert scored["independent_recommenders"] == 1
    assert scored["details"]["positive_threads"] == scored["details"]["positive_dates"] == 1
    assert scored["recommendation_count"] == 30  # Historical mentions remain available.
    assert (
        score_book([mention(author="alice", strength=0.65), mention(author="bob", strength=0.65)])[
            "all_time_score"
        ]
        > scored["all_time_score"]
    )


def test_criticism_reduces_score_once_per_reader_and_cannot_produce_negative_scores():
    positive = mention()
    critical = mention(author="bob", strength=0, sentiment=-0.8)
    ranked = score_book([positive, critical])
    assert 0 < ranked["all_time_score"] < score_book([positive])["all_time_score"]
    assert ranked["details"]["negative_users"] == 1
    assert ranked["all_time_score"] == score_book([positive, *[critical] * 20])["all_time_score"]
    disputed = [positive, critical, critical | {"author": "eve"}]
    assert score_book(disputed)["all_time_score"] == 0
    assert score_book(disputed)["recent_score"] == 0
    assert score_book(disputed)["recommendation_count"] == 1


def test_latest_opinion_replaces_old_opinion_but_neutral_reading_does_not():
    original = mention()
    critic = mention(created=original["created_at"] + 86400, strength=0, sentiment=-0.8)
    neutral = mention(created=critic["created_at"] + 86400, strength=0.2, sentiment=0)
    changed = score_book([original, critic, neutral])
    assert changed["all_time_score"] == changed["independent_recommenders"] == 0
    assert changed["details"]["negative_users"] == 1
    assert changed["recommendation_count"] == 1
    recommended_again = mention(created=neutral["created_at"] + 86400)
    assert score_book([original, critic, neutral, recommended_again])["all_time_score"] > 0
    assert (
        score_book([original, neutral])["all_time_score"]
        == score_book([original])["all_time_score"]
    )
    weaker = mention(created=neutral["created_at"] + 86400, strength=0.65)
    assert (
        score_book([original, weaker])["all_time_score"] == score_book([weaker])["all_time_score"]
    )


def test_conflicting_simultaneous_opinions_abstain_and_input_order_cannot_break_ties():
    positive = mention()
    critical = mention(strength=0, sentiment=-0.8)
    scored = score_book([positive, critical], now=1700000000)
    assert scored["all_time_score"] == scored["independent_recommenders"] == 0
    assert scored["details"]["conflicting_readers"] == 1
    examples = [positive, positive | {"thread_id": 2}, critical, mention(author="bob")]
    assert score_book(examples, now=1700000000) == score_book(
        list(reversed(examples)), now=1700000000
    )


def test_anonymous_votes_are_bounded_and_do_not_collide_with_real_username():
    anonymous = [mention(author=None, thread=i, created=1700000000 + i * 86400) for i in range(20)]
    scored = score_book(anonymous, now=anonymous[-1]["created_at"])
    assert scored["all_time_score"] == score_book([anonymous[-1]])["all_time_score"]
    assert scored["all_time_score"] < score_book([mention()])["all_time_score"]
    assert scored["independent_recommenders"] == 0
    mixed = score_book([mention(author=None), mention(author="unknown")])
    assert mixed["independent_recommenders"] == 1
    assert mixed["details"]["independent_contexts"] == 2


def test_irrelevant_text_and_thread_popularity_cannot_inflate_book_score():
    short = mention()
    long = deepcopy(short)
    long.update(context_text="Unrelated words and many other books. " * 1000, thread_score=1000000)
    assert score_book([short], now=1700000000) == score_book([long], now=1700000000)


def test_diversity_is_bounded_and_old_criticism_decays_in_recent_ranking():
    many = [
        mention(author=f"reader{i}", thread=i, created=1700000000 + i * 86400) for i in range(100)
    ]
    assert 1 < score_book(many)["details"]["diversity_multiplier"] <= 1.25
    now = 1700000000
    positive = mention(created=now)
    old_critic = mention(author="bob", created=now - 365 * 86400, strength=0, sentiment=-0.8)
    scored = score_book([positive, old_critic], now=now)
    assert scored["recent_score"] > scored["all_time_score"]


def test_web_startup_upgrades_scores_atomically_and_preserves_evidence(settings):
    with connect(settings.database_path) as conn:
        store_raw_item(conn, {"id": 100, "title": "Reading"}, 100)
        for comment_id, author in ((101, "alice"), (102, "bob")):
            store_raw_item(
                conn, {"id": comment_id, "parent": 100, "by": author, "time": 1700000000}, 100
            )
        conn.execute("""INSERT INTO books(id,canonical_title,normalized_title,authors,created_at,
            updated_at,all_time_score,score_details) VALUES (1,'Example','example','[]','today',
            'today',100,'{"formula_version":1}')""")
        for comment_id, sentiment, strength in ((101, 0.9, 0.95), (102, -0.8, 0)):
            conn.execute(
                """INSERT INTO book_mentions(book_id,comment_id,thread_id,raw_mention,
                normalized_mention,context_text,extraction_confidence,recommendation_strength,
                sentiment,status,created_at)
                VALUES (1,?,100,'Example','example','Evidence',1,?,?,'resolved','today')""",
                (comment_id, strength, sentiment),
            )
        conn.execute("""INSERT INTO extraction_cache VALUES ('cached','test','{}','{}','today')""")
        conn.execute("""INSERT INTO books_fts(rowid,title) VALUES (1,'Example')""")
        raw = [tuple(row) for row in conn.execute("SELECT * FROM hn_comments")]
        evidence = [tuple(row) for row in conn.execute("SELECT * FROM book_mentions")]
        cache = [tuple(row) for row in conn.execute("SELECT * FROM extraction_cache")]
    with TestClient(create_app(settings)) as client:
        book = client.get("/api/books/1").json()
        assert book["score_details"]["formula_version"] == 2
        assert book["score_details"]["negative_users"] == 1
        assert book["all_time_score"] == 0.15
        assert book["recommendation_count"] == book["independent_recommenders"] == 1
    with connect(settings.database_path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM hn_comments")] == raw
        assert [tuple(row) for row in conn.execute("SELECT * FROM book_mentions")] == evidence
        assert [tuple(row) for row in conn.execute("SELECT * FROM extraction_cache")] == cache
        assert conn.execute("SELECT title FROM books_fts").fetchone()[0] == "Example"
        before = tuple(conn.execute("SELECT * FROM books").fetchone())
        assert not ensure_current_rankings(conn)
        assert tuple(conn.execute("SELECT * FROM books").fetchone()) == before


def test_failed_score_upgrade_rolls_back_all_books(settings, monkeypatch):
    with connect(settings.database_path) as conn:
        for title in ("First", "Second"):
            conn.execute(
                """INSERT INTO books(canonical_title,normalized_title,created_at,updated_at,
                all_time_score,score_details)
                VALUES (?,?,'today','today',100,'{"formula_version":1}')""",
                (title, title.lower()),
            )
        before = [tuple(row) for row in conn.execute("SELECT * FROM books ORDER BY id")]
    calls = 0

    def fail_second(mentions, now=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Interrupted upgrade")
        return score_book(mentions, now=now)

    monkeypatch.setattr("app.ranking.score_book", fail_second)
    with (
        pytest.raises(RuntimeError, match="Interrupted upgrade"),
        connect(settings.database_path) as conn,
    ):
        ensure_current_rankings(conn)
    assert calls == 2
    with connect(settings.database_path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM books ORDER BY id")] == before


def test_library_sorts_recommendations_by_readers_and_breaks_ties_by_title(settings):
    with connect(settings.database_path) as conn:
        store_raw_item(conn, {"id": 100, "title": "Reading"}, 100)
        for book_id, title in ((1, "Repeated"), (2, "Independent"), (3, "Zebra"), (4, "Alpha")):
            conn.execute(
                """INSERT INTO books(id,canonical_title,normalized_title,created_at,updated_at)
                VALUES (?,?,?,'today','today')""",
                (book_id, title, title.lower()),
            )
        examples = [(1, "alice", 0.95, 0.9)] * 20 + [
            (2, "bob", 0.65, 0.6),
            (2, "eve", 0.65, 0.6),
            (3, "alice", 0.2, 0),
            (4, "alice", 0.2, 0),
        ]
        for cid, (bid, author, strength, sentiment) in enumerate(examples, 101):
            store_raw_item(conn, {"id": cid, "parent": 100, "by": author, "time": 1700000000}, 100)
            conn.execute(
                """INSERT INTO book_mentions(book_id,comment_id,thread_id,raw_mention,
                normalized_mention,context_text,extraction_confidence,recommendation_strength,
                sentiment,status,created_at)
                VALUES (?,?,100,'Title','title','Evidence',1,?,?,'resolved','today')""",
                (bid, cid, strength, sentiment),
            )
    with TestClient(create_app(settings)) as client:
        for sort in ("all-time", "recent", "recommendations"):
            result = client.get("/api/books", params={"sort": sort}).json()
            assert [b["canonical_title"] for b in result["items"]] == [
                "Independent",
                "Repeated",
                "Alpha",
                "Zebra",
            ]
            assert result["items"][0]["independent_recommenders"] == 2
            assert result["items"][1]["recommendation_count"] == 20
        assert (
            client.get("/api/books", params={"sort": "mentions"}).json()["items"][0][
                "canonical_title"
            ]
            == "Repeated"
        )
