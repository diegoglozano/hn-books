# HN Opinionated Library

A small, self-hosted bookshelf built from Hacker News reading discussions. Every book keeps its original recommendation evidence: comments, usernames, timestamps, thread titles, and links back to HN.

This implements the **first meaningful milestone**, with a ranked React library, book details, topic filters, FTS5 search, and opt-in thread discovery. Ingestion and reprocessing run as CLI commands using the same image as the web app.

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node 22 with npm.

```bash
uv sync --frozen
npm ci --prefix frontend
npm run build --prefix frontend
uv run python -m app.ingest thread 21457827
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. The example is [Ask HN: What Are You Reading?](https://news.ycombinator.com/item?id=21457827). The first ingestion makes cached Open Library lookups and may take a few minutes. Nothing is seeded automatically; books come from your chosen threads. The default database is `data/hn_books.db`.

For frontend development, run `npm run dev --prefix frontend` in a second terminal and open Vite's printed URL. Vite proxies API requests to port 8000. No CORS configuration or separate production frontend service is needed.

## Commands

Run these from the repository root, prefixed by `uv run` locally. Inside the Docker image, `python` already uses the installed application environment.

| Command | Behavior |
| --- | --- |
| `python -m app.ingest thread <HN_ID>` | Fetch one complete comment tree, persist raw records, process mentions, rebuild aggregates |
| `python -m app.ingest` | Refresh configured and already stored threads; suitable for daily scheduling |
| `python -m app.ingest discover` | Discover the first Algolia result page for each configured Ask HN query, then ingest |
| `python -m app.ingest backfill` | Traverse all available Algolia pages for configured queries, deduplicate IDs, then ingest |
| `python -m app.reprocess mentions` | Re-extract stored comments; reuse local books/cache and look up missing metadata |
| `python -m app.reprocess mentions --offline` | Re-extract with no network calls, using local books and cached metadata only |
| `python -m app.reprocess tags` | Reclassify stored mentions, then rebuild scores and search |
| `python -m app.recompute rankings` | Rebuild scores and FTS5 index |
| `python -m app.evaluate` | Print golden extraction, matching, tag, and strength metrics offline |

Daily ingestion deliberately starts with an explicit corpus. Historical discovery is opt-in. Algolia backfill covers the pages returned by its search API, not a guaranteed exhaustive archive of HN. A partial run exits nonzero, logs failures, and preserves fetched raw data for retry. Overlapping writer commands are rejected by a process lock.

Queries, curated title/author aliases, and the controlled taxonomy live in [app/data/library.toml](app/data/library.toml). Add an alias and reprocess mentions to improve a recurring unresolved title. Increment `PROCESSOR_VERSION` in `app/pipeline.py` when changing extraction/classification semantics so normal ingestion reprocesses unchanged raw comments.

## Configuration

Settings read environment variables and an optional local `.env`. `.env` and databases are ignored by Git. Start from [.env.example](.env.example); for local use, change `DATABASE_PATH` to `data/hn_books.db` or omit it.

| Variable | Default / purpose |
| --- | --- |
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

Open Library is the implemented metadata source. Google Books and LLM providers are deferred, so no API keys or model calls are required.

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
docker compose exec library python -m app.ingest thread 21457827
```

For local Docker access, [compose.local.yaml](compose.local.yaml) publishes `127.0.0.1:8000`. Set `LIBRARY_PORT=8080` before the command above if your local port 8000 is busy.

The image serves both the API and the built frontend on container port **8000**. It runs as UID **10001** and uses a persistent named volume at `/data`. Bind mounts must be writable by that UID. The database, WAL files, metadata cache and CLI lock all live in that directory.

For Coolify's **Docker Compose** build pack, use [compose.yaml](compose.yaml) alone. It exposes container port **8000** without reserving a host port. In **Domains for library**, enter `https://books.example.com:8000`, replacing the example hostname with your domain. The `:8000` suffix selects the internal port; visitors use `https://books.example.com`. Coolify generates the proxy routing. See its [Docker Compose guide](https://coolify.io/docs/applications/builds/docker-compose). After updating the repository, reload the Compose definition, confirm that `ports:` is absent, and redeploy.

For Coolify's **Dockerfile** build pack, use the root Dockerfile, set Ports Exposes to **8000**, and configure your domain. Add a named persistent volume with destination **`/data`**, and set **`DATABASE_PATH=/data/hn_books.db`**. Compose deployment already declares that volume. Coolify's [persistent storage guide](https://coolify.io/docs/core/persistent-storage/storage-mounts/overview) describes volume configuration.

In the running application's terminal, ingest your first thread:

```bash
python -m app.ingest thread 21457827
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

The golden set includes 15 constructed comments, four source-linked HN comments, and six constructed canonicalization cases. Its metrics detect regressions; they do **not** establish representative accuracy. Extraction and sentence-based classification are conservative heuristics. Lowercase quoted phrases stay unresolved; ambiguity and sarcasm need more labeled data. HN comment scores are unavailable from the official API, so the baseline uses thread score and context length.

See [docs/architecture.md](docs/architecture.md) for the inspectable ranking formula, stage boundaries, failure behavior and experiments to pursue next. Embeddings, semantic discovery, clustering, graph experiments, Polars/scikit-learn processing, personalized recommendations and model-based classification are deferred until the core corpus and evaluation justify them.
