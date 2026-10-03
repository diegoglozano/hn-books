# Engaged reading-thread expansion

The selected corpus expands beyond the latest thread with a five-year discovery
window and a minimum indexed comment count of **101**. Discovery searches Ask HN
story titles and retains What/Which (book/books) are you reading questions. Generic
reading discussions, blogs, headlines and reading workflows do not qualify.
This is title-query coverage in Algolia, not a claim to find every HN book discussion.

## First discovery snapshot

Checked October 3, 2026 against the [HN Search API](https://hn.algolia.com/api)
and the original HN story records. The five-year interval contains two eligible
threads:

| Thread | Date | Indexed comments | Initial action |
| --- | --- | ---: | --- |
| [What are you reading?](https://news.ycombinator.com/item?id=49893157) | 2026-09-29 | 880 | Already processed under the current version; skip |
| [What are you reading these days?](https://news.ycombinator.com/item?id=38237730) | 2023-11-12 | 281 | Append the complete comment tree and process with Luna |

The official HN records report the same current descendant counts:
[latest thread](https://hacker-news.firebaseio.com/v0/item/49893157.json),
[November 2023 thread](https://hacker-news.firebaseio.com/v0/item/38237730.json).
The latest thread's last completed rebuild reused 870 stored comments. Discovery
counts describe the current indexed discussion, including replies, and can differ
from the stored tree. Skipping its completed checkpoint preserves its existing
reviewed source; the separate daily ingestion command can refresh it later.

The initial pending addition therefore has an estimated **281 comments**. These
are comment counts, not guaranteed book mentions, and discovery incurs no Luna
requests. A successful ingestion will record the actual tree size, cache reuse,
request/token counts, unresolved matches and failures.

## Run in Coolify

After deployment, change the existing task command to:

```bash
python -m app.ingest reading --years 5
```

Use service `library`, timeout **36000 seconds**, and **Execute Now**. Keep
`LUNA_WORKERS=4` in Coolify for four concurrent model requests; the application
also defaults to four when the variable is absent. Upstream metadata requests
remain throttled separately. The ingestion command appends to the existing corpus,
stores raw items before extraction, applies source-bound reviews and updates
rankings, tags and search. Its writer lock serializes mutations.

Use `--dry-run` to preview, `--limit 3` to bound a batch, and repeat the same command
to resume interruptions. Completed current-version threads are skipped. A failed
thread remains pending; successfully stored raw records and extraction responses
are reused. Results publish per processed thread, using the existing resumable
ingestion workflow. This is distinct from the single-thread replacement rebuild.

## Review scope

This expansion was explicitly selected while full-thread review remains incomplete.
The latest thread has 184 of 870 stored comments reviewed, with 164 expected
mentions, 115 checked work/author identities, 32 pending catalog checks and 17
ambiguous identities. Its October 3 production rebuild verified all 80 persisted
comment corrections: 601 books, 946 resolved and 497 unresolved mentions, with
zero new Luna requests or failures.

The historical addition starts as model-processed source data. Processing success
and engagement do not establish extraction accuracy. Preserve the selected
regression results, label new-thread samples separately, and continue the
[source review](thread-review.md) without describing the enlarged corpus as
independently verified or treating curated regressions as a held-out benchmark.
The original [roadmap](next-steps.md) is retained unchanged.
