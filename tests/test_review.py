import json
import sqlite3
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.db import connect, store_raw_item
from app.evaluate_review import evaluate_review
from app.main import create_app
from app.review import content_digest, review_template, thread_report


@pytest.fixture
def review_snapshot(settings):
    with connect(settings.database_path) as conn:
        store_raw_item(conn, {"id": 100, "title": "Reading", "text": "Thread body"}, 100)
        for item in (
            {"id": 101, "parent": 100, "by": "alice", "text": "I recommend <i>Dune</i>."},
            {"id": 102, "parent": 101, "by": "bob", "text": "I agree. Mystery Book too."},
            {"id": 103, "parent": 100, "deleted": True},
            {"id": 104, "parent": 100, "text": "No reading this month."},
        ):
            store_raw_item(conn, item, 100)
        conn.execute("UPDATE hn_comments SET processed_hash='processed'")
        conn.execute("""INSERT INTO thread_checkpoints VALUES (100,1,'test-processor','today')""")
        conn.execute("""INSERT INTO books(id,canonical_title,normalized_title,authors,
        openlibrary_id,isbn_13,metadata_json,created_at,updated_at)
        VALUES (1,'Dune','dune','["Frank Herbert"]','/works/OL1W','["9780441172719"]',
        '{"resolution_version":2}','today','today')""")
        for comment_id, title, book_id, status, author, source, sentiment, strength in (
            (101, "Dune", 1, "resolved", "Frank Herbert", "inferred", "recommended", 0.95),
            (102, "Mystery Book", None, "unresolved", None, "unknown", "neutral", 0.2),
        ):
            extraction = {
                "title": title,
                "raw": title,
                "author": author,
                "author_source": source,
                "sentiment": sentiment,
                "model": "mock-luna",
                "prompt_version": "test",
            }
            conn.execute(
                """INSERT INTO book_mentions(book_id,comment_id,thread_id,raw_mention,
            normalized_mention,context_text,extraction_confidence,recommendation_strength,
            sentiment,status,candidates_json,created_at,extraction_json)
            VALUES (?, ?,100, ?, ?, 'context',0.8, ?, 0, ?, ?, 'today', ?)""",
                (
                    book_id,
                    comment_id,
                    title,
                    title.lower(),
                    strength,
                    status,
                    '[{"key":"/works/OL2W"}]' if book_id is None else "[]",
                    json.dumps(extraction),
                ),
            )
        conn.execute(
            """INSERT INTO book_mention_tags SELECT 1,id,0.8 FROM tags WHERE name='fiction'"""
        )
        conn.execute("""INSERT INTO ingestion_runs(command,started_at,status,metrics_json,error)
        VALUES ('rebuild thread 100','today','failed','{"llm_input_tokens":500}',
        'Private deployment detail should not appear')""")
    return thread_report(settings.database_path, 100)


def labeled_review(snapshot):
    labels = review_template(snapshot)
    labels["reviewer"] = "Test fixture; mocked integration, not model accuracy"
    for comment in labels["comments"]:
        comment.update(status="reviewed", expected_mentions=[], case_types=["empty"])
    labels["comments"][0].update(
        case_types=["explicit_recommendation"],
        expected_mentions=[
            {
                "raw": "Dune",
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "work_id": "/works/OL1W",
                "author_source": "inferred",
                "sentiment": "recommended",
                "strength": 0.95,
                "tags": ["fiction"],
            }
        ],
    )
    labels["comments"][1].update(
        case_types=["ambiguous"],
        expected_mentions=[
            {
                "raw": "Mystery Book",
                "title": "Mystery Book",
                "authors": [],
                "work_id": None,
                "author_source": "unknown",
                "sentiment": "neutral",
                "strength": 0.2,
                "tags": [],
            }
        ],
    )
    return labels


def redigest(snapshot):
    snapshot["snapshot_digest"] = content_digest(
        {
            key: value
            for key, value in snapshot.items()
            if key not in {"exported_at", "snapshot_digest"}
        }
    )


