"""Date-bounded title discovery with adaptive splitting around search pagination limits."""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.clients import RemoteClient
from app.config import library_config

logger = logging.getLogger(__name__)
Scope = Literal["ask-hn", "stories"]


@dataclass(frozen=True)
class ThreadCandidate:
    id: int
    title: str
    created_at: int
    comments: int


def discover_reading_threads(
    remote: RemoteClient,
    start: int,
    end: int,
    scope: Scope = "ask-hn",
) -> list[ThreadCandidate]:
    """Return matches in [start, end), deduplicated and sorted oldest first.

    Unlike the daily whitelist, the historical corpus is strictly date bounded.
    Search coverage is limited to configured title queries and the Algolia index.
    """
    if start >= end:
        raise ValueError("The start must be before the end of the discovery period")
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
                        comments=int(hit.get("num_comments") or 0),
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

    for query in library_config()["backfill_queries"]:
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
                "since": datetime.fromtimestamp(start, UTC).isoformat(),
                "until": datetime.fromtimestamp(end, UTC).isoformat(),
            }
        },
    )
    return candidates
