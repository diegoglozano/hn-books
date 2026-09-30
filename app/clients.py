import json
import logging
import sqlite3
import time
from typing import Any

import httpx

from app.config import Settings
from app.db import utc_now
from app.models import RunMetrics

logger = logging.getLogger(__name__)


class RemoteClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, params: dict | None = None) -> Any:
        for attempt in range(3):
            try:
                response = self.client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    raise
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("Retry limit reached")

    def hn_item(self, item_id: int) -> dict:
        item = self.get(f"{self.settings.hn_base_url}/item/{item_id}.json")
        if not isinstance(item, dict) or item.get("id") != item_id:
            raise ValueError(f"HN item {item_id} is missing or invalid")
        return item


class MetadataClient:
    def __init__(
        self,
        remote: RemoteClient,
        conn: sqlite3.Connection,
        metrics: RunMetrics,
        offline: bool = False,
    ) -> None:
        self.remote, self.conn, self.metrics, self.offline = remote, conn, metrics, offline
        self._last_request = 0.0

    def get(self, path: str, params: dict | None = None) -> dict:
        key = json.dumps([self.remote.settings.openlibrary_base_url, path, params], sort_keys=True)
        cached = self.conn.execute(
            "SELECT response_json FROM metadata_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
        if cached:
            return json.loads(cached[0])
        if self.offline:
            return {}
        delay = self.remote.settings.metadata_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        try:
            self.metrics.metadata_lookups += 1
            self._last_request = time.monotonic()
            result = self.remote.get(f"{self.remote.settings.openlibrary_base_url}{path}", params)
            if not isinstance(result, dict):
                raise ValueError("Unexpected Open Library response")
            self.conn.execute(
                "INSERT OR REPLACE INTO metadata_cache VALUES (?, ?, ?)",
                (key, json.dumps(result), utc_now()),
            )
            self.conn.commit()
            return result
        except (httpx.HTTPError, ValueError) as exc:
            self.metrics.failures += 1
            logger.warning("metadata_lookup_failed", extra={"detail": str(exc), "path": path})
            # Transient failures are deliberately not cached or treated as resolved.
            return {}

    def search(self, title: str, author: str | None = None) -> list[dict]:
        params: dict = {
            "title": title,
            "limit": 8,
            "fields": "key,title,author_name,first_publish_year,cover_i,isbn,subject",
        }
        if author:
            params["author"] = author
        return self.get("/search.json", params).get("docs", [])
