"""Export resolved and unresolved extractions together for manual accuracy review."""

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from app.config import get_settings, library_config
from app.db import utc_now
from app.extraction import plain_text
from app.luna import comment_input


def content_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def thread_report(database_path: Path, thread_id: int) -> dict:
    if not database_path.is_file():
        raise ValueError(f"Database does not exist: {database_path}")
    with closing(sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        # One SQLite snapshot keeps comments, classifications and identities consistent
        # even if ingestion or staging publication commits while the export is reading.
        conn.execute("BEGIN")
        thread = conn.execute("SELECT * FROM hn_threads WHERE id=?", (thread_id,)).fetchone()
        if not thread:
            raise ValueError(f"Thread {thread_id} is not stored in this database")
        mentions = []
        for row in conn.execute(
            """SELECT m.*,b.canonical_title,b.authors,b.openlibrary_id,b.google_books_id,
            b.isbn_10,b.isbn_13,b.metadata_json,c.author AS commenter
            FROM book_mentions m LEFT JOIN books b ON b.id=m.book_id
            JOIN hn_comments c ON c.id=m.comment_id WHERE m.thread_id=?
            ORDER BY m.comment_id,m.id""",
            (thread_id,),
        ):
            item = dict(row)
            item["extraction"] = json.loads(item.pop("extraction_json"))
            item["candidates"] = json.loads(item.pop("candidates_json"))
            item["authors"] = json.loads(item["authors"] or "[]")
            item["isbn_10"] = json.loads(item["isbn_10"] or "[]")
            item["isbn_13"] = json.loads(item["isbn_13"] or "[]")
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            item["tags"] = {
                tag["name"]: tag["confidence"]
                for tag in conn.execute(
                    """SELECT t.name,mt.confidence FROM book_mention_tags mt
                    JOIN tags t ON t.id=mt.tag_id WHERE mt.mention_id=? ORDER BY t.name""",
                    (item["id"],),
                )
            }
            item["hn_url"] = f"https://news.ycombinator.com/item?id={item['comment_id']}"
            mentions.append(item)
        rows = conn.execute(
            "SELECT * FROM hn_comments WHERE thread_id=? ORDER BY id",
            (thread_id,),
        ).fetchall()
        by_comment = defaultdict(list)
        for mention in mentions:
            by_comment[mention["comment_id"]].append(mention["id"])
        comments = []
        for row in rows:
            raw = json.loads(row["raw_json"])
            comments.append(
                {
                    "comment_id": row["id"],
                    "parent_id": row["parent_id"],
                    "commenter": row["author"],
                    "created_at": row["created_at"],
                    "html": row["text"],
                    "text": plain_text(row["text"]),
                    "processed": row["processed_hash"] is not None,
                    "processed_hash": row["processed_hash"],
                    "deleted": bool(raw.get("deleted")),
                    "dead": bool(raw.get("dead")),
                    "input": comment_input(conn, row),
                    "mention_ids": by_comment[row["id"]],
                    "hn_url": f"https://news.ycombinator.com/item?id={row['id']}",
                }
            )
        checkpoint = conn.execute(
            "SELECT * FROM thread_checkpoints WHERE thread_id=?", (thread_id,)
        ).fetchone()
        runs = []
        for row in conn.execute(
            """SELECT id,started_at,finished_at,status,metrics_json FROM ingestion_runs
            WHERE command=? ORDER BY id DESC LIMIT 30""",
            (f"rebuild thread {thread_id}",),
        ):
            run = dict(row)
            run["metrics"] = json.loads(run.pop("metrics_json"))
            runs.append(run)
        report = {
            "schema_version": 2,
            "exported_at": utc_now(),
            "thread_id": thread_id,
            "hn_url": f"https://news.ycombinator.com/item?id={thread_id}",
            "title": thread["title"],
            "thread_text": plain_text(json.loads(thread["raw_json"]).get("text", "")),
            "source_digest": content_digest(
                {
                    "thread_id": thread_id,
                    "title": thread["title"],
                    "thread_html": json.loads(thread["raw_json"]).get("text", ""),
                    "comments": [
                        {
                            key: comment[key]
                            for key in (
                                "comment_id",
                                "parent_id",
                                "commenter",
                                "created_at",
                                "html",
                                "deleted",
                                "dead",
                            )
                        }
                        for comment in comments
                    ],
                }
            ),
            "processing_checkpoint": dict(checkpoint) if checkpoint else None,
            "recorded_rebuild_runs": runs,
            "comments": len(comments),
            "processed_comments": sum(bool(c["processed"]) for c in comments),
            "resolved_mentions": sum(m["status"] == "resolved" for m in mentions),
            "unresolved_mentions": sum(m["status"] == "unresolved" for m in mentions),
            "comment_records": comments,
            "mentions": mentions,
            "comments_without_mentions": [c for c in comments if not c["mention_ids"]],
            "taxonomy": sorted(library_config()["topics"]),
            "observed_versions": {
                "models": sorted(
                    {m["extraction"]["model"] for m in mentions if "model" in m["extraction"]}
                ),
                "prompts": sorted(
                    {
                        m["extraction"]["prompt_version"]
                        for m in mentions
                        if "prompt_version" in m["extraction"]
                    }
                ),
                "matching": sorted(
                    {
                        m["metadata"]["resolution_version"]
                        for m in mentions
                        if "resolution_version" in m["metadata"]
                    }
                ),
            },
        }
        report["snapshot_digest"] = content_digest(
            {key: value for key, value in report.items() if key != "exported_at"}
        )
        return report


def review_template(report: dict) -> dict:
    """Predictions are never silently treated as ground-truth labels."""
    return {
        "schema_version": 1,
        "thread_id": report["thread_id"],
        "source_digest": report["source_digest"],
        "prediction_snapshot_digest": report["snapshot_digest"],
        "reviewer": None,
        "comments": [
            {
                "comment_id": comment["comment_id"],
                "source_url": comment["hn_url"],
                "status": "pending",
                "case_types": [],
                "expected_mentions": None,
                "notes": "",
            }
            for comment in report["comment_records"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", type=int, default=49893157)
    parser.add_argument("--output", type=Path, help="Write JSON to this file; default: stdout")
    parser.add_argument(
        "--template", type=Path, help="Create a pending review checklist; never overwrite"
    )
    args = parser.parse_args()
    if args.template and (
        args.template.exists() or (args.output and args.template.resolve() == args.output.resolve())
    ):
        parser.error("The review template must be a new file, separate from the snapshot")
    snapshot = thread_report(get_settings().database_path, args.thread)
    report = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n")
    else:
        print(report)
    if args.template:
        args.template.parent.mkdir(parents=True, exist_ok=True)
        with args.template.open("x") as file:
            file.write(json.dumps(review_template(snapshot), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