def test_complete_read_only_snapshot_and_pending_template(settings, review_snapshot):
    snapshot = review_snapshot
    assert snapshot["comments"] == snapshot["processed_comments"] == 4
    assert snapshot["resolved_mentions"] == snapshot["unresolved_mentions"] == 1
    assert [c["comment_id"] for c in snapshot["comments_without_mentions"]] == [103, 104]
    assert snapshot["comment_records"][1]["input"]["ancestors"] == [
        {"id": 101, "text": "I recommend Dune."}
    ]
    assert snapshot["comment_records"][2]["deleted"]
    assert snapshot["mentions"][0]["openlibrary_id"] == "/works/OL1W"
    assert snapshot["mentions"][0]["isbn_13"] == ["9780441172719"]
    assert snapshot["mentions"][0]["tags"] == {"fiction": 0.8}
    assert snapshot["mentions"][1]["candidates"] == [{"key": "/works/OL2W"}]
    assert snapshot["observed_versions"] == {
        "models": ["mock-luna"],
        "prompts": ["test"],
        "matching": [2],
    }
    assert snapshot["recorded_rebuild_runs"][0]["metrics"]["llm_input_tokens"] == 500
    assert "Private deployment" not in json.dumps(snapshot)
    template = review_template(snapshot)
    assert len(template["comments"]) == 4
    assert all(
        c["status"] == "pending" and c["expected_mentions"] is None for c in template["comments"]
    )
    assert (
        thread_report(settings.database_path, 100)["snapshot_digest"] == snapshot["snapshot_digest"]
    )
    with pytest.raises(ValueError, match="not stored"):
        thread_report(settings.database_path, 999)


def test_authenticated_export_requires_existing_admin_token(settings, review_snapshot):
    route = "/api/admin/threads/100/review"
    with TestClient(create_app(settings)) as client:
        assert client.get(route).status_code == 404
    settings.admin_token = "test-admin-token"
    with TestClient(create_app(settings)) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            assert client.get(route, headers=headers).status_code == 401
        headers = {"Authorization": "Bearer test-admin-token"}
        response = client.get(route, headers=headers)
        assert response.status_code == 200
        assert response.json()["snapshot"]["snapshot_digest"] == review_snapshot["snapshot_digest"]
        assert response.json()["review_template"]["comments"][2]["status"] == "pending"
        assert "test-admin-token" not in response.text
        assert client.get("/api/admin/threads/999/review", headers=headers).status_code == 404


def test_snapshot_is_consistent_during_a_concurrent_commit(settings, review_snapshot, monkeypatch):
    from app import review

    original = review.comment_input
    changed = False

    def concurrent_update(conn, comment):
        nonlocal changed
        if not changed:
            changed = True
            with connect(settings.database_path) as writer:
                writer.execute("UPDATE hn_comments SET text='changed' WHERE id=101")
                writer.execute("UPDATE thread_checkpoints SET processed_version='changed'")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("DELETE FROM hn_comments")
        return original(conn, comment)

    monkeypatch.setattr(review, "comment_input", concurrent_update)
    report = thread_report(settings.database_path, 100)
    assert report["snapshot_digest"] == review_snapshot["snapshot_digest"]
    assert report["processing_checkpoint"]["processed_version"] == "test-processor"
    assert report["comment_records"][1]["input"]["ancestors"][0]["text"] == "I recommend Dune."


def test_evaluation_counts_misses_false_matches_and_case_metrics(review_snapshot):
    snapshot = deepcopy(review_snapshot)
    labels = labeled_review(snapshot)
    # A missed Dune, a confidently wrong resolution, and a false positive in an empty comment.
    snapshot["mentions"].pop(0)
    snapshot["mentions"][0].update(status="resolved", openlibrary_id="/works/OL2W")
    false_mention = deepcopy(snapshot["mentions"][0])
    false_mention.update(comment_id=104, raw_mention="invented")
    false_mention["extraction"]["title"] = "Invented"
    snapshot["mentions"].append(false_mention)
    redigest(snapshot)
    report = evaluate_review(snapshot, labels)
    metrics = report["results"]["saved_extractor"]["metrics"]
    assert metrics["mention_precision"] == metrics["mention_recall"] == 0.5
    assert metrics["abstention_accuracy"] == 0
    assert metrics["grounded_evidence_rate"] == 0.5
    assert metrics["canonicalization_accuracy"] is None  # No matched known work.
    assert report["coverage"]["complete_thread_review"]
    assert {e["kind"] for e in report["results"]["saved_extractor"]["errors"]} == {
        "omission",
        "unexpected_resolution",
        "false_positive",
    }
    assert report["results"]["saved_extractor"]["by_case"]["explicit_recommendation"]["fn"] == 1
    assert "canonicalization_accuracy" not in report["results"]["heuristic_baseline"]["metrics"]


