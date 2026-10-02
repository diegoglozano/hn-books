import json
import sys
from pathlib import Path

import pytest

from app import comment_reviews
from app.comment_reviews import CommentReview, CommentReviews, check_review, source_digest
from app.db import connect, store_raw_item
from app.luna import LunaResult, extraction_key
from app.pipeline import processor_version
from app.rebuild import rebuild_thread
from app.review import thread_report
from tests.test_rebuild import mocks, seed_old_library


def make_review(comment, result):
    return CommentReview(
        thread_id=100,
        comment_id=comment["comment_id"],
        source_digest=source_digest(100, comment["comment_id"], comment["input"]),
        reviewer="Fixture reviewer",
        reviewed_at="2026-10-02",
        reason="Source-checked correction of a model extraction mistake.",
        sources=[comment["hn_url"]],
        case_types=["regression"],
        result=LunaResult.model_validate(result),
    )


def dune(sentiment="positive"):
    return {
        "mentions": [
            {
                "raw": "Dune",
                "title": "Dune",
                "author": "Frank Herbert",
                "author_source": "inferred",
                "work_id": None,
                "confidence": 1,
                "sentiment": sentiment,
                "tags": ["fiction"],
            }
        ]
    }


def install_review(monkeypatch, comment, result):
    decisions = CommentReviews(schema_version=1, comments=[make_review(comment, result)])
    monkeypatch.setattr(comment_reviews, "reviewed_comments", lambda: decisions)
    return decisions


def test_missed_mention_survives_fresh_staging_without_changing_model_cache(settings, monkeypatch):
    seed_old_library(settings)
    requests, _ = mocks(settings, monkeypatch, empty_mentions=True)
    rebuild_thread(settings, 100)
    snapshot = thread_report(settings.database_path, 100)
    assert snapshot["mentions"] == []
    with connect(settings.database_path) as conn:
        cached = conn.execute("SELECT * FROM extraction_cache").fetchall()
    model_key = extraction_key(settings, snapshot["comment_records"][0]["input"])
    before_version = processor_version(settings)
    install_review(monkeypatch, snapshot["comment_records"][0], dune())
    assert processor_version(settings) != before_version
    assert extraction_key(settings, snapshot["comment_records"][0]["input"]) == model_key
    result = rebuild_thread(
        settings, 100, staging_path=settings.database_path.parent / "new-stage.db"
    )
    assert result["metrics"]["llm_requests"] == 0
    assert result["metrics"]["llm_cache_hits"] == 1
    corrected = thread_report(settings.database_path, 100)
    assert corrected["resolved_mentions"] == 1
    assert corrected["comment_records"][0]["comment_review"]["original_result"] == {"mentions": []}
    assert (
        corrected["mentions"][0]["extraction"]["comment_review"]["reviewer"] == "Fixture reviewer"
    )
    with connect(settings.database_path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM extraction_cache")] == [
            tuple(row) for row in cached
        ]
        assert conn.execute("SELECT recommendation_count FROM books").fetchone()[0] == 1
    repeated = rebuild_thread(settings, 100, staging_path=Path(result["staging"]))
    assert repeated["metrics"]["comments_skipped"] == 1
    assert requests.count("/v1/responses") == 1


@pytest.mark.parametrize("replacement", [{"mentions": []}, dune("neutral")])
def test_rejection_and_classification_rebuild_counts_tags_search_and_can_be_reverted(
    settings, monkeypatch, replacement
):
    seed_old_library(settings)
    requests, _ = mocks(settings, monkeypatch)
    rebuild_thread(settings, 100)
    comment = thread_report(settings.database_path, 100)["comment_records"][0]
    decisions = install_review(monkeypatch, comment, replacement)
    rebuild_thread(settings, 100)
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT recommendation_count FROM books").fetchone()[0] == 0
        assert conn.execute("SELECT mention_count FROM books").fetchone()[0] == len(
            replacement["mentions"]
        )
        assert conn.execute("SELECT COUNT(*) FROM book_mention_tags").fetchone()[0] == len(
            replacement["mentions"]
        )
        indexed = conn.execute("SELECT hn_context FROM books_fts").fetchone()
        if replacement["mentions"]:
            assert indexed and indexed[0]
        else:
            assert indexed is None
    report = thread_report(settings.database_path, 100)
    assert (
        report["comment_records"][0]["comment_review"]["original_result"]["mentions"][0][
            "sentiment"
        ]
        == "recommended"
    )
    assert report["comment_records"][0]["comment_review"]["result"] == replacement
    decisions.comments.clear()
    rebuild_thread(settings, 100)
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT recommendation_count FROM books").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM applied_comment_reviews").fetchone()[0] == 0
    assert requests.count("/v1/responses") == 1


