import json
import math
import sqlite3
import time
from collections import defaultdict

FORMULA_VERSION = 2
RECENT_HALF_LIFE_DAYS = 365
NEGATIVE_VOTE_WEIGHT = 0.5
ANONYMOUS_VOTE_WEIGHT = 0.25


def is_positive(mention: dict) -> bool:
    return mention["recommendation_strength"] >= 0.6 and mention["sentiment"] > 0


def reader_opinions(mentions: list[dict]) -> tuple[list[dict], int]:
    """One latest non-neutral opinion per reader; unidentified readers share one vote."""
    by_reader = defaultdict(list)
    for mention in mentions:
        if is_positive(mention) or mention["sentiment"] < 0:
            key = ("reader", mention["author"]) if mention.get("author") else ("anonymous",)
            by_reader[key].append(mention)
    opinions = []
    conflicts = 0
    for history in by_reader.values():
        newest = max(m["created_at"] for m in history)
        latest = [m for m in history if m["created_at"] == newest]
        if len({is_positive(m) for m in latest}) > 1:
            # Timestamp ties cannot establish which conflicting opinion came last.
            conflicts += 1
            continue
        opinions.append(
            max(
                latest,
                key=lambda m: (
                    m["recommendation_strength"],
                    m["sentiment"],
                    -m["thread_id"],
                    -m.get("comment_id", 0),
                ),
            )
        )
    return opinions, conflicts


def positive_independence(positive: list[dict]) -> dict[str, int]:
    return {
        "users": len({m["author"] for m in positive if m.get("author")}),
        "threads": len({m["thread_id"] for m in positive}),
        "dates": len({int(m["created_at"]) // 86400 for m in positive}),
    }


def independence(mentions: list[dict]) -> dict[str, int]:
    opinions, _ = reader_opinions(mentions)
    return positive_independence([m for m in opinions if is_positive(m)])


def score_book(mentions: list[dict], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    opinions, conflicts = reader_opinions(mentions)
    positive = [m for m in opinions if is_positive(m)]
    negative = [m for m in opinions if m["sentiment"] < 0]
    counts = positive_independence(positive)
    support, opposition, recent_support, recent_opposition = [], [], [], []
    positive_voter_weight = 0.0
    for mention in opinions:
        weight = 1.0 if mention.get("author") else ANONYMOUS_VOTE_WEIGHT
        age = max(0, now - mention["created_at"]) / 86400
        decay = 0.5 ** (age / RECENT_HALF_LIFE_DAYS)
        if is_positive(mention):
            contribution = weight * mention["recommendation_strength"]
            positive_voter_weight += weight
            support.append(contribution)
            recent_support.append(contribution * decay)
        else:
            contribution = weight * NEGATIVE_VOTE_WEIGHT
            opposition.append(contribution)
            recent_opposition.append(contribution * decay)
    positive_weight, negative_weight = math.fsum(support), math.fsum(opposition)
    net_support = max(0.0, positive_weight - negative_weight)
    # A modest, bounded breadth bonus; a prolific reader cannot create additional votes.
    diversity = (
        1
        + min(0.1 * math.log1p(max(0, counts["threads"] - 1)), 0.15)
        + min(0.05 * math.log1p(max(0, counts["dates"] - 1)), 0.1)
    )
    # Reduce the influence of isolated endorsements. This is a heuristic, not a probability.
    support_multiplier = positive_voter_weight / (positive_voter_weight + 2)
    multiplier = support_multiplier * diversity
    return {
        "mention_count": len(mentions),
        "recommendation_count": sum(is_positive(m) for m in mentions),
        "independent_recommenders": counts["users"],
        "thread_count": len({m["thread_id"] for m in mentions}),
        "all_time_score": round(net_support * multiplier, 4),
        "recent_score": round(
            max(0.0, math.fsum(recent_support) - math.fsum(recent_opposition)) * multiplier,
            4,
        ),
        "details": {
            "formula_version": FORMULA_VERSION,
            "independent_contexts": len(positive),
            "positive_users": counts["users"],
            "negative_users": len({m["author"] for m in negative if m.get("author")}),
            "positive_threads": counts["threads"],
            "positive_dates": counts["dates"],
            "conflicting_readers": conflicts,
            "anonymous_opinions": sum(not m.get("author") for m in opinions),
            "anonymous_vote_weight": ANONYMOUS_VOTE_WEIGHT,
            "negative_vote_weight": NEGATIVE_VOTE_WEIGHT,
            "positive_weight": positive_weight,
            "negative_weight": negative_weight,
            "net_support": net_support,
            "support_multiplier": support_multiplier,
            "diversity_multiplier": diversity,
            "recent_half_life_days": RECENT_HALF_LIFE_DAYS,
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
    now = time.time()
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute(
        """SELECT m.*, c.author, c.created_at AS hn_created_at
        FROM book_mentions m JOIN hn_comments c ON c.id=m.comment_id
        WHERE m.book_id IS NOT NULL""",
    ):
        mention = dict(row)
        mention["created_at"] = row["hn_created_at"]
        grouped[row["book_id"]].append(mention)
    for book in conn.execute("SELECT id FROM books").fetchall():
        scores = score_book(grouped[book["id"]], now=now)
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


def ensure_current_rankings(conn: sqlite3.Connection) -> bool:
    """Upgrade derived scores on deployment, without extraction or network requests."""
    stale = (
        "SELECT 1 FROM books "
        "WHERE COALESCE(json_extract(score_details, '$.formula_version'), 0) != ? LIMIT 1"
    )
    if not conn.execute(stale, (FORMULA_VERSION,)).fetchone():
        return False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    # Another web worker may have completed the upgrade before this writer acquired the lock.
    if not conn.execute(stale, (FORMULA_VERSION,)).fetchone():
        return False
    compute_book_scores(conn)
    return True


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
