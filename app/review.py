"""Export resolved and unresolved extractions together for manual accuracy review."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.config import get_settings
from app.extraction import plain_text


def thread_report(database_path: Path, thread_id: int) -> dict:
    if not database_path.is_file():
        raise ValueError(f"Database does not exist: {database_path}")
    with closing(sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        thread = conn.execute("SELECT title FROM hn_threads WHERE id=?", (thread_id,)).fetchone()
        if not thread:
            raise ValueError(f"Thread {thread_id} is not stored in this database")
        mentions = []
        for row in conn.execute(
            """SELECT m.*,b.canonical_title,b.authors,c.author AS commenter
            FROM book_mentions m LEFT JOIN books b ON b.id=m.book_id
            JOIN hn_comments c ON c.id=m.comment_id WHERE m.thread_id=?
            ORDER BY m.comment_id,m.id""",
            (thread_id,),
        ):
            item = dict(row)
            item["extraction"] = json.loads(item.pop("extraction_json"))
            item["candidates"] = json.loads(item.pop("candidates_json"))
            item["authors"] = json.loads(item["authors"] or "[]")
            item["hn_url"] = f"https://news.ycombinator.com/item?id={item['comment_id']}"
            mentions.append(item)
        counts = conn.execute(
            "SELECT COUNT(*),SUM(processed_hash IS NOT NULL) FROM hn_comments WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        without_mentions = [
            {
                "comment_id": row["id"],
                "commenter": row["author"],
                "text": plain_text(row["text"]),
                "processed": row["processed_hash"] is not None,
                "hn_url": f"https://news.ycombinator.com/item?id={row['id']}",
            }
            for row in conn.execute(
                """SELECT c.* FROM hn_comments c LEFT JOIN book_mentions m ON m.comment_id=c.id
                WHERE c.thread_id=? AND m.id IS NULL ORDER BY c.id""",
                (thread_id,),
            )
        ]
        return {
            "thread_id": thread_id,
            "title": thread["title"],
            "comments": counts[0],
            "processed_comments": counts[1] or 0,
            "resolved_mentions": sum(m["status"] == "resolved" for m in mentions),
            "unresolved_mentions": sum(m["status"] == "unresolved" for m in mentions),
            "mentions": mentions,
            "comments_without_mentions": without_mentions,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", type=int, default=49893157)
    parser.add_argument("--output", type=Path, help="Write JSON to this file; default: stdout")
    args = parser.parse_args()
    report = json.dumps(
        thread_report(get_settings().database_path, args.thread), ensure_ascii=False, indent=2
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
