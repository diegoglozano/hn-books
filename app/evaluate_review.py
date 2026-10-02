"""Evaluate saved extraction outputs against explicit, source-linked review labels offline."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.extraction import (
    classify_mention,
    extract_mentions,
    mention_context,
    normalize_author,
    normalize_title,
)
from app.luna import LunaMention
from app.review import content_digest


class ExpectedMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw: str = Field(min_length=1)
    title: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    authors: list[str]
    work_id: str | None = Field(pattern=r"^/works/OL\d+W$")
    accepted_work_ids: list[str] = Field(default_factory=list)
    author_source: Literal["stated", "context", "inferred", "unknown"]
    sentiment: Literal["recommended", "positive", "neutral", "negative"]
    strength: float = Field(ge=0, le=1)
    tags: list[str]


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(counts: Counter, *, canonical: bool) -> dict:
    result: dict[str, int | float | None] = dict(counts)
    for metric, numerator, denominator in (
        ("mention_precision", "tp", counts["tp"] + counts["fp"]),
        ("mention_recall", "tp", counts["tp"] + counts["fn"]),
        ("tag_precision", "tags_tp", counts["tags_tp"] + counts["tags_fp"]),
        ("tag_recall", "tags_tp", counts["tags_tp"] + counts["tags_fn"]),
        ("sentiment_agreement", "sentiment_correct", counts["tp"]),
        ("recommendation_strength_agreement", "strength_correct", counts["tp"]),
        ("grounded_evidence_rate", "grounded", counts["predicted"]),
    ):
        result[metric] = ratio(counts[numerator], denominator)
    if canonical:
        result["canonicalization_accuracy"] = ratio(
            counts["identity_correct"], counts["known_works"]
        )
        result["author_accuracy"] = ratio(counts["authors_correct"], counts["known_works"])
        result["author_provenance_agreement"] = ratio(counts["provenance_correct"], counts["tp"])
        result["unresolved_rate"] = ratio(counts["unresolved"], counts["predicted"])
        result["abstention_accuracy"] = ratio(
            counts["abstentions_correct"], counts["ambiguous_works"]
        )
    return result


def heuristic_predictions(comment: dict) -> list[dict]:
    predictions = []
    for span in extract_mentions(comment["html"]):
        classification = classify_mention(mention_context(comment["text"], span.raw), span.title)
        sentiment = (
            "negative"
            if classification.sentiment < 0
            else "neutral"
            if classification.sentiment == 0
            else "recommended"
            if classification.recommendation_strength >= 0.9
            else "positive"
        )
        predictions.append(
            {
                "title": span.title,
                "raw_mention": span.raw,
                "recommendation_strength": classification.recommendation_strength,
                "tags": classification.tags,
                "extraction": {"sentiment": sentiment},
            }
        )
    return predictions


def original_luna_predictions(comment: dict, stored: list[dict]) -> list[dict]:
    correction = comment.get("comment_review")
    if not correction:
        return stored
    predictions = []
    for fields in correction["original_result"]["mentions"]:
        mention = LunaMention.model_validate(
            {key: value for key, value in fields.items() if key in LunaMention.model_fields}
        )
        classification = mention.classification()
        predictions.append(
            {
                "title": mention.title,
                "raw_mention": mention.raw,
                "recommendation_strength": classification.recommendation_strength,
                "tags": classification.tags,
                "extraction": fields,
            }
        )
    return predictions


def score_comment(
    comment: dict, predictions: list[dict], expected: list[ExpectedMention], *, canonical: bool
) -> tuple:
    counts = Counter(predicted=len(predictions), expected=len(expected))
    errors = []
    remaining = list(predictions)
    for gold in expected:
        titles = {normalize_title(title) for title in [gold.title, *gold.aliases]}
        prediction = next((p for p in remaining if normalize_title(p["title"]) in titles), None)
        if prediction is None:
            counts["fn"] += 1
            counts["tags_fn"] += len(gold.tags)
            errors.append({"kind": "omission", "title": gold.title})
            continue
        remaining.remove(prediction)
        counts["tp"] += 1
        actual_tags, expected_tags = set(prediction["tags"]), set(gold.tags)
        counts["tags_tp"] += len(actual_tags & expected_tags)
        counts["tags_fp"] += len(actual_tags - expected_tags)
        counts["tags_fn"] += len(expected_tags - actual_tags)
        counts["sentiment_correct"] += prediction["extraction"].get("sentiment") == gold.sentiment
        counts["strength_correct"] += (
            abs(prediction["recommendation_strength"] - gold.strength) <= 0.1 + 1e-9
        )
        counts["provenance_correct"] += (
            prediction["extraction"].get("author_source") == gold.author_source
        )
        if canonical and gold.work_id is not None:
            counts["known_works"] += 1
            correct = prediction.get("openlibrary_id") in {gold.work_id, *gold.accepted_work_ids}
            counts["identity_correct"] += correct
            counts["authors_correct"] += {
                normalize_author(author) for author in prediction.get("authors", [])
            } == {normalize_author(author) for author in gold.authors}
            if not correct:
                errors.append(
                    {
                        "kind": "identity_mismatch",
                        "title": gold.title,
                        "expected_work": gold.work_id,
                        "actual_work": prediction.get("openlibrary_id"),
                    }
                )
        elif canonical:
            counts["ambiguous_works"] += 1
            counts["abstentions_correct"] += prediction.get("status") == "unresolved"
            if prediction.get("status") == "resolved":
                errors.append({"kind": "unexpected_resolution", "title": gold.title})
    counts["fp"] += len(remaining)
    counts["tags_fp"] += sum(len(p["tags"]) for p in remaining)
    errors.extend({"kind": "false_positive", "title": p["title"]} for p in remaining)
    for prediction in predictions:
        counts["grounded"] += bool(
            prediction["raw_mention"] and prediction["raw_mention"] in comment["text"]
        )
        if prediction.get("status") == "unresolved":
            counts["unresolved"] += 1
            errors.append({"kind": "unresolved", "title": prediction["title"]})
    return counts, errors


def evaluate_review(snapshot: dict, labels: dict, *, allow_partial: bool = False) -> dict:
    if snapshot.get("schema_version") != 2 or labels.get("schema_version") != 1:
        raise ValueError("Unsupported snapshot or review schema version")
    digest = content_digest(
        {
            key: value
            for key, value in snapshot.items()
            if key not in {"exported_at", "snapshot_digest"}
        }
    )
    if snapshot["snapshot_digest"] != digest:
        raise ValueError("Snapshot contents do not match its digest")
    if (labels["thread_id"], labels["source_digest"]) != (
        snapshot["thread_id"],
        snapshot["source_digest"],
    ):
        raise ValueError("Review labels refer to a different source snapshot")
    comments = {c["comment_id"]: c for c in snapshot["comment_records"]}
    if len(comments) != len(snapshot["comment_records"]):
        raise ValueError("Snapshot contains duplicate comments")
    reviewed = {c["comment_id"]: c for c in labels["comments"]}
    if (
        len(reviewed) != len(labels["comments"])
        or not reviewed.keys() <= comments.keys()
        or (not allow_partial and reviewed.keys() != comments.keys())
    ):
        raise ValueError(
            "Review must include each stored comment exactly once, including empty results; "
            "--allow-partial permits an explicit subset without duplicates or unknown IDs"
        )
    ready = []
    for comment_id, label in reviewed.items():
        if label["source_url"] != comments[comment_id]["hn_url"]:
            raise ValueError(f"Wrong source URL for comment {comment_id}")
        if label["status"] not in {"pending", "reviewed"}:
            raise ValueError(f"Invalid review status for comment {comment_id}")
        if label["status"] == "pending":
            if label["expected_mentions"] is not None:
                raise ValueError("Pending comments must not contain ground-truth labels")
            continue
        if not isinstance(label["expected_mentions"], list) or not labels.get("reviewer"):
            raise ValueError("Reviewed comments need explicit labels and a named reviewer")
        expected = [ExpectedMention.model_validate(m) for m in label["expected_mentions"]]
        seen = set()
        for mention in expected:
            titles = {normalize_title(title) for title in [mention.title, *mention.aliases]}
            if "" in titles or seen & titles:
                raise ValueError(
                    "Expected titles and aliases must be nonempty and unique per comment"
                )
            seen.update(titles)
            if mention.accepted_work_ids and mention.work_id is None:
                raise ValueError("Ambiguous labels cannot accept canonical work IDs")
            for work_id in mention.accepted_work_ids:
                ExpectedMention.model_validate(mention.model_dump() | {"work_id": work_id})
            if mention.raw not in comments[comment_id]["text"]:
                raise ValueError(f"Ungrounded review label in comment {comment_id}")
            if set(mention.tags) - set(snapshot["taxonomy"]):
                raise ValueError(f"Unknown review topic in comment {comment_id}")
        ready.append((comments[comment_id], label, expected))
    complete = len(ready) == len(comments)
    checkpoint = snapshot["processing_checkpoint"] or {}
    processed = (
        all(comment["processed"] for comment in comments.values())
        and bool(checkpoint.get("processed_version"))
        and bool(checkpoint.get("completed_at"))
    )
    raw_complete = bool(checkpoint.get("raw_complete"))
    if not allow_partial and not (complete and processed and raw_complete):
        raise ValueError(
            "Complete evaluation requires every comment reviewed, processed, a complete raw tree "
            "and a finished processing checkpoint"
        )
    if not ready:
        raise ValueError("No reviewed comments to evaluate")
    predictions = defaultdict(list)
    for mention in snapshot["mentions"]:
        predictions[mention["comment_id"]].append(
            mention | {"title": mention["extraction"].get("title") or mention["normalized_mention"]}
        )
    outputs = {}
    applied = [comment["comment_id"] for comment, _, _ in ready if comment.get("comment_review")]
    names = ["saved_extractor", "heuristic_baseline"]
    if applied:
        names.append("original_luna_extraction")
    for name in names:
        total, by_case, errors = Counter(), defaultdict(Counter), []
        for comment, label, expected in ready:
            actual = (
                predictions[comment["comment_id"]]
                if name == "saved_extractor"
                else original_luna_predictions(comment, predictions[comment["comment_id"]])
                if name == "original_luna_extraction"
                else heuristic_predictions(comment)
            )
            counts, mistakes = score_comment(
                comment, actual, expected, canonical=name == "saved_extractor"
            )
            total.update(counts)
            for case in set(label["case_types"]) or {"uncategorized"}:
                by_case[case].update(counts)
            errors.extend(
                error | {"comment_id": comment["comment_id"], "source_url": comment["hn_url"]}
                for error in mistakes
            )
        outputs[name] = {
            "metrics": summarize(total, canonical=name == "saved_extractor"),
            "by_case": {
                case: summarize(counts, canonical=name == "saved_extractor")
                for case, counts in sorted(by_case.items())
            },
            "errors": errors,
        }
    return {
        "thread_id": snapshot["thread_id"],
        "reviewer": labels["reviewer"],
        "applied_comment_reviews": applied,
        "coverage": {
            "stored_comments": len(comments),
            "reviewed_comments": len(ready),
            "complete_thread_review": complete and processed and raw_complete,
        },
        "source_digest": snapshot["source_digest"],
        "snapshot_digest": snapshot["snapshot_digest"],
        "labels_digest": content_digest(labels),
        "observed_versions": snapshot["observed_versions"],
        "processing_checkpoint": snapshot["processing_checkpoint"],
        "recorded_rebuild_runs": snapshot["recorded_rebuild_runs"],
        "taxonomy_digest": content_digest(snapshot["taxonomy"]),
        "evaluation_version": 2,
        "results": outputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument(
        "--allow-partial", action="store_true", help="Report explicitly incomplete review coverage"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() in {args.snapshot.resolve(), args.labels.resolve()}:
        parser.error(
            "The evaluation output must be separate from saved predictions and review labels"
        )
    try:
        saved = json.loads(args.snapshot.read_text())
        report = evaluate_review(
            saved.get("snapshot", saved),
            json.loads(args.labels.read_text()),
            allow_partial=args.allow_partial,
        )
    except ValueError as error:
        parser.error(str(error))
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
