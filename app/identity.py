"""Source-linked bibliographic review decisions, independent of model extraction caches."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from app.extraction import normalize_author, normalize_title


@lru_cache
def identity_reviews() -> dict:
    return json.loads((Path(__file__).parent / "data/identity_reviews.json").read_text())


def identity_digest() -> str:
    return hashlib.sha256(json.dumps(identity_reviews(), sort_keys=True).encode()).hexdigest()


@lru_cache
def author_reviews() -> dict[str, dict]:
    lookup = {}
    for review in identity_reviews()["authors"]:
        for name in [review["name"], *review["aliases"]]:
            key = normalize_author(name)
            if key in lookup and lookup[key]["name"] != review["name"]:
                raise ValueError("Conflicting reviewed author aliases")
            lookup[key] = review
    return lookup


def author_identity(name: str) -> str:
    review = author_reviews().get(normalize_author(name))
    return normalize_author(review["name"] if review else name)


def author_display(name: str) -> str:
    review = author_reviews().get(normalize_author(name))
    return review.get("display_name", name.strip()) if review else name.strip()


def work_review(key: str) -> dict | None:
    return next((r for r in identity_reviews()["works"] if r["work_id"] == key), None)


def title_is_reviewed(title: str, review: dict) -> bool:
    return normalize_title(title) in {normalize_title(t) for t in review["titles"]}
