"""Back up the library, build one thread separately, and publish only a complete result."""

import argparse
import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app.clients import MetadataClient, RemoteClient
from app.config import Settings, get_settings
from app.db import connect, initialize, store_raw_item
from app.operations import configure_logging, tracked_run, writer_lock
from app.pipeline import extract_and_resolve, fetch_thread, processor_version, refresh_aggregates

logger = logging.getLogger(__name__)


def backup_database(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("Backup must have a different path from the library")
    if not source.is_file():
        raise ValueError(f"Database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing backups from accidental overwrite.
    with destination.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(destination)) as backup,
        ):
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def seed_staging(backup: Path, staging: Path, thread_id: int) -> None:
    initialize(staging)
    with connect(backup) as source, connect(staging) as target:
        thread = source.execute(
            "SELECT raw_json FROM hn_threads WHERE id=?", (thread_id,)
        ).fetchone()
        if thread:
            store_raw_item(target, json.loads(thread[0]), thread_id)
            for comment in source.execute(
                "SELECT raw_json FROM hn_comments WHERE thread_id=?", (thread_id,)
            ):
                store_raw_item(target, json.loads(comment[0]), thread_id)
        # Keep successful external responses, but discard all old canonical books/mentions.
        for table in ("metadata_cache", "extraction_cache"):
            if source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                for row in source.execute(f"SELECT * FROM {table}"):
                    target.execute(
                        f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' for _ in row)})",
                        tuple(row),
                    )


def publish_staging(staging: Path, live: Path, thread_id: int, version: str) -> None:
    with connect(staging) as source:
        if [row[0] for row in source.execute("SELECT id FROM hn_threads")] != [thread_id]:
            raise RuntimeError("Staging database must contain exactly the selected thread")
        checkpoint = source.execute(
            "SELECT raw_complete,processed_version FROM thread_checkpoints WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if not checkpoint or not checkpoint[0] or checkpoint[1] != version:
            raise RuntimeError("Staging thread has not completed processing")
        if source.execute("SELECT 1 FROM hn_comments WHERE processed_hash IS NULL").fetchone():
            raise RuntimeError("Staging contains unprocessed comments")
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Staging database integrity check failed")
        if source.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Staging database contains invalid references")
        source.commit()
        # SQLite backup replaces the destination transactionally, including its WAL state.
        # Do not rename/unlink a WAL database while the web app has connections open.
        with closing(sqlite3.connect(live, timeout=30)) as destination:
            source.backup(destination)


def rebuild_thread(
    settings: Settings,
    thread_id: int,
    *,
    backup_path: Path | None = None,
    staging_path: Path | None = None,
    refresh: bool = False,
) -> dict:
    if thread_id <= 0:
        raise ValueError("Thread ID must be positive")
    if settings.extraction_backend != "luna":
        raise ValueError("The library rebuild requires EXTRACTION_BACKEND=luna")
    if not settings.openai_api_key.get_secret_value():
        raise ValueError("Configure OPENAI_API_KEY before rebuilding the library")
    live = settings.database_path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_path or live.parent / "backups" / f"{live.stem}-{stamp}.db"
    staging_path = staging_path or live.with_name(f"{live.stem}.thread-{thread_id}.staging.db")
    if len({p.resolve() for p in (live, backup_path, staging_path)}) != 3:
        raise ValueError("Library, backup and staging paths must be different")
    staged_settings = settings.model_copy(update={"database_path": staging_path})
    with writer_lock(settings):
        with writer_lock(staged_settings):
            if live.exists():
                backup_database(live, backup_path)
                logger.info("library_backup_verified", extra={"path": str(backup_path)})
            if not staging_path.exists():
                if live.exists():
                    seed_staging(backup_path, staging_path, thread_id)
                else:
                    initialize(staging_path)
            else:
                initialize(staging_path)
        with tracked_run(staged_settings, f"rebuild thread {thread_id}") as metrics:
            remote = RemoteClient(staged_settings)
            try:
                with connect(staging_path) as conn:
                    existing_ids = {row[0] for row in conn.execute("SELECT id FROM hn_threads")}
                    if existing_ids - {thread_id}:
                        raise ValueError(
                            "Staging path contains another thread; choose a different path"
                        )
                    checkpoint = conn.execute(
                        "SELECT raw_complete FROM thread_checkpoints WHERE thread_id=?",
                        (thread_id,),
                    ).fetchone()
                    if refresh or not checkpoint or not checkpoint[0]:
                        fetch_thread(conn, remote, thread_id, metrics)
                    else:
                        metrics.threads_reused_raw += 1
                    if metrics.failures:
                        raise RuntimeError(
                            "Comment fetch failed; library unchanged; rerun to resume staging"
                        )
                    extract_and_resolve(
                        conn, MetadataClient(remote, conn, metrics), metrics, thread_id=thread_id
                    )
                    refresh_aggregates(conn)
                    if metrics.failures:
                        raise RuntimeError(
                            "Metadata lookup failed; library unchanged; rerun to resume staging"
                        )
                    conn.execute(
                        "UPDATE thread_checkpoints SET processed_version=?,completed_at=? "
                        "WHERE thread_id=?",
                        (processor_version(settings), datetime.now(UTC).isoformat(), thread_id),
                    )
            finally:
                remote.close()
        # Publish after the completed run commits, still holding the live writer lock.
        with writer_lock(staged_settings):
            publish_staging(staging_path, live, thread_id, processor_version(settings))
    result = {
        "thread_id": thread_id,
        "backup": str(backup_path) if backup_path.exists() else None,
        "staging": str(staging_path),
        "metrics": metrics.model_dump(),
    }
    logger.info("single_thread_library_published", extra={"detail": result})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", type=int, default=49893157)
    parser.add_argument(
        "--backup", type=Path, help="Unique backup destination; default: timestamped backup"
    )
    parser.add_argument(
        "--staging", type=Path, help="Persistent staging database; reused on retries"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Refetch comments even if staging is complete"
    )
    args = parser.parse_args()
    configure_logging()
    rebuild_thread(
        get_settings(),
        args.thread,
        backup_path=args.backup,
        staging_path=args.staging,
        refresh=args.refresh,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
