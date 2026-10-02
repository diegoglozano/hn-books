# HN Opinionated Library — Next Steps

Planning date: 2026-10-02.

This roadmap follows the original MVP requirements. Check each completion
criterion against the current implementation before starting new work.

## Priorities

1. Trustworthy book identities and grounded HN evidence.
2. Repeatable ingestion, review and reprocessing.
3. A useful ranked library and lexical search.
4. Measured recommendation and clustering experiments.

Keep the deployment suitable for a small home mini PC. Retain
React/Vite/TypeScript, Python/FastAPI, SQLite/FTS5, Docker and Coolify.
Use uv, Ruff and ty. Add Polars and scikit-learn where processing or
experiments justify them.

## Current baseline

The latest README documents:

- The library, book details, tags, rankings and lexical search.
- Date-bounded, resumable historical ingestion.
- Ingestion-time Luna extraction and classification with cached outputs.
- A staged single-thread rebuild and an extraction review export.
- Grounding validation and conservative metadata resolution.

These capabilities still need validation against manually labeled examples.

The existing golden evaluation measures the legacy heuristic extractor.
Mocked model tests verify integration behavior, not real model accuracy.

## 1. Validate one complete reading thread

This is the first priority before expanding ingestion.

### Tasks

- Review every comment, including replies and comments with no extracted books.
- Label title spans, canonical works, authors, sentiment, recommendation
  strength and controlled topics.
- Include long lists, numerical titles, abbreviations, translated titles,
  edition qualifiers and multiple authors.
- Include common-word titles such as It, We and Go.
- Distinguish books from programming languages, pronouns, articles,
  adaptations and incidental comparisons.
- Verify evidence against the original comment.
- Record author provenance: stated, contextual or inferred.
- Resolve at the work level while preserving edition identifiers separately.
- Retain unresolved mentions and competing candidates.
- Review duplicate Open Library records using documented identity rules.

### Completion criteria

- Each resolved mention has the correct identity and grounded HN evidence.
- Missed books and false matches are counted.
- Repeated processing creates no duplicates.
- Interrupted processing preserves raw data and the live library.
- Verified books appear through FastAPI and React with original HN links.

## 2. Build a golden dataset for the current extractor

### Tasks

- Turn reviewed comments into versioned, source-linked fixtures.
- Include positive, negative and genuinely ambiguous examples.
- Measure:
  - mention extraction precision and recall;
  - canonicalization accuracy and unresolved rate;
  - tag precision and recall;
  - recommendation-strength agreement.
- Report results by case type, not only overall averages.
- Compare the current extractor with the heuristic baseline on identical data.
- Track model, prompt, taxonomy and matching versions.
- Track runtime, cache reuse, token usage and cost.
- Make evaluation reproducible using saved outputs.
- Establish quality and cost targets before broad ingestion.
- Add regressions for confirmed production mistakes.
- Cover normalization, fuzzy matching, ranking, independence calculation
  and tag aggregation with unit tests.

### Completion criteria

- A saved report identifies false positives, omissions and unresolved identities.
- Real extraction accuracy is distinguished from mocked integration tests.
- Improvements are measured on fixed fixtures and held-out examples.
- Committed fixtures contain no credentials, private logs or deployment details.

## 3. Resume reliable historical ingestion

Return to the selected five-year backfill after the quality gate passes.

### Tasks

- Keep discovery queries and manually whitelisted IDs in configuration.
- Verify date boundaries, pagination, deduplication and complete comment trees.
- Describe coverage accurately: matching indexed reading-thread titles,
  rather than every potentially relevant HN discussion.
- Persist raw HN items before extraction or classification.
- Separate fetching checkpoints from derived-processing checkpoints.
- Resume interrupted work using stored raw data and cached external responses.
- Reprocess when extraction or matching versions change.
- Preserve review decisions across reprocessing.
- Confirm the intended live corpus before switching between a single-thread
  replacement workflow and historical ingestion.
- Execute long jobs through Coolify tasks with execution history and suitable
  timeouts.
- Serialize ingestion, rebuild and reprocessing with the writer lock.

### Completion criteria

- The requested scope completes with recorded pending counts and failures.
- Repeated runs reuse checkpoints and caches.
- Interrupted runs resume without duplicating records.
- The stored corpus can be reprocessed without fetching HN again.
- Scheduled execution continues independently of browser tabs.

## 4. Make unresolved identities reviewable

### Tasks

- Provide authenticated access to unresolved mentions, source comments,
  author provenance, confidence and candidate works.
- Persist explicit canonical mappings and curated aliases.
- Provide a CLI or authenticated correction workflow for:
  - resolving ambiguous identities;
  - merging duplicate books;
  - rejecting false mentions;
  - correcting canonical records.
- Record correction provenance.
- Rebuild affected rankings, tags and search indexes.
- Keep destructive operations out of public endpoints.

### Completion criteria

- Reviewers can correct identities without editing raw comments.
- Corrections survive reprocessing.
- False recommendation counts disappear after their source mentions are corrected.
- Work-versus-edition decisions remain inspectable.

