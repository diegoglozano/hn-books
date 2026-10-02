# HN Opinionated Library

A small, self-hosted bookshelf built from Hacker News reading discussions. Every book keeps its original recommendation evidence: comments, usernames, timestamps, thread titles, and links back to HN.

This implements the **first meaningful milestone**, with a ranked React library, book details, topic filters, FTS5 search, and opt-in thread discovery. Ingestion and reprocessing run as CLI commands using the same image as the web app.

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node 22 with npm.

```bash
uv sync --frozen
# Set OPENAI_API_KEY in your environment or local .env before ingestion.
npm ci --prefix frontend
npm run build --prefix frontend
uv run python -m app.ingest thread 49893157
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. The current focus is [Ask HN: What are you reading?](https://news.ycombinator.com/item?id=49893157). The first ingestion makes cached Open Library lookups and may take a few minutes. Nothing is seeded automatically; books come from your chosen threads. The default database is `data/hn_books.db`.

For frontend development, run `npm run dev --prefix frontend` in a second terminal and open Vite's printed URL. Vite proxies API requests to port 8000. No CORS configuration or separate production frontend service is needed.

## Commands

Run these from the repository root, prefixed by `uv run` locally. Inside the Docker image, `python` already uses the installed application environment.

| Command | Behavior |
| --- | --- |
| `python -m app.ingest thread <HN_ID>` | Fetch one complete comment tree, persist raw records, process mentions, rebuild aggregates |
| `python -m app.ingest` | Refresh configured and already stored threads; suitable for daily scheduling |
| `python -m app.ingest discover` | Discover the first Algolia result page for each configured Ask HN query, then ingest |
| `python -m app.ingest backfill --years 5` | Resume five years of date-bounded reading-thread discovery and ingestion |
| `python -m app.reprocess mentions` | Re-extract stored comments; reuse local books/cache and look up missing metadata |
| `python -m app.reprocess mentions --offline` | Re-extract with no network calls; requires cached Luna results and metadata |
| `python -m app.reprocess mentions --thread <HN_ID>` | Re-extract one stored thread without fetching its HN comments again |
| `python -m app.reprocess tags` | Reuse Luna extraction to refresh classifications, then rebuild scores/search |
| `python -m app.rebuild --thread 49893157` | Back up the library, rebuild only this thread using Luna in staging, then replace the library |
| `python -m app.review --thread 49893157 --output data/latest-thread-review.json` | Export all resolved/unresolved extractions with comment evidence for review |
| `python -m app.recompute rankings` | Rebuild scores and FTS5 index |
| `python -m app.evaluate` | Print golden extraction, matching, tag, and strength metrics offline |

Daily ingestion deliberately starts with an explicit corpus. Historical discovery is opt-in. A partial run exits nonzero, logs failures, and preserves fetched raw data for retry. Overlapping writer commands are rejected by a process lock.

Queries, legacy aliases, and the controlled taxonomy live in [app/data/library.toml](app/data/library.toml). Luna handles title extraction and classification by default. Changing the model, prompt, topic names, comment, or supplied ancestor context invalidates its cache. Increment `PROMPT_VERSION` in `app/luna.py` for extraction changes and `PROCESSOR_VERSION` in `app/pipeline.py` for matching or aggregation semantics.

## Rebuild the latest thread with Luna

Set `OPENAI_API_KEY` as a secret in Coolify, `OPENAI_MODEL=gpt-6-luna`,
`EXTRACTION_BACKEND=luna`, and `HN_THREAD_IDS=49893157`, then deploy this code.
Disable any historical backfill task while focusing on this thread. Run in the
application container so the command operates on the persistent `/data` volume:

```bash
python -m app.rebuild --thread 49893157
python -m app.review --thread 49893157 --output /data/latest-thread-review.json
```

Locally, prefix commands with `uv run`; write the review under `data/`.
The rebuild creates a timestamped SQLite backup under `data/backups/` (or
`/data/backups/` in Docker), verifies its integrity, and builds the selected
thread in a separate `hn_books.thread-49893157.staging.db`. It discards old
canonical books and mentions, retaining external response caches. The live
library is replaced transactionally only after the entire fetched comment tree
and processing run finish without upstream failures. Readers can keep using the
old library during processing. Unresolved bibliographic identities are retained
for review; completing extraction does not imply every identity is verified.

Interrupted or failed runs leave the live library intact and the staging database
available for resume. Rerun the same command; cached Luna outputs, including empty
results, avoid repeated extraction charges. Add `--refresh` to fetch fresh HN
comments instead of reusing a complete staged tree. Each retry takes a new backup;
`--backup` can supply a unique destination and `--staging` an alternate path.
All writer commands share the live database lock during the rebuild.

Luna requests run concurrently (`LUNA_WORKERS=4`, configurable from 1 to 16).
Open Library verification remains sequential and rate limited. Cache writes and
comment updates stay on the main thread. Identical pending inputs share a request,
and successful in-flight outputs are cached if another request fails. The model,
prompt, and cache keys are unchanged, so previous staging work remains reusable.
Phase start/end logs and a heartbeat every 15 seconds identify backup, staging,
HN fetching, extraction/verification, and publishing. Each processed comment logs
progress with total, processed, and skipped counts; heartbeats indicate that the
process is alive, while increasing counts demonstrate completed work.

Luna receives each complete comment, its book links, the thread title/body, and
up to two ancestors (each capped at 6,000 characters). It extracts complete titles,
authors with stated/context/inferred provenance, exact evidence excerpts, per-book
sentiment and recommendation strength, and controlled topics. Reasoning is disabled
and strict JSON output is required. Unsupported evidence, invalid topics, invented
Open Library links, incomplete responses and API failures never fall back to regex
extraction. Authors inferred by Luna must still match Open Library; titles without
an author or explicit work link remain unresolved rather than matching an adaptation.
The authenticated unresolved endpoint and the review export include the model's
extraction data. Ingestion metrics record model requests, cache hits, and token usage.

Repeated normalized titles in a model response are combined into one mention per
comment after every original excerpt and link passes grounding validation. Original
variants remain in the extraction cache and in `extraction.duplicate_mentions` for
review. Conflicting sentiments become neutral; conflicting authors or work links
remain unresolved. Duplicate output alone does not trigger a model retry or stop
the rebuild.

## Backfill reading discussions

Preview the scope, pending thread count, and estimated comment volume without ingesting:

```bash
python -m app.ingest backfill --years 5 --dry-run
```

Ingest all matching Ask HN discussions from the last five years:

```bash
python -m app.ingest backfill --years 5
```

Run this inside the deployed container so it writes to the deployed SQLite volume. Locally, prefix commands with `uv run`. The initial metadata enrichment may take hours for a large corpus. Each thread's raw records commit before extraction, and its results become visible immediately. Repeating the same command skips threads with a complete tree and successful processing under the current processor version. Incomplete trees are retried; metadata failures resume from stored raw comments without fetching HN again. Unresolved bibliographic matches are retained and do not prevent a successfully processed thread from being checkpointed.

For smaller batches, add `--limit 20`. Repeat that command to process the next pending threads. `--dry-run` reports how many remain; ingestion logs persist `threads_remaining`, skipped threads, cached raw reuse, and cumulative processing metrics. `--refresh` explicitly refetches completed threads.

Specify an exact period or include other HN story discussions:

```bash
python -m app.ingest backfill --since 2021-09-30 --until 2026-10-01
python -m app.ingest backfill --years 5 --scope stories
```

`--since` is inclusive and `--until` is exclusive, in UTC. `--years` and `--since` are mutually exclusive. The default range is five calendar years ending now. The historical whitelist does not inject out-of-range threads; the separate daily command still uses `HN_THREAD_IDS`.

The broad `backfill_queries` in `app/data/library.toml` search title terms such as reading, books, and literature. The default scope is Ask HN; `--scope stories` includes all indexed HN story titles. Results are deduplicated by HN ID and processed oldest first. Queries that exceed Algolia's pagination capacity are recursively split into smaller date windows. Incomplete result pages fail explicitly rather than claiming a complete backfill. Coverage means all retrievable matches for these configured title queries in Algolia's index, not every potentially relevant HN discussion; unusual titles and unindexed/deleted threads can still be missed. Some title matches may be about reading code or documentation and yield no books.

For Coolify, create a task targeting `library` with command `python -m app.ingest backfill --years 5` and timeout **36000 seconds**, then use **Execute Now**. If it reaches the timeout, rerun the task to resume. You can temporarily use this as the daily ingestion task until `threads_remaining` reaches zero, then restore `python -m app.ingest` for daily refreshes. Keep one ingestion task active at a time to avoid competing for the writer lock.

## Configuration

Settings read environment variables and an optional local `.env`. `.env` and databases are ignored by Git. Start from [.env.example](.env.example); for local use, change `DATABASE_PATH` to `data/hn_books.db` or omit it.

| Variable | Default / purpose |
| --- | --- |
| `OPENAI_API_KEY` | Required secret for uncached Luna extraction |
| `OPENAI_MODEL` | `gpt-6-luna` |
| `LUNA_WORKERS` | 4 concurrent Luna requests; allowed range 1–16 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `EXTRACTION_BACKEND` | `luna`; `heuristic` is an explicit legacy comparison mode |
| `DATABASE_PATH` | `data/hn_books.db` locally; `/data/hn_books.db` in Docker |
| `HN_THREAD_IDS` | Comma-separated manual whitelist for daily ingestion |
| `HN_BASE_URL` | Official Firebase HN API base URL |
| `ALGOLIA_BASE_URL` | Algolia HN API base URL |
| `OPENLIBRARY_BASE_URL` | `https://openlibrary.org` |
| `HTTP_USER_AGENT` | `HNOpinionatedLibrary/0.1`; optionally add your contact address |
| `HTTP_TIMEOUT` | 30 seconds per request |
| `HN_FETCH_WORKERS` | 8 concurrent HN requests |
| `METADATA_INTERVAL` | 1.1 seconds minimum between uncached Open Library requests |
| `ADMIN_TOKEN` | Empty disables debug endpoints; set a secret to enable bearer access |
| `FRONTEND_PATH` | `frontend/dist` |

