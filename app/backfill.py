"""Resumable historical ingestion, committing a complete raw thread before processing it."""

import logging
import sqlite3

from app.clients import MetadataClient, RemoteClient
from app.db import utc_now
from app.models import RunMetrics
from app.pipeline import PROCESSOR_VERSION, extract_and_resolve, fetch_thread, refresh_aggregates

logger = logging.getLogger(__name__)


def completed_threads(conn: sqlite3.Connection) -> set[int]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT thread_id FROM thread_checkpoints WHERE raw_complete=1 AND processed_version=?",
            (PROCESSOR_VERSION,),
        )
    }


def ingest_backfill(
    conn: sqlite3.Connection,
    remote: RemoteClient,
    thread_ids: list[int],
    metrics: RunMetrics,
    limit: int | None = None,
    refresh: bool = False,
) -> None:
    metadata = MetadataClient(remote, conn, metrics)
    attempted = 0
    for thread_id in thread_ids:
        checkpoint = conn.execute(
            "SELECT * FROM thread_checkpoints WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if (
            not refresh
            and checkpoint
            and checkpoint["raw_complete"]
            and (checkpoint["processed_version"] == PROCESSOR_VERSION)
        ):
            metrics.threads_skipped += 1
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        logger.info(
            "backfill_thread_started",
            extra={
                "item_id": thread_id,
                "detail": {"attempt": attempted, "total": len(thread_ids)},
            },
        )
        try:
            if not refresh and checkpoint and checkpoint["raw_complete"]:
                metrics.threads_reused_raw += 1
            else:
                fetch_thread(conn, remote, thread_id, metrics)
            before_processing = metrics.failures
            extract_and_resolve(conn, metadata, metrics, thread_id=thread_id)
            # Publish each processed thread immediately; interrupted jobs keep usable results.
            refresh_aggregates(conn)
            if metrics.failures == before_processing:
                conn.execute(
                    """UPDATE thread_checkpoints SET processed_version=?,completed_at=?
                    WHERE thread_id=? AND raw_complete=1""",
                    (PROCESSOR_VERSION, utc_now(), thread_id),
                )
                conn.commit()
            logger.info(
                "backfill_thread_completed",
                extra={
                    "item_id": thread_id,
                    "metrics": metrics.model_dump(),
                },
            )
        except Exception as exc:
            conn.rollback()
            metrics.failures += 1
            logger.exception(
                "backfill_thread_failed",
                extra={
                    "item_id": thread_id,
                    "detail": str(exc),
                },
            )

    metrics.threads_remaining = len(set(thread_ids) - completed_threads(conn))
    logger.info("backfill_batch_finished", extra={"metrics": metrics.model_dump()})
