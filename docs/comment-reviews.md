# Source-bound comment corrections

The last verified full rebuild completed on October 3, 2026 at 03:00 UTC with all
32 then-deployed review decisions applied: 870 comments, 930 resolved and 503
unresolved mentions across 588 books. It reused 864 cached Luna results, made no
new model requests, performed eight metadata lookups and recorded zero failures
in 44.3 seconds. A subsequent unresolved-only retry completed at 03:28 UTC with
the same library totals and no model requests or failures. These counts
establish processing coverage;
they do not establish extraction accuracy.

Screening the 177 comments with no extracted mentions found missed contextual
replies, an abbreviation and a numerical title. Reviewing two adjacent author
comparisons also found seven false entries for author names or an unspecified
novel. The first correction set in
[app/data/comment_reviews.json](../app/data/comment_reviews.json) covers 11 complete
comments: ten added mentions and seven rejected entries, while retaining the
valid Middlemarch mention. The decisions are Codex source checks, pending
independent human review. This is a selected regression set, not a complete
thread review or a held-out accuracy benchmark.

The expanded set adds 21 complete comment decisions, bringing the total to 32.
These reject author-only entries and unspecified works, distinguish TV-adaptation
praise from book recommendations, and correct titles, author provenance,
reading intentions and topics. Examples include rejecting an arbitrary John le
Carré omnibus, keeping only The River in a comment praising the Station Eleven
TV series, and identifying There Is No Antimemetics Division from the immediate
parent instead of choosing Ra from an older ancestor. Energy and Civilization
is nonfiction history, so its science-fiction tag is removed. All decisions bind
to the current comment and its actual ancestor context.

The next batch adds 30 complete comments: the first remaining live, nonempty
comments of at most 250 characters, ordered by comment ID. This is a systematic
short-comment batch, with titles, replies, lists, praise and reading intentions.
It adds 15 persisted comment decisions and revises the existing parenting-title
decision, bringing the correction file to 47 decisions. These changes still need
the next production rebuild.

Examples include identifying authors on Private Revolutions, Quantum Break:
Zero State and the two Patrick Radden Keefe books; retaining a neutral request
about Gibbon's abridgements; and distinguishing manga adaptations from original
Lovecraft prose. The comment does not identify the manga creators, so those
references remain unresolved rather than acquiring guessed adaptation credits.
The quoted Carryx series and The Captive's War are one series reference. The
Selfish Gene is evolutionary biology, for which the controlled taxonomy has no
tag; it must not be tagged science fiction. The Mistborn catalog record describes
a complete trilogy bundle and is retained, with its fantasy topic corrected.

The source-linked [regression labels](../tests/fixtures/reviewed_comment_labels.json)
now cover 134 comments. Of 83 expected mentions, 60 have catalog-checked
identities, 19 still require catalog verification and four have genuinely
ambiguous identities: unspecified Bible editions and manga creators. The
[saved baseline](../tests/fixtures/reviewed_comment_baseline.json) was recomputed
against the enlarged labels and records ten omissions and 39 false entries before
applying corrections. The earlier 104-comment baseline recorded eight omissions
and 38 false entries.
These are different label sets, not an improvement in the model. The parenting
book's publisher title is Ten Things I Wish You Knew About Raising Boys by Billy
Garvey; evaluation also accepts the commenter's paraphrase as a title alias.
Those figures describe these regressions, not accuracy across the thread. No full comments, credentials
or deployment logs are committed in the fixtures.

To compare a saved full-thread export against these sparse labels, explicitly allow
partial evaluation:

```bash
uv run python -m app.evaluate_review \
  --snapshot data/review-bundle.json \
  --labels tests/fixtures/reviewed_comment_labels.json --allow-partial \
  --output data/comment-regression-evaluation.json
```

Coverage is 134 of 870 comments. Full evaluation still requires every comment
to be present and reviewed, with no pending identity labels. Do not treat a selected
regression set as satisfying the complete-thread quality gate. A pending catalog
check is distinct from a genuinely ambiguous identity: it does not score as a
correct abstention or contribute to canonicalization accuracy. Mention, sentiment,
strength and topic metrics can still be evaluated independently of that check.