def test_evaluation_wrong_canonical_identity_is_counted(review_snapshot):
    snapshot = deepcopy(review_snapshot)
    snapshot["mentions"][0].update(openlibrary_id="/works/OL999W", authors=["Someone Else"])
    redigest(snapshot)
    report = evaluate_review(snapshot, labeled_review(snapshot))
    metrics = report["results"]["saved_extractor"]["metrics"]
    assert metrics["canonicalization_accuracy"] == metrics["author_accuracy"] == 0
    assert metrics["unresolved_rate"] == 0.5
    assert metrics["abstention_accuracy"] == 1
    assert "identity_mismatch" in {
        e["kind"] for e in report["results"]["saved_extractor"]["errors"]
    }
    assert "identity_mismatch" not in {
        e["kind"] for e in report["results"]["heuristic_baseline"]["errors"]
    }


def test_evaluation_keeps_original_luna_omissions_and_false_positives_visible(review_snapshot):
    snapshot = deepcopy(review_snapshot)
    snapshot["comment_records"][0]["comment_review"] = {"original_result": {"mentions": []}}
    snapshot["comment_records"][3]["comment_review"] = {
        "original_result": {
            "mentions": [
                {
                    "raw": "No reading",
                    "title": "No reading",
                    "author": None,
                    "author_source": "unknown",
                    "work_id": None,
                    "confidence": 0.8,
                    "sentiment": "recommended",
                    "tags": ["fiction"],
                }
            ]
        }
    }
    redigest(snapshot)
    report = evaluate_review(snapshot, labeled_review(snapshot))
    assert report["applied_comment_reviews"] == [101, 104]
    assert report["results"]["saved_extractor"]["metrics"]["mention_recall"] == 1
    original = report["results"]["original_luna_extraction"]
    assert original["metrics"]["mention_recall"] == original["metrics"]["mention_precision"] == 0.5
    assert {error["kind"] for error in original["errors"]} == {
        "omission",
        "false_positive",
        "unresolved",
    }
    assert "canonicalization_accuracy" not in original["metrics"]


def test_evaluation_blocks_pending_and_mismatched_sources(review_snapshot):
    labels = review_template(review_snapshot)
    with pytest.raises(ValueError, match="Complete evaluation"):
        evaluate_review(review_snapshot, labels)
    with pytest.raises(ValueError, match="No reviewed"):
        evaluate_review(review_snapshot, labels, allow_partial=True)
    labels = labeled_review(review_snapshot)
    labels["comments"][3].update(status="pending", expected_mentions=None)
    with pytest.raises(ValueError, match="Complete evaluation"):
        evaluate_review(review_snapshot, labels)
    partial = evaluate_review(review_snapshot, labels, allow_partial=True)
    assert partial["coverage"] == {
        "stored_comments": 4,
        "reviewed_comments": 3,
        "complete_thread_review": False,
    }
    labels["source_digest"] = "different"
    with pytest.raises(ValueError, match="different source"):
        evaluate_review(review_snapshot, labels)


def test_sparse_regression_labels_require_explicit_partial_evaluation(review_snapshot):
    labels = labeled_review(review_snapshot)
    labels["comments"] = [labels["comments"][0], labels["comments"][3]]
    with pytest.raises(ValueError, match="each stored comment"):
        evaluate_review(review_snapshot, labels)
    report = evaluate_review(review_snapshot, labels, allow_partial=True)
    assert report["coverage"] == {
        "stored_comments": 4,
        "reviewed_comments": 2,
        "complete_thread_review": False,
    }
    assert report["results"]["saved_extractor"]["metrics"]["predicted"] == 1
    labels["comments"].append(labels["comments"][0])
    with pytest.raises(ValueError, match="each stored comment"):
        evaluate_review(review_snapshot, labels, allow_partial=True)
    labels["comments"][-1] = labels["comments"][-1] | {"comment_id": 999}
    with pytest.raises(ValueError, match="each stored comment"):
        evaluate_review(review_snapshot, labels, allow_partial=True)


@pytest.mark.parametrize(
    "change", ["duplicate", "missing", "ungrounded", "topic", "anonymous", "url"]
)
def test_review_labels_must_be_explicit_complete_and_grounded(review_snapshot, change):
    labels = labeled_review(review_snapshot)
    if change == "duplicate":
        labels["comments"].append(labels["comments"][0])
    elif change == "missing":
        labels["comments"].pop()
    elif change == "ungrounded":
        labels["comments"][0]["expected_mentions"][0]["raw"] = "Absent excerpt"
    elif change == "topic":
        labels["comments"][0]["expected_mentions"][0]["tags"] = ["not-in-taxonomy"]
    elif change == "anonymous":
        labels["reviewer"] = None
    elif change == "url":
        labels["comments"][0]["source_url"] = "https://example.com"
    with pytest.raises(ValueError):
        evaluate_review(review_snapshot, labels)


