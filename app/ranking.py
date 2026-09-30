import json
import math
import sqlite3
import time
from collections import defaultdict


def independence(mentions: list[dict]) -> dict[str, int]:
    positive = [m for m in mentions if m["recommendation_strength"] >= 0.6 and m["sentiment"] > 0]
    return {
        "users": len({m["author"] for m in positive if m.get("author")}),
        "threads": len({m["thread_id"] for m in positive}),
        "dates": len({int(m["created_at"]) // 86400 for m in positive}),
    }


def score_book(mentions: list[dict], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    counts = independence(mentions)
    positive = [m for m in mentions if m["recommendation_strength"] >= 0.6 and m["sentiment"] > 0]
    # One contribution per user/thread/day. Repeated replies cannot inflate frequency.
    contexts: dict[tuple, dict] = {}
    for mention in positive:
        key = (
            mention.get("author") or "unknown",
            mention["thread_id"],
            int(mention["created_at"]) // 86400,
        )
        if (
            key not in contexts
            or mention["recommendation_strength"] > contexts[key]["recommendation_strength"]
        ):
            contexts[key] = mention
    contributions = []
    recent = []
    for mention in contexts.values():
        quality = 1 + min(math.log1p(max(0, mention.get("thread_score", 0))) / 20, 0.3)
        explanation = min(len(mention["context_text"].split()) / 80, 1) * 0.2
        contribution = mention["recommendation_strength"] * (quality + explanation)
        age = max(0, now - mention["created_at"]) / 86400
        contributions.append(contribution)
        recent.append(contribution * 0.5 ** (age / 365))
    diversity = 1 + 0.15 * math.log1p(counts["threads"]) + 0.1 * math.log1p(counts["dates"])
    # Authors are counted separately for inspection; distinct contexts already reward independence.
    return {
        "mention_count": len(mentions),
        "recommendation_count": len(positive),
        "independent_recommenders": counts["users"],
        "thread_count": len({m["thread_id"] for m in mentions}),
        "all_time_score": round(sum(contributions) * diversity, 4),
        "recent_score": round(sum(recent) * diversity, 4),
        "details": {
            "formula_version": 1,
            "independent_contexts": len(contexts),
            "positive_users": counts["users"],
            "positive_threads": counts["threads"],
            "positive_dates": counts["dates"],
            "diversity_multiplier": diversity,
            "recent_half_life_days": 365,
        },
    }


def aggregate_tags(conn: sqlite3.Connection, book_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT t.name, COUNT(*) AS mention_count, AVG(mt.confidence) AS confidence
        FROM book_mention_tags mt JOIN tags t ON t.id=mt.tag_id
        JOIN book_mentions m ON m.id=mt.mention_id WHERE m.book_id=?
        GROUP BY t.id ORDER BY mention_count DESC, t.name""",
            (book_id,),
        )
    ]


def compute_book_scores(conn: sqlite3.Connection) -> None:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute(
        """SELECT m.*, c.author, c.created_at AS hn_created_at, t.score AS thread_score
        FROM book_mentions m JOIN hn_comments c ON c.id=m.comment_id
        JOIN hn_threads t ON t.id=m.thread_id WHERE m.book_id IS NOT NULL""",
    ):
        mention = dict(row)
        mention["created_at"] = row["hn_created_at"]
        grouped[row["book_id"]].append(mention)
    for book in conn.execute("SELECT id FROM books").fetchall():
        scores = score_book(grouped[book["id"]])
        conn.execute(
            """UPDATE books SET mention_count=?, recommendation_count=?,
            independent_recommenders=?, thread_count=?, all_time_score=?, recent_score=?,
            score_details=? WHERE id=?""",
            (
                *[
                    scores[k]
                    for k in (
                        "mention_count",
                        "recommendation_count",
                        "independent_recommenders",
                        "thread_count",
                        "all_time_score",
                        "recent_score",
                    )
                ],
                json.dumps(scores["details"]),
                book["id"],
            ),
        )


def update_search_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM books_fts")
    for book in conn.execute("SELECT * FROM books WHERE mention_count > 0").fetchall():
        tags = " ".join(tag["name"].replace("-", " ") for tag in aggregate_tags(conn, book["id"]))
        contexts = " ".join(
            row[0]
            for row in conn.execute(
                "SELECT context_text FROM book_mentions WHERE book_id=?",
                (book["id"],),
            )
        )
        conn.execute(
            "INSERT INTO books_fts(rowid,title,authors,description,tags,hn_context) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                book["id"],
                book["canonical_title"],
                " ".join(json.loads(book["authors"])),
                book["description"] or "",
                tags,
                contexts,
            ),
        )
