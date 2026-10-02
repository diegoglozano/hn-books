import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from app.clients import MetadataClient, RemoteClient
from app.config import Settings, get_settings, library_config
from app.db import store_raw_item, utc_now
from app.extraction import (
    classify_mention,
    extract_mentions,
    mention_context,
    normalize_title,
    plain_text,
)
from app.luna import PROMPT_VERSION, LunaExtractor, LunaResult, comment_input, extraction_key
from app.models import RunMetrics
from app.operations import progress_phase
from app.ranking import compute_book_scores, update_search_indexes
from app.resolution import resolve_book

logger = logging.getLogger(__name__)
PROCESSOR_VERSION = "5-luna-" + PROMPT_VERSION


def processor_version(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.extraction_backend == "heuristic":
        return PROCESSOR_VERSION
    return PROCESSOR_VERSION + ":" + extraction_key(settings, {})[:16]


def discover_threads(remote: RemoteClient, backfill: bool = False) -> list[int]:
    discovered: set[int] = set(remote.settings.thread_ids)
    for query in library_config()["discovery_queries"]:
        page = 0
        while True:
            params = {"query": query, "tags": "ask_hn", "hitsPerPage": 100, "page": page}
            result = remote.get(f"{remote.settings.algolia_base_url}/search", params)
            discovered.update(int(hit["objectID"]) for hit in result.get("hits", []))
            page += 1
            if not backfill or page >= result.get("nbPages", 0):
                break
    return sorted(discovered)


def fetch_thread(
    conn: sqlite3.Connection, remote: RemoteClient, thread_id: int, metrics: RunMetrics
) -> None:
    with progress_phase("fetching_hn_comments", metrics):
        _fetch_thread(conn, remote, thread_id, metrics)


def _fetch_thread(
    conn: sqlite3.Connection, remote: RemoteClient, thread_id: int, metrics: RunMetrics
) -> None:
    thread = remote.hn_item(thread_id)
    if thread.get("type") != "story":
        raise ValueError(f"HN item {thread_id} is not a thread")
    store_raw_item(conn, thread, thread_id)
    conn.execute(
        """INSERT INTO thread_checkpoints(thread_id,raw_complete) VALUES (?, 0)
        ON CONFLICT(thread_id) DO UPDATE SET raw_complete=0,
        processed_version=NULL,completed_at=NULL""",
        (thread_id,),
    )
    conn.commit()
    failures_before_fetch = metrics.failures
    metrics.threads_fetched += 1
    pending = list(thread.get("kids", []))
    seen = {thread_id}
    with ThreadPoolExecutor(max_workers=remote.settings.hn_fetch_workers) as pool:
        while pending:
            batch, pending = pending[:64], pending[64:]
            batch = [item_id for item_id in batch if item_id not in seen]
            seen.update(batch)
            futures = [(item_id, pool.submit(remote.hn_item, item_id)) for item_id in batch]
            for item_id, future in futures:
                try:
                    item = future.result()
                    pending.extend(item.get("kids", []))
                    metrics.new_comments += int(store_raw_item(conn, item, thread_id))
                    metrics.comments_fetched += 1
                    conn.commit()
                    if metrics.comments_fetched % 25 == 0:
                        logger.info(
                            "comment_fetch_progress",
                            extra={"item_id": item_id, "metrics": metrics.model_dump()},
                        )
                except Exception as exc:
                    metrics.failures += 1
                    logger.exception(
                        "comment_fetch_failed",
                        extra={
                            "item_id": item_id,
                            "detail": str(exc),
                        },
                    )
    if metrics.failures == failures_before_fetch:
        conn.execute("UPDATE thread_checkpoints SET raw_complete=1 WHERE thread_id=?", (thread_id,))
        conn.commit()
    # Each run revisits the whole tree, including children of deleted comments.


def extract_and_resolve(
    conn: sqlite3.Connection,
    metadata: MetadataClient,
    metrics: RunMetrics,
    force: bool = False,
    thread_id: int | None = None,
) -> None:
    query = "SELECT * FROM hn_comments"
    parameters: tuple = ()
    if thread_id is not None:
        query += " WHERE thread_id=?"
        parameters = (thread_id,)
    rows = conn.execute(query + " ORDER BY id", parameters).fetchall()
    known_titles = [row[0] for row in conn.execute("SELECT canonical_title FROM books")]
    settings = metadata.remote.settings
    extractor = (
        LunaExtractor(settings, conn, metrics, offline=metadata.offline)
        if settings.extraction_backend == "luna"
        else None
    )
    metrics.comments_total += len(rows)
    jobs = []
    for comment in rows:
        payload = comment_input(conn, comment) if extractor else {}
        source_key = extraction_key(settings, payload) if extractor else "heuristic"
        digest = hashlib.sha256(
            (processor_version(settings) + source_key + comment["raw_json"]).encode()
        ).hexdigest()
        has_unresolved = conn.execute(
            "SELECT 1 FROM book_mentions WHERE comment_id=? AND status='unresolved'",
            (comment["id"],),
        ).fetchone()
        if not force and digest == comment["processed_hash"] and not has_unresolved:
            metrics.comments_skipped += 1
            continue
        jobs.append((comment, payload, digest))
    results = (
        extractor.extract_many([job[1] for job in jobs])
        if extractor
        else ((index, LunaResult(mentions=[])) for index in range(len(jobs)))
    )
    try:
        with progress_phase(
            "luna_extraction_and_verification" if extractor else "extraction", metrics
        ):
            try:
                _process_results(
                    conn, metadata, metrics, jobs, results, known_titles, bool(extractor)
                )
            finally:
                results.close()
    finally:
        if extractor:
            extractor.close()


def _process_results(
    conn: sqlite3.Connection,
    metadata: MetadataClient,
    metrics: RunMetrics,
    jobs: list[tuple[sqlite3.Row, dict, str]],
    results: Iterable[tuple[int, LunaResult]],
    known_titles: list[str],
    use_luna: bool,
) -> None:
    settings = metadata.remote.settings
    for index, extracted in results:
        comment, _, digest = jobs[index]
        text = plain_text(comment["text"])
        if use_luna:
            mentions = [
                (
                    m.span(),
                    m.classification(),
                    m.evidence()
                    | {
                        "model": settings.openai_model,
                        "prompt_version": PROMPT_VERSION,
                    },
                )
                for m in extracted.mentions
            ]
        else:
            mentions = [
                (span, classify_mention(mention_context(text, span.raw), span.title), {})
                for span in extract_mentions(comment["text"], known_titles)
            ]
        prepared = []
        if use_luna and mentions:
            logger.info(
                "comment_verification_started",
                extra={"item_id": comment["id"], "detail": {"mentions": len(mentions)}},
            )
        for span, classification, evidence in mentions:
            book_id, confidence, candidates = resolve_book(conn, span, metadata)
            prepared.append((span, book_id, confidence, candidates, classification, evidence))
            metrics.mentions_extracted += 1
            metrics.classifications_performed += 1
            if book_id:
                metrics.books_resolved += 1
            else:
                metrics.unresolved_mentions += 1
        # Replace derived rows atomically, only after all lookups complete.
        conn.execute("DELETE FROM book_mentions WHERE comment_id=?", (comment["id"],))
        for span, book_id, confidence, candidates, classification, evidence in prepared:
            cursor = conn.execute(
                """INSERT INTO book_mentions(book_id,comment_id,thread_id,raw_mention,
                    normalized_mention,context_text,extraction_confidence,resolution_confidence,
                    recommendation_strength,sentiment,status,candidates_json,created_at,extraction_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    book_id,
                    comment["id"],
                    comment["thread_id"],
                    span.raw,
                    normalize_title(span.title),
                    text,
                    span.confidence,
                    confidence,
                    classification.recommendation_strength,
                    classification.sentiment,
                    "resolved" if book_id else "unresolved",
                    json.dumps(candidates),
                    utc_now(),
                    json.dumps(evidence),
                ),
            )
            for tag, tag_confidence in classification.tags.items():
                conn.execute(
                    """INSERT INTO book_mention_tags(mention_id,tag_id,confidence)
                        SELECT ?, id, ? FROM tags WHERE name=?""",
                    (cursor.lastrowid, tag_confidence, tag),
                )
        conn.execute("UPDATE hn_comments SET processed_hash=? WHERE id=?", (digest, comment["id"]))
        conn.commit()
        metrics.comments_processed += 1
        if use_luna:
            logger.info(
                "luna_processing_progress",
                extra={"item_id": comment["id"], "metrics": metrics.model_dump()},
            )


def classify_mentions(conn: sqlite3.Connection) -> int:
    if get_settings().extraction_backend == "luna":
        raise ValueError(
            "Use reprocess mentions to refresh Luna classifications from cached extraction"
        )
    count = 0
    for mention in conn.execute(
        """SELECT m.*, b.canonical_title FROM book_mentions m
        LEFT JOIN books b ON b.id=m.book_id""",
    ).fetchall():
        classification = classify_mention(
            mention_context(mention["context_text"], mention["raw_mention"]),
            mention["canonical_title"] or mention["raw_mention"],
        )
        conn.execute("DELETE FROM book_mention_tags WHERE mention_id=?", (mention["id"],))
        conn.execute(
            "UPDATE book_mentions SET sentiment=?,recommendation_strength=? WHERE id=?",
            (classification.sentiment, classification.recommendation_strength, mention["id"]),
        )
        for tag, confidence in classification.tags.items():
            conn.execute(
                "INSERT INTO book_mention_tags SELECT ?, id, ? FROM tags WHERE name=?",
                (mention["id"], confidence, tag),
            )
        count += 1
    return count


def refresh_aggregates(conn: sqlite3.Connection) -> None:
    compute_book_scores(conn)
    update_search_indexes(conn)
    conn.commit()
