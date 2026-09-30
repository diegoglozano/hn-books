import json
from pathlib import Path

import pytest

from app.extraction import (
    classify_mention,
    extract_mentions,
    mention_context,
    normalize_author,
    normalize_title,
    plain_text,
    title_similarity,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Designing Data–Intensive Applications", "designing data intensive applications"),
        ("  GÖDEL, Escher &amp; Bach ", "godel escher bach"),
        ("The   Rust—Programming Language", "the rust programming language"),
    ],
)
def test_normalization(value, expected):
    assert normalize_title(value) == expected


def test_author_normalization():
    assert normalize_author("Kleppmann, Martin") == normalize_author("Martin Kleppmann")
    assert normalize_author("J. R. R. Tolkien") == "j r r tolkien"


def test_fuzzy_matching_does_not_match_arbitrary_subsets():
    assert (
        title_similarity(
            "Designing Data Intensive Applications", "Designing Data-Intensive Applications"
        )
        == 1
    )
    assert title_similarity("Dune", "Dune: A Novel") == 1
    assert title_similarity("Dune", "Children of Dune") < 0.94
    assert title_similarity("", "Dune") == 0


def test_safe_plain_text_and_links():
    assert plain_text("<p>A &amp; B</p><p><i>Book</i></p>") == "A & B\n\nBook"
    spans = extract_mentions('<a href="https://openlibrary.org/works/OL123W">A Book</a>')
    assert spans[0].work_id == "/works/OL123W"


DATA = Path(__file__).parent.parent / "app/data"
GOLDEN = json.loads((DATA / "golden.json").read_text()) + json.loads(
    (DATA / "golden_hn.json").read_text()
)


@pytest.mark.parametrize("sample", GOLDEN)
def test_golden_extraction_and_classification(sample):
    spans = extract_mentions(sample["html"])
    actual = {normalize_title(span.title): span for span in spans}
    expected = {normalize_title(book["title"]): book for book in sample["books"]}
    assert actual.keys() == expected.keys()
    for title, book in expected.items():
        classification = classify_mention(
            mention_context(plain_text(sample["html"]), actual[title].raw),
            title,
        )
        assert classification.sentiment == book["sentiment"]
        assert classification.recommendation_strength == book["strength"]
        assert set(book["tags"]) <= classification.tags.keys()
