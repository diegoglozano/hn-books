import json
import re
import sqlite3
from typing import Any, TypedDict

from rapidfuzz.fuzz import ratio

from app.clients import MetadataClient
from app.db import utc_now
from app.extraction import normalize_author, normalize_title, title_similarity
from app.identity import (
    author_display,
    author_identity,
    identity_digest,
    identity_reviews,
    title_is_reviewed,
    work_review,
)
from app.models import MentionSpan

RESOLUTION_VERSION = 3


def unique_authors(authors: list[str]) -> list[str]:
    """Collapse duplicate catalog names while preserving their display spelling."""
    seen = set()
    result = []
    for author in authors:
        key = author_identity(author)
        if key and key not in seen:
            seen.add(key)
            result.append(author_display(author))
    return result


def author_parts(author: str) -> list[str]:
    comma_parts = re.split(r",\s*(?:and\s+)?", author)
    if len(comma_parts) > 1 and all(len(part.split()) >= 2 for part in comma_parts):
        return [name for part in comma_parts for name in author_parts(part)]
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
    expected = author_identity(author)
    return any(
        ratio(expected, author_identity(value)) >= 87
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


def preferred_review(span: MentionSpan) -> dict | None:
    if span.work_id or not span.author:
        return None
    return next(
        (
            r
            for r in identity_reviews()["works"]
            if title_is_reviewed(span.title, r) and complete_author_match(span.author, r["authors"])
        ),
        None,
    )


def implicit_exclusion(span: MentionSpan, key: str) -> dict | None:
    if span.work_id == key or not span.author:
        return None
    return next(
        (
            r
            for r in identity_reviews()["implicit_exclusions"]
            if r["work_id"] == key and complete_author_match(span.author, r["authors"])
        ),
        None,
    )


def reviewed_title_score(span: MentionSpan, key: str, title: str) -> float:
    review = work_review(key)
    if (
        review
        and span.author
        and complete_author_match(span.author, review["authors"])
        and title_is_reviewed(span.title, review)
        and title_is_reviewed(title, review)
    ):
        return 1.0
    return title_similarity(span.title, title)


def resolve_book(
    conn: sqlite3.Connection, span: MentionSpan, metadata: MetadataClient
) -> tuple[int | None, float, list[dict]]:
    if span.confidence < 0.7 or (span.require_author and not span.author and not span.work_id):
        return None, 0, []
    local = []
    for row in conn.execute("SELECT * FROM books"):
        if implicit_exclusion(span, row["openlibrary_id"]):
            continue
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
            stored = json.loads(row["metadata_json"])
            if (
                stored.get("resolution_version") != RESOLUTION_VERSION
                or stored.get("identity_review_digest") != identity_digest()
            ):
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
        preferred = preferred_review(span)
        if preferred and not any(d.get("key") == preferred["work_id"] for d in docs):
            # A reviewed subtitle may not be indexed by the provider. Reuse the normal
            # canonical-title search cache; author/work verification still follows.
            docs = [*docs, *metadata.search(preferred["title"], preferred["authors"][0])]
    selected, confidence, evidence = select_candidate(span, docs)
    if selected is None:
        return None, confidence, evidence
    key, doc = selected["key"], selected["doc"]
    existing = conn.execute("SELECT id FROM books WHERE openlibrary_id=?", (key,)).fetchone()
    work = metadata.get(f"{key}.json")
    # No incomplete canonical record on upstream failure. Reprocessing can retry later.
    if not work.get("title"):
        return None, selected["confidence"], evidence
    if not span.work_id and reviewed_title_score(span, key, work["title"]) < 0.94:
        return None, selected["confidence"], evidence
    review = work_review(key)
    if review and not title_is_reviewed(work["title"], review):
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
    canonical_authors = review["authors"] if review else work_authors or selected["authors"]
    if span.author and canonical_authors and not author_matches(span.author, canonical_authors):
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
            review["title"] if review else doc["title"],
            normalize_title(review["title"] if review else doc["title"]),
            json.dumps(unique_authors(canonical_authors)),
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
                    "identity_review_digest": identity_digest(),
                    "identity_review": review,
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
        score = (
            1.0 if span.work_id == key else reviewed_title_score(span, key, doc.get("title", ""))
        )
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
        if c["confidence"] >= 0.94
        and author_matches(span.author, c["authors"])
        and not implicit_exclusion(span, c["key"])
    ]
    preferred = preferred_review(span)
    reviewed = [c for c in eligible if preferred and c["key"] == preferred["work_id"]]
    if reviewed:
        eligible = reviewed
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
            tuple(sorted(author_identity(a) for a in c["authors"])),
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
    evidence = []
    for candidate in candidates:
        item = {k: v for k, v in candidate.items() if k != "doc"}
        exclusion = implicit_exclusion(span, candidate["key"])
        review = work_review(candidate["key"])
        if exclusion or review:
            item["identity_review"] = exclusion or review
            item["excluded_by_review"] = exclusion is not None
        evidence.append(item)
    selected = eligible[0] if eligible and (not ambiguous or catalog_duplicates) else None
    return selected, candidates[0]["confidence"] if candidates else 0, evidence