Open Library verifies book metadata. OpenAI Luna performs ingestion-time extraction and classification; serving the web app and recomputing rankings do not require an API key. Offline reprocessing uses cached Luna outputs; a missing extraction raises an error rather than silently falling back to heuristics.

## API

Interactive schemas are available at `/docs`.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | SQLite connectivity and initialized schema |
| `GET /api/stats` | Corpus totals and last successful processing run |
| `GET /api/books` | Ranked pagination: `page`, `page_size`, `sort`, `q`, repeated `tag` |
| `GET /api/search?q=books+about+databases` | FTS5 search over titles, authors, descriptions, HN tags and comment text |
| `GET /api/books/{id}` | Metadata, HN counts, score factors, tags, recommendation timeline |
| `GET /api/books/{id}/mentions` | Paginated HN evidence, sentiment, strength, original URLs |
| `GET /api/tags` | Controlled topics and book counts |
| `GET /api/threads` | Paginated source thread list |
| `GET /api/threads/{id}` | Stored thread and paginated plain-text comments |
| `GET /api/admin/unresolved-mentions` | Paginated uncertain spans and candidate match evidence |
| `GET /api/admin/ingestion-status` | Last 30 runs, status, metrics and failures |

Sort values: `all-time`, `recent`, `mentions`, `recommendations`. Multiple topic filters intersect. Search is lexical; it safely tokenizes input rather than accepting raw FTS syntax. Debug routes require `Authorization: Bearer <ADMIN_TOKEN>` and expose no destructive operations. Raw HN HTML is retained in SQLite; React displays plain text and never injects HN HTML.

