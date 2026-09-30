"""Small offline evaluation. Synthetic fixtures are regression checks, not field accuracy."""

import json
from pathlib import Path

from app.extraction import (
    classify_mention,
    extract_mentions,
    mention_context,
    normalize_title,
    plain_text,
)
from app.models import MentionSpan
from app.resolution import select_candidate


def evaluate() -> dict:
    root = Path(__file__).parent / "data"
    samples = json.loads((root / "golden.json").read_text()) + json.loads(
        (root / "golden_hn.json").read_text()
    )
    mentions_tp = mentions_fp = mentions_fn = tags_tp = tags_fp = tags_fn = strength_matches = 0
    matched = 0
    for sample in samples:
        actual = {
            normalize_title(s.title): s
            for s in extract_mentions(sample["html"], sample.get("known_titles"))
        }
        expected = {normalize_title(b["title"]): b for b in sample["books"]}
        mentions_tp += len(actual.keys() & expected.keys())
        mentions_fp += len(actual.keys() - expected.keys())
        mentions_fn += len(expected.keys() - actual.keys())
        for title in actual.keys() & expected.keys():
            classification = classify_mention(
                mention_context(plain_text(sample["html"]), actual[title].raw),
                title,
            )
            predicted_tags, expected_tags = set(classification.tags), set(expected[title]["tags"])
            tags_tp += len(predicted_tags & expected_tags)
            tags_fp += len(predicted_tags - expected_tags)
            tags_fn += len(expected_tags - predicted_tags)
            strength_matches += (
                abs(classification.recommendation_strength - expected[title]["strength"]) <= 0.1
            )
            matched += 1
    cases = json.loads((root / "resolution_golden.json").read_text())
    canonical_correct = 0
    for case in cases:
        selected, _, _ = select_candidate(MentionSpan(**case["mention"]), case["candidates"])
        canonical_correct += (selected["key"] if selected else None) == case["expected_work"]
    return {
        "corpus": "15 synthetic comments + 8 labeled HN comments; not representative accuracy",
        "source_linked_comments": sum("source_url" in sample for sample in samples),
        "comments": len(samples),
        "canonicalization_cases": len(cases),
        "mention_precision": mentions_tp / max(1, mentions_tp + mentions_fp),
        "mention_recall": mentions_tp / max(1, mentions_tp + mentions_fn),
        "canonicalization_accuracy": canonical_correct / max(1, len(cases)),
        "tag_precision": tags_tp / max(1, tags_tp + tags_fp),
        "tag_recall": tags_tp / max(1, tags_tp + tags_fn),
        "recommendation_strength_agreement": strength_matches / max(1, matched),
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2))
