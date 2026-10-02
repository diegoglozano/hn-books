import json
import re
import sqlite3
from typing import Any, TypedDict

from rapidfuzz.fuzz import ratio

from app.clients import MetadataClient
from app.db import utc_now
from app.extraction import normalize_author, normalize_title, title_similarity
from app.models import MentionSpan

RESOLUTION_VERSION = 2


def unique_authors(authors: list[str]) -> list[str]:
    """Collapse duplicate catalog names while preserving their display spelling."""
    seen = set()
    result = []
    for author in authors:
        key = normalize_author(author)
        if key and key not in seen:
            seen.add(key)
            result.append(author.strip())
    return result


def author_parts(author: str) -> list[str]:
    parts = re.split(r"\s+(?:and|&)\s+", author)
    if len(parts) > 1:
        surname = parts[-1].split()[-1]
        return [f"{part} {surname}" if len(part.split()) == 1 else part for part in parts]
    return parts


class Candidate(TypedDict):
    key: str
    title: str
    authors: list[str]
    confidence: float
    doc: dict[str, Any]


def author_matches(author: str | None, authors: list[str]) -> bool:
    if not author:
        return True
    parts = author_parts(author)
    if len(parts) > 1:
        # Shared surnames: "Ann and Jeff VanderMeer" names two distinct authors.
        return all(author_matches(part, authors) for part in parts)
    expected = normalize_author(author)
    return any(
        ratio(expected, normalize_author(value)) >= 87
        or (
            len(author.split()) == 1
            and normalize_author(value).split()[-len(expected.split()) :] == expected.split()
        )
        for value in authors
        if normalize_author(value)
    )


def work_key(value: str) -> str | None:
    match = re.fullmatch(r"(?:/works/)?(OL\d+W)", value)
    return f"/works/{match[1]}" if match else None


def complete_author_match(author: str, authors: list[str]) -> bool:
    expected = author_parts(author)
    return len(unique_authors(authors)) == len(expected) and all(
        author_matches(part, authors) for part in expected
    )


def resolve_book(
    conn: sqlite3.Connection, span: MentionSpan, metadata: MetadataClient
) -> tuple[int | None, float, list[dict]]:
    if span.confidence < 0.7 or (span.require_author and not span.author and not span.work_id):
        return None, 0, []
    local = []
    for row in conn.execute("SELECT * FROM books"):
        score = title_similarity(span.title, row["canonical_title"])
        if (span.work_id and row["openlibrary_id"] == span.work_id) or (
            not span.work_id
            and score >= 0.94
            and author_matches(span.author, json.loads(row["authors"]))
            and normalize_title(span.title) == row["normalized_title"]
            and (not span.author or complete_author_match(span.author, json.loads(row["authors"])))
        ):
            # Reprocess matching changes against cached source metadata, not stale
            # canonical records. Luna caches are independent of this version.
            if json.loads(row["metadata_json"]).get("resolution_version") != RESOLUTION_VERSION:
                continue
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
        if not docs and span.author:
            # Search can reject abbreviated/coordinated author names. Candidate validation
            # still requires the extracted authors to agree; the title threshold is unchanged.
            docs = metadata.search(span.title)
    selected, confidence, evidence = select_candidate(span, docs)
    if selected is None:
        return None, confidence, evidence
    key, doc = selected["key"], selected["doc"]
    existing = conn.execute("SELECT id FROM books WHERE openlibrary_id=?", (key,)).fetchone()
    work = metadata.get(f"{key}.json")
    # No incomplete canonical record on upstream failure. Reprocessing can retry later.
    if not work.get("title"):
        return None, selected["confidence"], evidence
    if not span.work_id and title_similarity(span.title, work["title"]) < 0.94:
        return None, selected["confidence"], evidence
    work_authors = []
    for entry in work.get("authors", []):
        author_key = entry.get("author", {}).get("key", "")
        if re.fullmatch(r"/authors/OL\d+A", author_key):
            author_data = metadata.get(f"{author_key}.json")
            if author_data.get("name"):
                work_authors.append(author_data["name"])
    if span.author and work.get("authors") and not work_authors:
        return None, selected["confidence"], evidence
    if span.author and work_authors and not author_matches(span.author, work_authors):
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
        updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(openlibrary_id) DO UPDATE SET
        canonical_title=excluded.canonical_title, normalized_title=excluded.normalized_title,
        authors=excluded.authors, publication_year=excluded.publication_year,
        description=excluded.description, cover_url=excluded.cover_url,
        isbn_10=excluded.isbn_10, isbn_13=excluded.isbn_13,
        metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
        (
            doc["title"],
            normalize_title(doc["title"]),
            json.dumps(unique_authors(work_authors or selected["authors"])),
            doc.get("first_publish_year"),
            description,
            f"https://covers.openlibrary.org/b/id/{cover}-M.jpg" if cover else None,
            key,
            json.dumps([v for v in isbns if len(v) == 10]),
            json.dumps([v for v in isbns if len(v) == 13]),
            json.dumps(
                {
                    "source": "openlibrary",
                    "search": doc,
                    "work": work,
                    "resolution_version": RESOLUTION_VERSION,
                }
            ),
            now,
            now,
        ),
    )
    book_id = existing[0] if existing else cursor.lastrowid
    assert book_id is not None
    conn.commit()
    return book_id, selected["confidence"], evidence


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
                "authors": unique_authors(doc.get("author_name", [])),
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
    # A title with a colon can identify a sequel or adaptation, not just a subtitle.
    # Prefer the full title whenever the catalog provides an exact title/author match.
    exact = [c for c in eligible if normalize_title(c["title"]) == normalize_title(span.title)]
    if exact:
        eligible = exact
    if span.author:
        complete = [c for c in eligible if complete_author_match(span.author, c["authors"])]
        if complete:
            eligible = complete
    # Distinct Open Library work IDs often duplicate one title by the same author.
    # Only coalesce exact full-title matches with the same complete author identities;
    # surname-only ambiguity and different collaborators still remain unresolved.
    identities = {
        (
            normalize_title(c["title"]),
            tuple(sorted(normalize_author(a) for a in c["authors"])),
            any(
                re.search(r"\b(?:comics?|graphic novels?|manga)\b", subject, re.I)
                for subject in c["doc"].get("subject", [])
            ),
        )
        for c in eligible
    }
    catalog_duplicates = bool(
        span.author and exact and len(identities) == 1 and eligible[0]["authors"]
    )
    if catalog_duplicates:
        eligible.sort(
            key=lambda c: (
                -len(set(c["doc"].get("isbn", []))),
                c["doc"].get("first_publish_year") or 9999,
                int(c["key"][9:-1]),
            )
        )
    ambiguous = (
        len({c["key"] for c in eligible if eligible[0]["confidence"] - c["confidence"] < 0.04}) > 1
        if eligible
        else False
    )
    evidence = [{k: v for k, v in c.items() if k != "doc"} for c in candidates]
    selected = eligible[0] if eligible and (not ambiguous or catalog_duplicates) else None
    return selected, candidates[0]["confidence"] if candidates else 0, evidence
