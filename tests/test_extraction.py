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


def test_bibliographic_spans_preserve_original_text_and_full_author():
    spans = extract_mentions(
        "<p>Paideia: The Ideals of Greek Culture, vol 1-3, by Werner Jaeger</p>"
        "<p>The Federalist Papers, ed. by Kesler</p>"
        "<p>The Weird by Ann and Jeff VanderMeer</p>"
        '<p>Alec "The Years Have Pants" by Eddie Campbell</p>'
        "<p>2666 by Roberto Bolaño</p>"
    )
    actual = {span.title: span for span in spans}
    assert len(actual) == 5
    assert actual["Paideia: The Ideals of Greek Culture"].raw.endswith("vol 1-3")
    assert actual["The Federalist Papers"].raw == "The Federalist Papers, ed."
    assert actual["The Weird"].author == "Ann and Jeff VanderMeer"
    assert actual['Alec "The Years Have Pants"'].author == "Eddie Campbell"
    assert actual["2666"].confidence >= 0.7


def test_known_title_keeps_explicit_author_for_disambiguation():
    span = extract_mentions("Diary by Witold Gombrowicz", known_titles=["Diary"])[0]
    assert span.author == "Witold Gombrowicz"


def test_multiple_explicit_books_in_one_paragraph():
    spans = extract_mentions("2666 by Roberto Bolaño. Lab Girl by Hope Jahren.")
    assert {span.title for span in spans} == {"2666", "Lab Girl"}


def test_unbulleted_titles_require_bibliographic_context():
    assert extract_mentions("The Notebooks of Joseph Joubert") == []
    spans = extract_mentions(
        "<p>2666 by Roberto Bolaño</p><p>Diary by Witold Gombrowicz</p>"
        "<p>Lab Girl by Hope Jahren</p><p>The Notebooks of Joseph Joubert</p>"
        "<p>Loren Eiseley in the Library of America Edition</p><p>This changed my life</p>"
    )
    actual = {span.title: span for span in spans}
    assert len(actual) == 5
    assert actual["The Notebooks of Joseph Joubert"].confidence >= 0.7
    assert actual["Loren Eiseley in the Library of America Edition"].confidence < 0.7


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