def test_stale_review_aborts_before_network_and_preserves_live_library(settings, monkeypatch):
    seed_old_library(settings)
    requests, _ = mocks(settings, monkeypatch)
    result = rebuild_thread(settings, 100)
    before = thread_report(settings.database_path, 100)
    install_review(monkeypatch, before["comment_records"][0], {"mentions": []})
    with connect(Path(result["staging"])) as conn:
        store_raw_item(conn, {"id": 101, "parent": 100, "text": "I recommend Piranesi."}, 100)
    total = len(requests)
    with pytest.raises(ValueError, match="source changed"):
        rebuild_thread(settings, 100)
    assert len(requests) == total
    assert (
        thread_report(settings.database_path, 100)["snapshot_digest"] == before["snapshot_digest"]
    )


@pytest.mark.parametrize(
    "edit",
    [
        {"raw": '"Dune"'},
        {"work_id": "/works/OL1W"},
        {"tags": ["nonexistent-topic"]},
        {"author_source": "unknown"},
    ],
)
def test_reviewed_fields_are_never_silently_normalized(edit):
    comment = {
        "comment_id": 101,
        "hn_url": "https://news.ycombinator.com/item?id=101",
        "input": {"current_comment": {"text": "Dune", "links": []}, "ancestors": []},
    }
    result = dune()
    result["mentions"][0].update(edit)
    with pytest.raises(ValueError):
        check_review(make_review(comment, result), comment["input"])


def test_ancestor_edits_invalidate_pronoun_review():
    payload = {
        "current_comment": {"text": "This one is great", "links": []},
        "ancestors": [{"id": 99, "text": "Dune by Frank Herbert"}],
    }
    comment = {
        "comment_id": 101,
        "hn_url": "https://news.ycombinator.com/item?id=101",
        "input": payload,
    }
    result = dune()
    result["mentions"][0]["raw"] = "This one is great"
    review = make_review(comment, result)
    payload["ancestors"][0]["text"] = "Piranesi by Susanna Clarke"
    with pytest.raises(ValueError, match="source changed"):
        check_review(review, payload)


def test_cli_prepares_review_without_mutating_database_or_overwriting_files(
    settings, monkeypatch, tmp_path
):
    seed_old_library(settings)
    mocks(settings, monkeypatch)
    rebuild_thread(settings, 100)
    before = thread_report(settings.database_path, 100)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"snapshot": before}))
    mentions = tmp_path / "mentions.json"
    mentions.write_text('{"mentions": []}')
    proposed = tmp_path / "proposed.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comment_reviews",
            "--snapshot",
            str(snapshot),
            "--comment",
            "101",
            "--mentions",
            str(mentions),
            "--reviewer",
            "Fixture reviewer",
            "--reason",
            "Reject a source-checked false mention",
            "--case",
            "false-positive",
            "--output",
            str(proposed),
        ],
    )
    assert comment_reviews.main() == 0
    prepared = comment_reviews.read_reviews(proposed)
    review = next(r for r in prepared.comments if r.comment_id == 101)
    check_review(review, before["comment_records"][0]["input"])
    assert review.result.mentions == []
    saved = proposed.read_text()
    with pytest.raises(SystemExit):
        comment_reviews.main()
    assert proposed.read_text() == saved
    assert (
        thread_report(settings.database_path, 100)["snapshot_digest"] == before["snapshot_digest"]
    )
