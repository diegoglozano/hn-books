import json
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.db import connect, initialize
from app.extraction import plain_text
from app.ranking import aggregate_tags, ensure_current_rankings
from app.review import review_template, thread_report


def serialize_book(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    book = dict(row)
    for key in ("authors", "isbn_10", "isbn_13", "metadata_json", "score_details"):
        book[key] = json.loads(book[key])
    book["tags"] = aggregate_tags(conn, book["id"])
    return book


def fts_query(value: str) -> str:
    stopwords = {"book", "books", "about", "on", "the", "a", "an", "for", "i", "want"}
    tokens = [token for token in re.findall(r"\w+", value.casefold()) if token not in stopwords]
    return " AND ".join(f'"{token}"*' for token in tokens[:20])


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialize(settings.database_path)
        with connect(settings.database_path) as conn:
            ensure_current_rankings(conn)
        yield

    app = FastAPI(title="HN Opinionated Library", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def require_admin(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> None:
        if not settings.admin_token:
            raise HTTPException(404, "Debug endpoints are disabled")
        if not credentials or not secrets.compare_digest(
            credentials.credentials, settings.admin_token
        ):
            raise HTTPException(401, "A valid admin bearer token is required")

    @app.get("/health")
    def health() -> dict:
        with connect(settings.database_path) as conn:
            conn.execute("SELECT 1 FROM books LIMIT 1")
        return {"status": "ok"}

    @app.get("/api/stats")
    def stats() -> dict:
        with connect(settings.database_path) as conn:
            return {
                "books": conn.execute(
                    "SELECT COUNT(*) FROM books WHERE mention_count>0"
                ).fetchone()[0],
                "threads": conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0],
                "comments": conn.execute("SELECT COUNT(*) FROM hn_comments").fetchone()[0],
                "recommenders": conn.execute(
                    """SELECT COUNT(DISTINCT c.author) FROM hn_comments c JOIN book_mentions m
                    ON m.comment_id=c.id WHERE m.book_id IS NOT NULL
                    AND m.recommendation_strength>=0.6 AND m.sentiment>0""",
                ).fetchone()[0],
                "last_updated": conn.execute(
                    "SELECT MAX(finished_at) FROM ingestion_runs WHERE status='completed'",
                ).fetchone()[0],
            }

    def list_books(q: str, tags: list[str], sort: str, page: int, page_size: int) -> dict:
        with connect(settings.database_path) as conn:
            where = ["b.mention_count>0"]
            params: list = []
            expression = fts_query(q)
            if q.strip() and not expression:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
            if expression:
                where.append("b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)")
                params.append(expression)
            for tag in tags:
                where.append("""EXISTS (SELECT 1 FROM book_mentions m
                    JOIN book_mention_tags mt ON mt.mention_id=m.id
                    JOIN tags t ON t.id=mt.tag_id WHERE m.book_id=b.id AND t.name=?)""")
                params.append(tag)
            clause = " AND ".join(where)
            order = {
                "all-time": "b.all_time_score DESC, b.independent_recommenders DESC",
                "recent": "b.recent_score DESC, b.independent_recommenders DESC",
                "mentions": "b.mention_count DESC, b.all_time_score DESC",
                "recommendations": "b.independent_recommenders DESC, b.all_time_score DESC",
            }[sort]
            total = conn.execute(f"SELECT COUNT(*) FROM books b WHERE {clause}", params).fetchone()[
                0
            ]
            rows = conn.execute(
                f"SELECT b.* FROM books b WHERE {clause} "
                f"ORDER BY {order}, b.canonical_title COLLATE NOCASE, b.id "
                "LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "items": [serialize_book(conn, row) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    @app.get("/api/books")
    @app.get("/api/search")
    def books(
        q: Annotated[str, Query(max_length=300)] = "",
        tag: Annotated[list[str] | None, Query()] = None,
        sort: Literal["all-time", "recent", "mentions", "recommendations"] = "all-time",
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 12,
    ) -> dict:
        return list_books(q, tag or [], sort, page, page_size)

    @app.get("/api/books/{book_id}")
    def book_detail(book_id: int) -> dict:
        with connect(settings.database_path) as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Book not found")
            book = serialize_book(conn, row)
            book["timeline"] = [
                dict(row)
                for row in conn.execute(
                    """SELECT strftime('%Y-%m', c.created_at, 'unixepoch') AS month,
                COUNT(*) AS mentions, COUNT(DISTINCT CASE WHEN m.recommendation_strength>=0.6
                AND m.sentiment>0 THEN c.author END) AS recommenders
                FROM book_mentions m JOIN hn_comments c ON c.id=m.comment_id
                WHERE m.book_id=? GROUP BY month ORDER BY month""",
                    (book_id,),
                )
            ]
            return book

    @app.get("/api/books/{book_id}/mentions")
    def book_mentions(
        book_id: int,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict:
        with connect(settings.database_path) as conn:
            if not conn.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                raise HTTPException(404, "Book not found")
            total = conn.execute(
                "SELECT COUNT(*) FROM book_mentions WHERE book_id=?",
                (book_id,),
            ).fetchone()[0]
            items = []
            for row in conn.execute(
                """SELECT m.*, c.author, c.created_at AS hn_created_at, t.title AS thread_title
                FROM book_mentions m JOIN hn_comments c ON c.id=m.comment_id
                JOIN hn_threads t ON t.id=m.thread_id WHERE m.book_id=?
                ORDER BY m.recommendation_strength DESC, c.created_at DESC LIMIT ? OFFSET ?""",
                (book_id, page_size, (page - 1) * page_size),
            ):
                mention = dict(row)
                mention["hn_url"] = f"https://news.ycombinator.com/item?id={mention['comment_id']}"
                mention["thread_url"] = (
                    f"https://news.ycombinator.com/item?id={mention['thread_id']}"
                )
                mention["tags"] = [
                    dict(tag)
                    for tag in conn.execute(
                        "SELECT t.name, mt.confidence FROM book_mention_tags mt "
                        "JOIN tags t ON t.id=mt.tag_id WHERE mt.mention_id=?",
                        (mention["id"],),
                    )
                ]
                mention["candidates"] = json.loads(mention.pop("candidates_json"))
                mention["extraction"] = json.loads(mention.pop("extraction_json"))
                items.append(mention)
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/tags")
    def tags() -> list[dict]:
        with connect(settings.database_path) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT t.name, t.description, COUNT(DISTINCT m.book_id) AS book_count
                FROM tags t LEFT JOIN book_mention_tags mt ON mt.tag_id=t.id
                LEFT JOIN book_mentions m ON m.id=mt.mention_id
                GROUP BY t.id ORDER BY book_count DESC, t.name""",
                )
            ]

    @app.get("/api/threads")
    def threads(page: Annotated[int, Query(ge=1)] = 1) -> dict:
        with connect(settings.database_path) as conn:
            rows = conn.execute(
                """SELECT t.id,t.title,t.author,t.created_at,t.score,t.descendants,
                t.last_fetched_at, COUNT(c.id) AS stored_comments FROM hn_threads t
                LEFT JOIN hn_comments c ON c.thread_id=t.id GROUP BY t.id
                ORDER BY t.created_at DESC LIMIT 20 OFFSET ?""",
                ((page - 1) * 20,),
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "page": page,
                "total": conn.execute("SELECT COUNT(*) FROM hn_threads").fetchone()[0],
            }

    @app.get("/api/threads/{thread_id}")
    def thread(
        thread_id: int,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict:
        with connect(settings.database_path) as conn:
            row = conn.execute("SELECT * FROM hn_threads WHERE id=?", (thread_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Thread not found")
            comments = [
                dict(c)
                for c in conn.execute(
                    "SELECT id,parent_id,author,text,created_at FROM hn_comments WHERE thread_id=? "
                    "ORDER BY created_at,id LIMIT ? OFFSET ?",
                    (thread_id, page_size, (page - 1) * page_size),
                )
            ]
            for comment in comments:
                comment["text"] = plain_text(comment["text"])
            return {
                "thread": dict(row),
                "comments": comments,
                "page": page,
                "total": conn.execute(
                    "SELECT COUNT(*) FROM hn_comments WHERE thread_id=?", (thread_id,)
                ).fetchone()[0],
            }

    @app.get("/api/admin/threads/{thread_id}/review", dependencies=[Depends(require_admin)])
    def review_thread(thread_id: int) -> dict:
        try:
            report = thread_report(settings.database_path, thread_id)
        except ValueError as error:
            raise HTTPException(404, "Thread not found") from error
        return {"snapshot": report, "review_template": review_template(report)}

    @app.get("/api/admin/unresolved-mentions", dependencies=[Depends(require_admin)])
    def unresolved(page: Annotated[int, Query(ge=1)] = 1) -> dict:
        with connect(settings.database_path) as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM book_mentions WHERE status='unresolved' "
                    "ORDER BY id LIMIT 50 OFFSET ?",
                    ((page - 1) * 50,),
                )
            ]
            for row in rows:
                row["candidates"] = json.loads(row.pop("candidates_json"))
                row["extraction"] = json.loads(row.pop("extraction_json"))
            return {
                "items": rows,
                "page": page,
                "total": conn.execute(
                    "SELECT COUNT(*) FROM book_mentions WHERE status='unresolved'"
                ).fetchone()[0],
            }

    @app.get("/api/admin/ingestion-status", dependencies=[Depends(require_admin)])
    def ingestion_status() -> list[dict]:
        with connect(settings.database_path) as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 30",
                )
            ]
            for row in rows:
                row["metrics"] = json.loads(row.pop("metrics_json"))
            return rows

    if settings.frontend_path.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_path, html=True), name="frontend")
    return app


app = create_app()
