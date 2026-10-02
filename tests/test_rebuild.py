import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.clients import RemoteClient
from app.db import connect, store_raw_item
from app.luna import LunaExtractor
from app.operations import writer_lock
from app.pipeline import processor_version
from app.rebuild import backup_database, publish_staging, rebuild_thread
from app.review import thread_report


def seed_old_library(settings):
    with connect(settings.database_path) as conn:
        for item_id in (100, 200):
            store_raw_item(conn, {"id": item_id, "type": "story", "title": "Old thread"}, item_id)
        conn.execute("""INSERT INTO books(
        canonical_title,normalized_title,authors,created_at,updated_at)
        VALUES ('Wrong old book','wrong old book','[]','yesterday','yesterday')""")


def mocks(settings, monkeypatch, fail_metadata=False, duplicate_mentions=False):
    settings.extraction_backend = "luna"
    settings.openai_api_key = SecretStr("test-key")
    requests = []
    state = {"fail_metadata": fail_metadata}
    doc = {"title": "Dune", "key": "/works/OL1W", "author_name": ["Frank Herbert"]}

    def handle(request):
        requests.append(request.url.path)
        path = request.url.path
        if path.endswith("/item/100.json"):
            return httpx.Response(
                200, json={"id": 100, "type": "story", "title": "New reading thread", "kids": [101]}
            )
        if path.endswith("/item/101.json"):
            return httpx.Response(200, json={"id": 101, "parent": 100, "text": "I recommend Dune."})
        if path == "/search.json":
            return (
                httpx.Response(403)
                if state["fail_metadata"]
                else httpx.Response(200, json={"docs": [doc]})
            )
        if path == "/works/OL1W.json":
            return httpx.Response(200, json={"title": "Dune"})
        if path == "/v1/responses":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "usage": {"input_tokens": 500, "output_tokens": 80},
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "mentions": [
                                                {
                                                    "raw": "Dune",
                                                    "title": "Dune",
                                                    "author": "Frank Herbert",
                                                    "author_source": "inferred",
                                                    "work_id": None,
                                                    "confidence": 0.98,
                                                    "sentiment": "recommended",
                                                    "tags": ["fiction"],
                                                }
                                            ]
                                            * (2 if duplicate_mentions else 1)
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                },
            )
        raise AssertionError(path)

    monkeypatch.setattr(
        "app.rebuild.RemoteClient",
        lambda settings: RemoteClient(
            settings, httpx.Client(transport=httpx.MockTransport(handle))
        ),
    )
    monkeypatch.setattr(
        "app.pipeline.LunaExtractor",
        lambda settings, conn, metrics, offline: LunaExtractor(
            settings,
            conn,
            metrics,
            offline,
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    return requests, state


def test_backup_includes_wal_and_never_overwrites_existing_backup(settings):
    seed_old_library(settings)
    backup = settings.database_path.parent / "backup.db"
    # Keep a connection open so the committed rows remain in the WAL.
    with connect(settings.database_path) as conn:
        store_raw_item(conn, {"id": 300, "type": "story", "title": "Still in WAL"}, 300)
        conn.commit()
        backup_database(settings.database_path, backup)
    with connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0] == 3
        assert conn.execute("SELECT canonical_title FROM books").fetchone()[0] == "Wrong old book"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(FileExistsError):
        backup_database(settings.database_path, backup)
    with pytest.raises(ValueError, match="different path"):
        backup_database(settings.database_path, settings.database_path)


@pytest.mark.parametrize("duplicate_mentions", [False, True])
def test_rebuild_publishes_only_selected_thread_and_preserves_backup(
    settings, monkeypatch, duplicate_mentions
):
    seed_old_library(settings)
    requests, _ = mocks(settings, monkeypatch, duplicate_mentions=duplicate_mentions)
    result = rebuild_thread(settings, 100)
    with connect(Path(result["backup"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0] == 2
        assert conn.execute("SELECT canonical_title FROM books").fetchone()[0] == "Wrong old book"
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT id FROM hn_threads").fetchall()[0][0] == 100
        assert conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0] == 1
        assert conn.execute("SELECT canonical_title FROM books").fetchone()[0] == "Dune"
        assert conn.execute("SELECT recommendation_count FROM books").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
        assert (
            conn.execute("SELECT status FROM ingestion_runs ORDER BY id DESC").fetchone()[0]
            == "completed"
        )
    assert result["metrics"]["llm_requests"] == 1
    assert requests.count("/v1/responses") == 1
    report = thread_report(settings.database_path, 100)
    assert report["processed_comments"] == 1
    assert report["resolved_mentions"] == 1
    assert report["mentions"][0]["extraction"]["author"] == "Frank Herbert"
    if duplicate_mentions:
        assert len(report["mentions"][0]["extraction"]["duplicate_mentions"]) == 2


@pytest.mark.parametrize("duplicate_mentions", [False, True])
def test_failed_rebuild_leaves_live_unchanged_and_resumes_without_paying_again(
    settings, monkeypatch, duplicate_mentions
):
    seed_old_library(settings)
    requests, state = mocks(
        settings, monkeypatch, fail_metadata=True, duplicate_mentions=duplicate_mentions
    )
    with pytest.raises(RuntimeError, match="Metadata lookup failed"):
        rebuild_thread(settings, 100)
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0] == 2
        assert conn.execute("SELECT canonical_title FROM books").fetchone()[0] == "Wrong old book"
    state["fail_metadata"] = False
    result = rebuild_thread(settings, 100)
    assert requests.count("/v1/responses") == 1
    assert requests.count("/v0/item/100.json") == 1
    assert result["metrics"]["llm_cache_hits"] == 1
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT canonical_title FROM books").fetchone()[0] == "Dune"


def test_incomplete_stage_cannot_replace_library(settings):
    seed_old_library(settings)
    staging = settings.database_path.parent / "stage.db"
    backup_database(settings.database_path, staging)
    with pytest.raises(RuntimeError, match="exactly the selected thread"):
        publish_staging(staging, settings.database_path, 100, processor_version(settings))
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0] == 2


def test_missing_key_and_active_writer_prevent_mutation(settings, monkeypatch):
    seed_old_library(settings)
    settings.extraction_backend = "luna"
    settings.openai_api_key = SecretStr("")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        rebuild_thread(settings, 100)
    assert not (settings.database_path.parent / "backups").exists()
    mocks(settings, monkeypatch)
    with writer_lock(settings), pytest.raises(RuntimeError, match="already running"):
        rebuild_thread(settings, 100)
    assert not (settings.database_path.parent / "backups").exists()


def test_backup_failure_prevents_fetch_and_reset(settings, monkeypatch):
    seed_old_library(settings)
    requests, _ = mocks(settings, monkeypatch)

    def fail_backup(*args):
        raise OSError("disk full")

    monkeypatch.setattr("app.rebuild.backup_database", fail_backup)
    with pytest.raises(OSError, match="disk full"):
        rebuild_thread(settings, 100)
    assert requests == []
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0] == 2