The October 3 production export verified all six repaired mentions from the prior
[identity review](identity-reviews.md), including all 32 then-checked identities.
The new offline projection matches all 83 expected mentions, all 60 checked work
and author identities, and all four required abstentions in the enlarged selected
set. It processes complete comments through the normal resolver using saved
extractions and metadata lookups, with zero model requests. It is a local
simulation, not a completed production rebuild or full-thread accuracy result.
The separate original-Luna comparison still reports ten omissions and 39 false
entries against these same enlarged labels. This verifies the repairs without
presenting curated outputs as improved model accuracy.

## Scope and evidence

Every decision stores the thread/comment IDs, reviewer, date, reason, case types,
source links and a complete corrected result. A digest binds it to the exact
model input, including the current text, links, ancestor IDs/text and thread
context. Current-comment evidence must be literal; ancestors may identify what
that evidence refers to. A plural reference can support two distinct works when
the parent explicitly identifies both.

The numerical title's author is supported by the
[National Library of New Zealand catalog](https://natlib.govt.nz/records/22191646).
The DCC abbreviation's author is supported by the
[publisher's series page](https://www.penguinrandomhouse.com/series/43C/dungeon-crawler-carl/).
The first set's other author credits come from the linked HN ancestors, except the
well-known George Eliot attribution on Middlemarch. The expanded decisions also
link publisher and catalog records for inferred credits and corrected titles.
Corrections still pass through normal bibliographic resolution: adding a reviewed mention does not force its work ID
or guarantee a resolved catalog match.

Review only uniquely identifiable references. Unnamed novels, author-only praise,
ambiguous singular replies to lists, and adaptation discussions must not acquire
an invented book identity. Bible translations and other remaining omissions need
further review; the current set does not claim to fix every screened comment.

## Prepare a correction

Save an authenticated review snapshot as described in
[the review workflow](thread-review.md). Write a separate JSON file containing
the complete corrected `{"mentions": [...]}` result for one comment. Use
`{"mentions": []}` to reject all its false mentions. Each mention uses the Luna
schema: `raw`, `title`, `author`, `author_source`, `work_id`, `confidence`,
`sentiment` and `tags`. Retain every valid mention; this replaces the whole
comment's derived output. Correct sentiment or topics in the same way.

```bash
uv run python -m app.comment_reviews \
  --snapshot data/review-bundle.json --comment 49901526 \
  --mentions data/corrected-mentions.json \
  --reviewer 'Reviewer name' \
  --reason 'Author comparisons do not identify any exact literary work.' \
  --case author-only --output data/proposed-comment-reviews.json
```

Add bibliographic evidence with repeatable `--source` options. The command
validates the saved snapshot and correction, merges existing decisions, and writes
a **new proposed file**. It does not modify SQLite or overwrite existing files.
Review its diff, copy it to `app/data/comment_reviews.json`, and deploy through a
PR. Changing an existing decision replaces that comment's entry; Git retains
the previous revisions. Raw comments remain unchanged. Book merging and a
correction UI remain future work.

## Apply and audit

After deployment, execute the existing Coolify task:

```bash
python -m app.rebuild --thread 49893157
```

The review digest changes the processing version, while model prompts and cache
keys stay unchanged. Corrections apply after cached extraction and before metadata
resolution. The staged rebuild preserves the live library until processing,
rankings, tags, search indexes and database checks complete. Decisions survive
fresh staging because they live in Git. Removing a decision restores the cached
model result on the next rebuild.

Changed source or ancestor context aborts processing before model requests or
replacement of derived rows. Review the new source and update the decision
through a PR rather than silently reusing it.

The authenticated export stores applied decisions and the original validated Luna
result on each comment, including comments whose entire output was rejected.
The extraction cache retains the original model response. Individual corrected
mentions carry `extraction.comment_review` provenance.

Offline evaluation reports which reviewed comments contain corrections. When
corrections are present it additionally reports `original_luna_extraction`
precision, recall, sentiment, strength and topic metrics against the same labels.
It does not claim canonical metadata accuracy for raw model results. This keeps
manual repairs from appearing to improve Luna's own extraction accuracy. Continue
independent labeling of every comment before expanding ingestion.
