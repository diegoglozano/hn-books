"""Curated reading threads and date-bounded search for broader backfills."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.clients import RemoteClient
from app.config import library_config

logger = logging.getLogger(__name__)
Scope = Literal["ask-hn", "stories"]
Collection = Literal["discussions", "reading"]


@dataclass(frozen=True)
class ThreadCandidate:
    id: int
    title: str
    created_at: int
    comments: int


def curated_reading_threads(
    remote: RemoteClient, start: int, end: int, min_comments: int
) -> list[ThreadCandidate]:
    """Check configured IDs against current HN story records, without title matching."""
    configured = library_config()["reading_thread_ids"]
    if not isinstance(configured, list) or any(
        type(item_id) is not int or item_id <= 0 for item_id in configured
    ):
        raise ValueError("reading_thread_ids must be a list of positive integer HN IDs")
    candidates = []
    with ThreadPoolExecutor(max_workers=max(1, remote.settings.hn_fetch_workers)) as executor:
        for item in executor.map(remote.hn_item, sorted(set(configured))):
            if item.get("type") != "story":
                raise ValueError(f"Curated HN item {item['id']} is not a thread")
            if item.get("deleted") or item.get("dead"):
                logger.warning("curated_thread_unavailable", extra={"item_id": item["id"]})
                continue
            created_at = int(item["time"])
            comments = int(item.get("descendants") or 0)
            if start <= created_at < end and comments >= min_comments:
                candidates.append(ThreadCandidate(item["id"], item["title"], created_at, comments))
    candidates.sort(key=lambda candidate: (candidate.created_at, candidate.id))
    logger.info(
        "curated_reading_threads_checked",
        extra={
            "detail": {
                "configured": len(set(configured)),
                "eligible": len(candidates),
                "min_comments": min_comments,
                "since": datetime.fromtimestamp(start, UTC).isoformat(),
                "until": datetime.fromtimestamp(end, UTC).isoformat(),
            }
        },
    )
    return candidates


def discover_reading_threads(
    remote: RemoteClient,
    start: int,
    end: int,
    scope: Scope = "ask-hn",
    *,
    collection: Collection = "discussions",
    min_comments: int = 0,
) -> list[ThreadCandidate]:
    """Return matches in [start, end), deduplicated and sorted oldest first.

    Unlike the daily whitelist, the historical corpus is strictly date bounded.
    The reading collection uses configured IDs and official HN story records.
    Broader backfills use configured title queries and the Algolia index.
    """
    if start >= end:
        raise ValueError("The start must be before the end of the discovery period")
    if min_comments < 0:
        raise ValueError("The minimum comment count cannot be negative")
    if collection == "reading":
        return curated_reading_threads(remote, start, end, min_comments)
    discovered: dict[int, ThreadCandidate] = {}

    def search_window(query: str, lower: int, upper: int) -> None:
        params = {
            "query": query,
            "tags": "ask_hn" if scope == "ask-hn" else "story",
            "restrictSearchableAttributes": "title",
            "numericFilters": f"created_at_i>={lower},created_at_i<{upper}",
            "hitsPerPage": 100,
            "page": 0,
        }
        if min_comments:
            params["numericFilters"] += f",num_comments>={min_comments}"
        url = f"{remote.settings.algolia_base_url}/search_by_date"
        first = remote.get(url, params)
        total, pages = int(first["nbHits"]), int(first["nbPages"])
        page_size = int(first.get("hitsPerPage", 100))
        # HN's index is commonly capped at 1,000 accessible results. Also respect
        # smaller server caps. HN can set exhaustiveNbHits=False even for a single
        # result, so that flag alone is not evidence of truncated pagination.
        capped = total > pages * page_size or total >= 1000
        if capped:
            if upper - lower <= 1:
                raise RuntimeError(f"Search pagination is capped within one second for {query!r}")
            middle = lower + (upper - lower) // 2
            logger.info(
                "discovery_window_split",
                extra={
                    "detail": {
                        "query": query,
                        "start": lower,
                        "end": upper,
                        "reported_hits": total,
                    }
                },
            )
            search_window(query, lower, middle)
            search_window(query, middle, upper)
            return
        fetched = 0
        for page in range(max(1, pages)):
            result = first if page == 0 else remote.get(url, dict(params, page=page))
            hits = result["hits"]
            fetched += len(hits)
            for hit in hits:
                created_at = int(hit["created_at_i"])
                title = hit.get("title") or ""
                comments = int(hit.get("num_comments") or 0)
                if comments < min_comments:
                    continue
                # Algolia prefix/stemming matches can include bookmarks/readme/bread.
                # Keep actual reading/book words while retaining broad discussion coverage.
                if lower <= created_at < upper and re.search(
                    r"\b(?:books?|read(?:ing)?|novels?|literature)\b",
                    title,
                    re.IGNORECASE,
                ):
                    item_id = int(hit["objectID"])
                    discovered[item_id] = ThreadCandidate(
                        id=item_id,
                        title=hit.get("title") or "Untitled thread",
                        created_at=created_at,
                        comments=comments,
                    )
        if fetched < total:
            raise RuntimeError(f"Incomplete search results for {query!r}: {fetched} of {total}")
        logger.info(
            "discovery_window_completed",
            extra={
                "detail": {
                    "query": query,
                    "start": lower,
                    "end": upper,
                    "hits": fetched,
                }
            },
        )

    queries = library_config()["backfill_queries"]
    for query in queries:
        search_window(query, start, end)
    candidates = sorted(
        discovered.values(), key=lambda candidate: (candidate.created_at, candidate.id)
    )
    logger.info(
        "backfill_discovery_completed",
        extra={
            "detail": {
                "threads": len(candidates),
                "scope": scope,
                "collection": collection,
                "min_comments": min_comments,
                "since": datetime.fromtimestamp(start, UTC).isoformat(),
                "until": datetime.fromtimestamp(end, UTC).isoformat(),
            }
        },
    )
    return candidates