## 5. Validate the core library

### Ranking and tags

- Expose mention counts, positive recommendation counts, independent users,
  independent threads and date coverage separately.
- Keep ranking factors inspectable:
  frequency, independence, strength, quality and time.
- Preserve all-time and recent rankings with modest recency weighting.
- Use comment scores only when available and reliable.
- Tag individual mentions, then aggregate tags to books.
- Test sensitivity to prolific users and repeated conversation replies.
- Prevent praise for one book from being attached to another book in the
  same comment.

### Search and frontend

- Validate browsing, pagination/loading, sorting and tag filters.
- Show metadata, ranking factors, history, source comments and HN links.
- Index titles, authors, descriptions, tags and HN context using FTS5.
- Evaluate title/author relevance separately from full-comment relevance.
- Test realistic queries such as “distributed systems” and
  “books about databases.”
- Confirm that stale false mentions disappear from rankings and search.
- Verify loading, empty, error and retry states on desktop and mobile.

### Completion criteria

- Displayed identities, counts and recommendation evidence are accurate.
- Rankings and filters agree with mention-level data.
- Search passes a small labeled set of realistic queries.
- Reprocessing rebuilds aggregates and indexes consistently.

## 6. Complete lightweight operations

### Tasks

- Keep one application image for FastAPI and processing commands.
- Verify persistent SQLite storage and container write permissions.
- Verify the health check and Coolify routing.
- Use scheduled tasks/cron for daily ingestion.
- Record run status, progress, skipped/pending counts, failures and runtime.
- Distinguish active work, interrupted runs and the last successful run.
- Make competing writer runs identifiable to operators.
- Document timeout, cancellation, deployment and resume behavior.
- Test consistent SQLite backups and restoration.
- Measure memory use, disk growth and processing cost.
- Keep secrets outside Git.

### Completion criteria

- Operators can identify the active job and see completed progress.
- Failed jobs have useful logs and a documented retry procedure.
- Daily ingestion requires no open terminal.
- A tested backup restores the corpus and cached processing state.
- Resource consumption fits the mini PC.

## 7. Experiment with semantic discovery and clustering

These experiments follow a useful, validated core library.

### Semantic representation

- Aggregate HN recommendation-context embeddings into book representations.
- Compare these with publisher-description representations.
- Start with a simple nearest-neighbor baseline and KMeans.
- Version representations, aggregation methods and clustering parameters.
- Evaluate retrieval quality, cluster coherence and stability.

### Co-recommendation graph

- Connect books recommended by the same users and/or in the same threads.
- Weight repeated independent co-occurrence.
- Measure distortion from prolific users and large threads.
- Test community detection and graph-based neighbors.
- Compare graph neighborhoods with semantic neighborhoods.

### Hybrid recommendations

- Compare semantic-only, graph-only and hybrid methods.
- Accept interests, selected tags, liked books and exclusions.
- Combine similarity with the HN ranking baseline.
- Explain recommendations using actual HN comments and measured signals.
- Start with SQLite storage and local brute-force retrieval.
- Evaluate sqlite-vec only when measured limitations justify it.

### Completion criteria

- Evaluate on labeled queries or held-out users/threads.
- Check for evaluation leakage.
- Report quality, runtime, memory consumption and cost.
- Ship experimental features only when they improve discovery.
- Serving recommendations does not require an LLM call for every query.

## Scientific questions

| Question | Experiment |
| --- | --- |
| Does independence beat raw mentions? | Compare mention counts with unique-user/thread/date signals |
| Does recommendation strength help? | Compare ranking with and without strength |
| Are HN contexts better than descriptions? | Evaluate both representations on identical queries |
| Do HN clusters differ from categories? | Compare clusters with the controlled taxonomy |
| Does co-recommendation improve retrieval? | Compare results with and without graph features |
| Does a hybrid approach help? | Compare semantic, graph and hybrid methods |
| Are rankings stable over time? | Save snapshots and measure rank changes |
| Do prolific users distort results? | Apply user caps and leave-one-user-out analysis |
| Can cheaper classification match the current model? | Compare constrained classification/Jev, if available, on the same golden data |

## Milestone order

| Milestone | Required result |
| --- | --- |
| M1 — Verified thread | Complete raw tree, reviewed identities, metadata, independent recommendations, API and React evidence |
| M2 — Repeatable corpus | Current-extractor evaluation, persisted review decisions and resumable ingestion |
| M3 — Useful library | Validated ranking, tags, lexical search, history and scheduled operation |
| M4 — Recommendation science | Measured semantic, graph and hybrid experiments |

## Deferred scope

Defer accounts, social features, app-authored reviews, payments, mobile apps,
real-time ingestion, elaborate recommendation ML, dedicated vector databases,
Elasticsearch, Redis, Celery, Airflow, Dagster, Kubernetes and distributed services.

Consider additional metadata providers, sqlite-vec, advanced classifiers,
Postgres or orchestration changes only after measured usage or data requirements
justify their complexity.
