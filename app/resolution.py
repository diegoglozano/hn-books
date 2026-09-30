import json
import re
import sqlite3
from typing import Any, TypedDict

from rapidfuzz.fuzz import ratio

from app.clients import MetadataClient
from app.db import utc_now
from app.extraction import normalize_author, normalize_title, title_similarity
from app.models import MentionSpan


class Candidate(TypedDict):
    key: str
    title: str
    authors: list[str]
    confidence: float
    doc: dict[str, Any]


def author_matches(author: str | None, authors: list[str]) -> bool:
    if not author:
        return True
    expected = normalize_author(author)
    return any(
        ratio(expected, normalize_author(value)) >= 87
        or (len(expected.split()) == 1 and expected == normalize_author(value).split()[-1])
        for value in authors
        if normalize_author(value)
    )


def work_key(value: str) -> str | None:
    match = re.fullmatch(r"(?:/works/)?(OL\d+W)", value)
    return f"/works/{match[1]}" if match else None


def resolve_book(
    conn: sqlite3.Connection, span: MentionSpan, metadata: MetadataClient
) -> tuple[int | None, float, list[dict]]:
    if span.confidence < 0.7:
        return None, 0, []
    local = []
    for row in conn.execute("SELECT * FROM books"):
        score = title_similarity(span.title, row["canonical_title"])
        if (span.work_id and row["openlibrary_id"] == span.work_id) or (
            not span.work_id
            and score >= 0.94
            and author_matches(span.author, json.loads(row["authors"]))
        ):
            local.append((row["id"], score))
    if len(local) == 1:
        return local[0][0], max(local[0][1], 0.94), []
    if len(local) > 1:
        return None, 0, [{"book_id": book_id, "confidence": score} for book_id, score in local]

    docs: list[dict[str, Any]]
    if span.work_id:
        work = metadata.get(f"{span.work_id}.json")
        if not work.get("title"):
            return None, 0, []
        authors = []
        for entry in work.get("authors", []):
            author_key = entry.get("author", {}).get("key", "")
            if re.fullmatch(r"/authors/OL\d+A", author_key):
                data = metadata.get(f"{author_key}.json")
                if data.get("name"):
                    authors.append(data["name"])
        docs = [{"key": span.work_id, "title": work["title"], "author_name": authors}]
    else:
        docs = metadata.search(span.title, span.author)
    selected, confidence, evidence = select_candidate(span, docs)
    if selected is None:
        return None, confidence, evidence
    key, doc = selected["key"], selected["doc"]
    existing = conn.execute("SELECT id FROM books WHERE openlibrary_id=?", (key,)).fetchone()
    if existing:
        return existing[0], selected["confidence"], evidence
    work = metadata.get(f"{key}.json")
    # No incomplete canonical record on upstream failure. Reprocessing can retry later.
    if not work.get("title"):
        return None, selected["confidence"], evidence
    description = work.get("description", "")
    if isinstance(description, dict):
        description = description.get("value", "")
    covers = work.get("covers", [])
    cover = doc.get("cover_i") or next((v for v in covers if v > 0), None)
    isbns = doc.get("isbn", [])
    now = utc_now()
    cursor = conn.execute(
        """INSERT INTO books(canonical_title, normalized_title, authors, publication_year,
        description, cover_url, openlibrary_id, isbn_10, isbn_13, metadata_json, created_at,
        updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            doc["title"],
            normalize_title(doc["title"]),
            json.dumps(selected["authors"]),
            doc.get("first_publish_year"),
            description,
            f"https://covers.openlibrary.org/b/id/{cover}-M.jpg" if cover else None,
            key,
            json.dumps([v for v in isbns if len(v) == 10]),
            json.dumps([v for v in isbns if len(v) == 13]),
            json.dumps({"source": "openlibrary", "search": doc, "work": work}),
            now,
            now,
        ),
    )
    assert cursor.lastrowid is not None
    conn.commit()
    return cursor.lastrowid, selected["confidence"], evidence


def select_candidate(
    span: MentionSpan,
    docs: list[dict[str, Any]],
) -> tuple[Candidate | None, float, list[dict]]:
    """Pure conservative matching stage, shared by ingestion and golden evaluation."""
    candidates: list[Candidate] = []
    for doc in docs:
        key = work_key(doc.get("key", ""))
        if not key:
            continue
        score = 1.0 if span.work_id == key else title_similarity(span.title, doc.get("title", ""))
        candidates.append(
            {
                "key": key,
                "title": doc.get("title", ""),
                "authors": doc.get("author_name", []),
                "confidence": round(score, 4),
                "doc": doc,
            }
        )
    candidates.sort(key=lambda item: item["confidence"], reverse=True)
    eligible = [
        c
        for c in candidates
        if c["confidence"] >= 0.94 and author_matches(span.author, c["authors"])
    ]
    ambiguous = (
        len({c["key"] for c in eligible if eligible[0]["confidence"] - c["confidence"] < 0.04}) > 1
        if eligible
        else False
    )
    evidence = [{k: v for k, v in c.items() if k != "doc"} for c in candidates]
    selected = eligible[0] if eligible and not ambiguous else None
    return selected, candidates[0]["confidence"] if candidates else 0, evidence
