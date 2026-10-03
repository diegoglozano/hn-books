# Complete-thread review

Start with thread [49893157](https://news.ycombinator.com/item?id=49893157).
The [roadmap](next-steps.md) retains complete-thread review as a milestone. The
selected [engaged-thread expansion](engaged-reading-threads.md) proceeds within
a five-year window while review remains incomplete: 184 of the latest thread's
870 stored comments are source-checked. Newly ingested threads also need separate
source review. A successful rebuild, a resolved-book count, and mocked model
tests do not establish extraction accuracy.

## Capture the results

Set `ADMIN_TOKEN` as a secret environment variable in Coolify and redeploy. The
existing admin routes remain disabled when this setting is empty. Send the token
in an `Authorization: Bearer …` header to:

```text
GET /api/admin/threads/49893157/review
```

The response contains `snapshot` and `review_template`. Save the response as
`data/review-bundle.json`, and save its `review_template` field separately as
`data/review-labels.json`. Do not put the token in a URL or a committed fixture.
For example, from a machine with `ADMIN_TOKEN` already set:

```bash
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${ADMIN_TOKEN}" \
  https://books.diegoglozano.com/api/admin/threads/49893157/review \
  --output data/review-bundle.json
```

The same export works inside a Coolify scheduled task without bearer access:

```bash
python -m app.review --thread 49893157 \
  --output /data/review-snapshot.json --template /data/review-labels.json
```

Locally use `uv run` and `data/` paths. Template creation refuses to overwrite
an existing file, so rerunning it cannot erase labels. The API creates an in-memory
pending template; it never saves, changes, or replaces review decisions.

The snapshot reads SQLite in a single read-only transaction. It contains:

- Every stored comment, its HTML/plain text, source link, parent, deleted/dead
  flags, processing hash, model input context and mention IDs.
- Every resolved and unresolved mention, original extraction audit, candidates,
  author provenance, sentiment, strength and per-mention topics.
- Canonical work identifiers, separate ISBNs and metadata, plus the raw-tree
  checkpoint and versions actually observed in stored results.
- Up to 30 recorded rebuild runs for this thread, with runtime, cache and token
  metrics. These are recorded run metrics, not a lifetime billing total or price
  estimate; a fresh staging database may not retain earlier runs.

`comments_without_mentions` includes only comments with no detected mentions;
a comment with unresolved mentions is still represented in the main comment list.
Processing success and resolution success are separate.

Applied [comment corrections](comment-reviews.md) appear as `comment_review` on
each affected comment, including rejected empty outputs. This includes the
original validated Luna result; corrected mentions also carry review provenance.
Evaluation lists applied corrections and reports original Luna extraction metrics
separately when corrections are present. The saved pipeline result includes
review decisions and must not be represented as raw model accuracy.

`source_digest` identifies source text, comment identities, authors and tree
relationships. HN vote changes alone do not invalidate labels. `snapshot_digest`
also identifies saved predictions, metadata, processing state, taxonomy and run
metrics, excluding the export timestamp. Later predictions can be evaluated against
the same labels if the source digest still matches.

## Label independently

Set a descriptive `reviewer` identifier. Every comment starts with `status: pending`
and `expected_mentions: null`; predictions are not copied into the ground truth.
Read the original comment and ancestors, then set `status: reviewed` and write
the expected mentions. Use `expected_mentions: []` for a reviewed comment with no
books, including empty/deleted comments.

Each expected mention requires these fields:

```json
{
  "raw": "Dune",
  "title": "Dune",
  "aliases": [],
  "authors": ["Frank Herbert"],
  "work_id": "/works/OL893414W",
  "identity_status": "verified",
  "author_source": "inferred",
  "sentiment": "recommended",
  "strength": 0.95,
  "tags": ["fiction"]
}
```

This is a schema example using [Open Library's Dune work](https://openlibrary.org/works/OL893414W/Dune),
not a label for an actual thread comment. `raw` must
appear literally in that comment's plain text. Verify the work and authors using
bibliographic sources. `identity_status: verified` requires a checked work ID.
Use `identity_status: ambiguous` with `work_id: null` when the identity should
remain unresolved. Use `identity_status: pending` with `work_id: null` when the
catalog identity has not yet been checked. Pending identities are listed in the
report, excluded from canonicalization and abstention scoring, and prevent the
full-thread quality gate from passing. Legacy labels without `identity_status`
retain their previous meaning: a work ID is verified and a null ID is ambiguous.
ISBNs describe editions and do not replace a work ID. `author_source`
is `stated`, `context`, `inferred`, or `unknown`. Topics must come from the snapshot's
controlled taxonomy. Strength is between 0 and 1; sentiment is `recommended`,
`positive`, `neutral`, or `negative`.

Use `case_types` for categories such as `long_list`, `numeric_title`, `abbreviation`,
`translation`, `edition`, `multiple_authors`, `common_word`, `contextual_reply`,
`comparison`, `negative`, `ambiguous`, and `no_books`. Add notes explaining
non-obvious decisions and bibliographic evidence. Aliases accept independently
verified alternative title spellings in the extraction comparison. Do not add an
alias merely to make an incorrect title pass.

For independently verified duplicate catalog records of the same work, optional
`accepted_work_ids` lists equivalent IDs. Document the bibliographic basis in the
comment's notes before evaluating an updated matcher; do not accept a comic,
adaptation, or different volume simply because it shares a title.

The current pipeline and evaluator use one normalized title per comment. Distinct
works with the same normalized title in one comment need a future identity-aware
mention schema; record that limitation rather than collapsing them into a claim
of complete accuracy.

## Evaluate saved outputs

```bash
uv run python -m app.evaluate_review \
  --snapshot data/review-bundle.json --labels data/review-labels.json \
  --output data/review-evaluation.json
```

The evaluator accepts an API bundle or a standalone CLI snapshot. It verifies the
snapshot digest, source association, one review entry per stored comment, source
links, grounded label excerpts and controlled topics. A full evaluation requires
every comment reviewed and processed, with a complete raw tree, a finished
processing checkpoint and no pending identity labels. Old processing hashes alone
cannot pass after a source refresh clears that checkpoint. During
review, `--allow-partial` permits a sparse subset of source-linked labels, reports
the explicit coverage and leaves `complete_thread_review: false`. Duplicate or
unknown comment IDs remain invalid. Unlabeled comments never contribute to the
reported partial metrics.

It compares the saved extractor and the legacy heuristic on the **same reviewed
comments**, reporting overall and per-case results. Matching uses normalized titles
and explicit aliases; duplicate predictions count as false positives. The report
lists omissions, false positives, wrong work identities, unresolved mentions and
unexpected resolutions of identities labeled ambiguous.

- Mention precision/recall count all detected titles, including unresolved ones.
- Canonicalization accuracy and author accuracy use matched mentions with a known
  reviewed work ID; missed titles are counted by recall. Abstention accuracy uses
  matched mentions labeled ambiguous. The heuristic comparison evaluates extraction
  and classification only, since it performs no metadata resolution here.
- Topic precision/recall include tags on false-positive mentions and missing tags
  on omitted mentions. Sentiment and strength agreement use matched mentions;
  strength tolerance is 0.1.
- Grounded-evidence rate requires each prediction's stored excerpt to appear in
  the current comment. Undefined metrics have JSON `null`, not a perfect score.

Source, prediction, label and taxonomy digests, observed model/prompt/matching
versions, processor checkpoint and evaluation version accompany the results.
Running this command makes no API requests and incurs no extraction charges.
The report is repeatable on saved files; it does not establish held-out accuracy
or certify the labels as independently human-verified.

## Finish the milestone

Full-thread labeling remains outstanding until a production snapshot is captured
and every comment is independently reviewed. Check the known series-versus-volume
and contributor-versus-author issues, compare misses against unresolved detections,
and record corrections. Metadata fixes deployed as code require a rebuild task to
be reflected in stored results; exporting alone does not reprocess anything.

Review labels do not mutate the library. Verified
[identity mappings](identity-reviews.md) and
[source-bound comment corrections](comment-reviews.md) are persisted through Git
and applied on the next rebuild. Continue complete labeling, held-out cases and
agreed quality/cost targets before resuming the selected historical corpus. Keep saved
production exports under ignored `data/`; commit deliberately curated public HN
fixtures with review provenance, without tokens or private deployment information.