## Docker and Coolify

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build -d
docker compose exec library python -m app.ingest thread 49893157
```

For local Docker access, [compose.local.yaml](compose.local.yaml) publishes `127.0.0.1:8000`. Set `LIBRARY_PORT=8080` before the command above if your local port 8000 is busy.

The image serves both the API and the built frontend on container port **8000**. It runs as UID **10001** and uses a persistent named volume at `/data`. Bind mounts must be writable by that UID. The database, WAL files, metadata cache and CLI lock all live in that directory.

For Coolify's **Docker Compose** build pack, use [compose.yaml](compose.yaml) alone. It exposes container port **8000** without reserving a host port. In **Domains for library**, enter `https://books.example.com:8000`, replacing the example hostname with your domain. The `:8000` suffix selects the internal port; visitors use `https://books.example.com`. Coolify generates the proxy routing. See its [Docker Compose guide](https://coolify.io/docs/applications/builds/docker-compose). After updating the repository, reload the Compose definition, confirm that `ports:` is absent, and redeploy.

For Coolify's **Dockerfile** build pack, use the root Dockerfile, set Ports Exposes to **8000**, and configure your domain. Add a named persistent volume with destination **`/data`**, and set **`DATABASE_PATH=/data/hn_books.db`**. Compose deployment already declares that volume. Coolify's [persistent storage guide](https://coolify.io/docs/core/persistent-storage/storage-mounts/overview) describes volume configuration.

In the running application's terminal, ingest your first thread:

```bash
python -m app.ingest thread 49893157
```

Create a [Coolify scheduled task](https://coolify.io/docs/core/automation/scheduled-tasks/create-a-task) with command **`python -m app.ingest`**, frequency **`0 3 * * *`**, and timeout **3600 seconds**. This refreshes the known corpus daily. For Compose, select service **`library`**. Execute the task once and inspect its output and stored ingestion-run record. Scheduling uses the deployment server's timezone. Large backfills should be run manually with a suitable timeout.

The application provides `/health` and the Docker image includes a health check. Keep the SQLite volume on the mini PC's local disk. For a consistent live database backup, use SQLite's backup API rather than copying the main database file while WAL writes are active:

```bash
docker compose exec library python -c "import sqlite3; source = sqlite3.connect('/data/hn_books.db'); target = sqlite3.connect('/data/hn_books.backup.db'); source.backup(target); target.close(); source.close()"
```

Copy that backup to your backup destination. Restoring requires stopping ingestion and the web process before replacing the database.

## Validation and limitations

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
uv run python -m app.evaluate
npm run format:check --prefix frontend
npm run build --prefix frontend
```

CI checks Python on 3.12, builds the frontend, and builds and starts the Docker image. Tests cover normalization, fuzzy matching, ambiguity, full trees including deleted parents, repeat ingestion, offline reprocessing, tag aggregation, recommendation independence, decay, API evidence, search and debug access.

The existing golden set measures the legacy heuristic baseline, not Luna accuracy.
New offline tests mock the Responses API to verify strict request shape, evidence
validation, cache invalidation, per-book classification persistence, metadata author
validation, and safe backup/rebuild/resume behavior. They do not measure a real
model's accuracy. Review the latest thread's exported results after the first paid
run, including unresolved identities and comments with no extracted books. Inferred
authors and ambiguous follow-up references still need scrutiny. HN comment scores
are unavailable from the official API, so ranking uses thread score and context length.

Only resolved mentions appear as library books. A detected title may remain unresolved because Open Library returned no matching work, the stated title/author disagrees with its metadata, or several works are equally plausible. Inspect the authenticated unresolved-mentions endpoint for these cases. An empty author-filtered lookup retries by title, while retaining the same title and author validation. After an extraction update, reprocess a stored thread with the command above, or reprocess all mentions. Wait for any active ingestion to finish first; writer commands share a lock.

See [docs/architecture.md](docs/architecture.md) for the inspectable ranking formula, stage boundaries, failure behavior and experiments to pursue next. Embeddings, semantic discovery, clustering, graph experiments, Polars/scikit-learn processing, personalized recommendations are deferred until the core corpus and evaluation justify them.