def test_tampered_saved_predictions_and_incomplete_processing_cannot_pass(review_snapshot):
    labels = labeled_review(review_snapshot)
    snapshot = deepcopy(review_snapshot)
    snapshot["mentions"][0]["openlibrary_id"] = "/works/OL999W"
    with pytest.raises(ValueError, match="digest"):
        evaluate_review(snapshot, labels)
    snapshot["comment_records"][0]["processed"] = False
    redigest(snapshot)
    with pytest.raises(ValueError, match="Complete evaluation"):
        evaluate_review(snapshot, labels)
    snapshot["comment_records"][0]["processed"] = True
    snapshot["processing_checkpoint"]["raw_complete"] = 0
    redigest(snapshot)
    with pytest.raises(ValueError, match="Complete evaluation"):
        evaluate_review(snapshot, labels)


def test_review_source_digest_ignores_votes_but_tracks_changed_text(settings, review_snapshot):
    with connect(settings.database_path) as conn:
        story = json.loads(
            conn.execute("SELECT raw_json FROM hn_threads WHERE id=100").fetchone()[0]
        )
        store_raw_item(conn, story | {"score": 999}, 100)
    assert (
        thread_report(settings.database_path, 100)["source_digest"]
        == review_snapshot["source_digest"]
    )
    with connect(settings.database_path) as conn:
        comment = json.loads(
            conn.execute("SELECT raw_json FROM hn_comments WHERE id=101").fetchone()[0]
        )
        store_raw_item(conn, comment | {"text": "I no longer recommend Dune."}, 100)
    assert (
        thread_report(settings.database_path, 100)["source_digest"]
        != review_snapshot["source_digest"]
    )


@pytest.mark.parametrize("missing_field", ["processed_version", "completed_at"])
def test_old_comment_hashes_do_not_pass_an_unfinished_processing_checkpoint(
    review_snapshot, missing_field
):
    snapshot = deepcopy(review_snapshot)
    assert all(comment["processed_hash"] for comment in snapshot["comment_records"])
    snapshot["processing_checkpoint"][missing_field] = None
    redigest(snapshot)
    labels = labeled_review(snapshot)
    with pytest.raises(ValueError, match="finished processing checkpoint"):
        evaluate_review(snapshot, labels)
    report = evaluate_review(snapshot, labels, allow_partial=True)
    assert not report["coverage"]["complete_thread_review"]


def test_explicit_aliases_and_verified_duplicate_work_ids(review_snapshot):
    labels = labeled_review(review_snapshot)
    dune = labels["comments"][0]["expected_mentions"][0]
    dune.update(
        title="Dune: verified alternative",
        aliases=["Dune"],
        work_id="/works/OL99W",
        accepted_work_ids=["/works/OL1W"],
    )
    result = evaluate_review(review_snapshot, labels)
    assert result["results"]["saved_extractor"]["metrics"]["canonicalization_accuracy"] == 1
    assert result["results"]["saved_extractor"]["metrics"]["mention_recall"] == 1


def test_cli_protects_existing_labels_and_evaluates_api_bundle(
    settings, review_snapshot, monkeypatch, tmp_path
):
    from app.evaluate_review import main as evaluate_main
    from app.review import main as review_main

    labels_path = tmp_path / "review-labels.json"
    labels_path.write_text(json.dumps(labeled_review(review_snapshot)))
    labels_before = labels_path.read_bytes()
    monkeypatch.setattr("sys.argv", ["review", "--thread", "100", "--template", str(labels_path)])
    with pytest.raises(SystemExit) as exit_info:
        review_main()
    assert exit_info.value.code == 2
    assert labels_path.read_bytes() == labels_before
    bundle_path = tmp_path / "review-bundle.json"
    bundle_path.write_text(
        json.dumps(
            {"snapshot": review_snapshot, "review_template": review_template(review_snapshot)}
        )
    )
    output = tmp_path / "evaluation.json"
    arguments = [
        "evaluate_review",
        "--snapshot",
        str(bundle_path),
        "--labels",
        str(labels_path),
        "--output",
        str(output),
    ]
    monkeypatch.setattr("sys.argv", arguments)
    assert evaluate_main() == 0
    first = output.read_bytes()
    assert evaluate_main() == 0
    assert output.read_bytes() == first
    monkeypatch.setattr("sys.argv", [*arguments[:-1], str(labels_path)])
    with pytest.raises(SystemExit) as exit_info:
        evaluate_main()
    assert exit_info.value.code == 2
    assert labels_path.read_bytes() == labels_before
