"""Prepare source-bound comment corrections for review and deployment through Git."""

import argparse
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db import utc_now
from app.extraction import normalize_title
from app.luna import LunaResult, validate_result
from app.review import content_digest

REVIEWS_PATH = Path(__file__).parent / "data" / "comment_reviews.json"


class CommentReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: int = Field(gt=0)
    comment_id: int = Field(gt=0)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    reason: str = Field(min_length=10)
    sources: list[str] = Field(min_length=1)
    case_types: list[str] = Field(min_length=1)
    result: LunaResult


class CommentReviews(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    comments: list[CommentReview]


def source_digest(thread_id: int, comment_id: int, payload: dict) -> str:
    # Bind the decision to the current evidence AND the exact context used to interpret it.
    return content_digest({"thread_id": thread_id, "comment_id": comment_id, "input": payload})


def read_reviews(path: Path) -> CommentReviews:
    reviews = CommentReviews.model_validate_json(path.read_text())
    keys = [(review.thread_id, review.comment_id) for review in reviews.comments]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate comment review decisions")
    return reviews


@lru_cache
def reviewed_comments() -> CommentReviews:
    return read_reviews(REVIEWS_PATH)


def review_digest() -> str:
    return content_digest(reviewed_comments().model_dump())


def check_review(review: CommentReview, payload: dict) -> None:
    if review.source_digest != source_digest(review.thread_id, review.comment_id, payload):
        raise ValueError(f"Comment review source changed for {review.comment_id}; review it again")
    result = review.result.model_copy(deep=True)
    before = result.model_dump()
    # Reviewed fields must already be literal and valid; silent model-output repair is unsuitable.
    if any(m.raw not in payload["current_comment"]["text"] for m in result.mentions):
        raise ValueError("Reviewed evidence must be an exact current-comment excerpt")
    validate_result(result, payload)
    if before != result.model_dump():
        raise ValueError("Reviewed fields need normalization; correct the decision explicitly")
    titles = [normalize_title(m.title) for m in result.mentions]
    if len(titles) != len(set(titles)):
        raise ValueError("Reviewed result contains duplicate titles")


def comment_review(thread_id: int, comment_id: int, payload: dict) -> CommentReview | None:
    review = next(
        (
            r
            for r in reviewed_comments().comments
            if (r.thread_id, r.comment_id) == (thread_id, comment_id)
        ),
        None,
    )
    if review:
        check_review(review, payload)
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--comment", type=int, required=True)
    parser.add_argument(
        "--mentions", type=Path, required=True, help="Complete corrected LunaResult JSON"
    )
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--reviews", type=Path, default=REVIEWS_PATH)
    parser.add_argument(
        "--output", type=Path, required=True, help="New proposed review file; never overwrites"
    )
    args = parser.parse_args()
    if args.output.resolve() in {
        args.snapshot.resolve(),
        args.mentions.resolve(),
        args.reviews.resolve(),
    }:
        parser.error("Output must be separate from source files and existing review decisions")
    try:
        saved = json.loads(args.snapshot.read_text())
        snapshot = saved.get("snapshot", saved)
        if snapshot.get("schema_version") != 2 or snapshot["snapshot_digest"] != content_digest(
            {k: v for k, v in snapshot.items() if k not in {"exported_at", "snapshot_digest"}}
        ):
            raise ValueError("A valid complete review snapshot is required")
        comments = [c for c in snapshot["comment_records"] if c["comment_id"] == args.comment]
        if len(comments) != 1:
            raise ValueError("The selected comment must appear exactly once in the snapshot")
        comment = comments[0]
        review = CommentReview(
            thread_id=snapshot["thread_id"],
            comment_id=args.comment,
            source_digest=source_digest(snapshot["thread_id"], args.comment, comment["input"]),
            reviewer=args.reviewer,
            reviewed_at=utc_now(),
            reason=args.reason,
            sources=[comment["hn_url"], *args.source],
            case_types=args.case,
            result=LunaResult.model_validate_json(args.mentions.read_text()),
        )
        check_review(review, comment["input"])
        existing = read_reviews(args.reviews)
        existing.comments = [
            r
            for r in existing.comments
            if (r.thread_id, r.comment_id) != (review.thread_id, review.comment_id)
        ] + [review]
        existing.comments.sort(key=lambda r: (r.thread_id, r.comment_id))
        with args.output.open("x") as output:
            output.write(json.dumps(existing.model_dump(), ensure_ascii=False, indent=2) + "\n")
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    print(f"Prepared {args.output}; review the diff and deploy it as {REVIEWS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
