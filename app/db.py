import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings, library_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS hn_threads (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT, author TEXT,
    created_at INTEGER NOT NULL, score INTEGER NOT NULL DEFAULT 0,
    descendants INTEGER NOT NULL DEFAULT 0, raw_json TEXT NOT NULL, last_fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hn_comments (
    id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES hn_threads(id),
    parent_id INTEGER, author TEXT, text TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0, raw_json TEXT NOT NULL,
    last_fetched_at TEXT NOT NULL, processed_hash TEXT
);
CREATE INDEX IF NOT EXISTS comments_thread ON hn_comments(thread_id);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY, canonical_title TEXT NOT NULL, normalized_title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]', publication_year INTEGER, description TEXT,
    cover_url TEXT, openlibrary_id TEXT UNIQUE, google_books_id TEXT,
    isbn_10 TEXT NOT NULL DEFAULT '[]', isbn_13 TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0, recommendation_count INTEGER NOT NULL DEFAULT 0,
    independent_recommenders INTEGER NOT NULL DEFAULT 0, thread_count INTEGER NOT NULL DEFAULT 0,
    all_time_score REAL NOT NULL DEFAULT 0, recent_score REAL NOT NULL DEFAULT 0,
    score_details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS books_normalized ON books(normalized_title);
CREATE TABLE IF NOT EXISTS book_mentions (
    id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id),
    comment_id INTEGER NOT NULL REFERENCES hn_comments(id),
    thread_id INTEGER NOT NULL REFERENCES hn_threads(id),
    raw_mention TEXT NOT NULL, normalized_mention TEXT NOT NULL, context_text TEXT NOT NULL,
    extraction_confidence REAL NOT NULL, resolution_confidence REAL,
    recommendation_strength REAL NOT NULL, sentiment REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('resolved', 'unresolved')),
    candidates_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    UNIQUE(comment_id, normalized_mention)
);
CREATE INDEX IF NOT EXISTS mentions_book ON book_mentions(book_id);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT
);
CREATE TABLE IF NOT EXISTS book_mention_tags (
    mention_id INTEGER NOT NULL REFERENCES book_mentions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id), confidence REAL NOT NULL,
    PRIMARY KEY(mention_id, tag_id)
);
CREATE TABLE IF NOT EXISTS metadata_cache (
    cache_key TEXT PRIMARY KEY, response_json TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}', error TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, authors, description, tags, hn_context, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS thread_checkpoints (
    thread_id INTEGER PRIMARY KEY REFERENCES hn_threads(id),
    raw_complete INTEGER NOT NULL DEFAULT 0,
    processed_version TEXT, completed_at TEXT
);
PRAGMA user_version = 2;
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = path or get_settings().database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO tags(name, description) VALUES (?, ?)",
            [(name, name.replace("-", " ")) for name in library_config()["topics"]],
        )


def store_raw_item(conn: sqlite3.Connection, item: dict, thread_id: int) -> bool:
    """Persist even dead/deleted items; never perform extraction in this stage."""
    now = utc_now()
    raw = json.dumps(item, sort_keys=True)
    if item["id"] == thread_id:
        conn.execute(
            """INSERT INTO hn_threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, url=excluded.url,
            author=excluded.author, score=excluded.score, descendants=excluded.descendants,
            raw_json=excluded.raw_json, last_fetched_at=excluded.last_fetched_at""",
            (
                thread_id,
                item.get("title", "Deleted thread"),
                item.get("url"),
                item.get("by"),
                item.get("time", 0),
                item.get("score", 0),
                item.get("descendants", 0),
                raw,
                now,
            ),
        )
        return False
    is_new = conn.execute("SELECT 1 FROM hn_comments WHERE id=?", (item["id"],)).fetchone() is None
    conn.execute(
        """INSERT INTO hn_comments
        (id, thread_id, parent_id, author, text, created_at, raw_json, last_fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
        parent_id=excluded.parent_id, author=excluded.author, text=excluded.text,
        raw_json=excluded.raw_json, last_fetched_at=excluded.last_fetched_at""",
        (
            item["id"],
            thread_id,
            item.get("parent"),
            item.get("by"),
            "" if item.get("deleted") or item.get("dead") else item.get("text", ""),
            item.get("time", 0),
            raw,
            now,
        ),
    )
    return is_new
