# Engaged reading-thread expansion

The reading command uses the explicit `reading_thread_ids` list in
[`app/data/library.toml`](../app/data/library.toml). Add or remove HN story IDs
there and deploy through a PR. Titles beside the IDs are maintenance notes;
there are no title patterns or search queries for this collection.

Eligibility defaults to the last **five years** and **more than 100 comments**
(101 inclusive). The command checks each ID against the official HN story
record, deduplicates IDs and processes eligible threads newest first. Dates and
counts are current values from HN, rather than Algolia's index. Deleted/dead
stories are skipped; missing records, invalid IDs and non-stories fail explicitly.
The separate broad `backfill` command retains its existing Algolia search.

## Curated expansion snapshot

Checked October 4, 2026. The list contains **43 eligible threads** spanning
December 2021 to September 2026, with **11,564 HN comments** in total. The two
existing threads have completed current-version checkpoints and are skipped;
the next ingestion therefore adds **41 threads**, estimated at **10,402 comments**.
Counts include replies and can differ from the fetched raw-tree size. Some
selected discussions also mention articles, blogs or courses; extraction still
keeps only identifiable books.

These include annual finished-book roundups, favorites, rereads, books that
changed readers' views, and general or subject-specific recommendations. The
list is curated coverage, not a claim to contain every eligible HN discussion.

| Thread | Date | HN comments | Status before this expansion |
| --- | --- | ---: | --- |
| [Ask HN: What are you reading?](https://news.ycombinator.com/item?id=49893157) | 2026-09-29 | 881 | Already processed |
| [Ask HN: What did you read in 2025?](https://news.ycombinator.com/item?id=46391572) | 2025-12-26 | 448 | Pending |
| [Ask HN: Books about people who did hard things](https://news.ycombinator.com/item?id=42614722) | 2025-01-06 | 431 | Pending |
| [Ask HN: What is the best thing you read in 2024?](https://news.ycombinator.com/item?id=42508087) | 2024-12-25 | 131 | Pending |
| [Ask HN: What were the best books you read this year?](https://news.ycombinator.com/item?id=42268570) | 2024-11-28 | 150 | Pending |
| [Ask HN: What book had a big impact on you as a child or teenager?](https://news.ycombinator.com/item?id=41759206) | 2024-10-06 | 153 | Pending |
| [Ask HN: What's the "best" book you've ever read?](https://news.ycombinator.com/item?id=41756432) | 2024-10-06 | 940 | Pending |
| [Ask HN: What books should I read to improve as a software engineer?](https://news.ycombinator.com/item?id=41387062) | 2024-08-29 | 123 | Pending |
| [Ask HN: What nonfiction books do you keep rereading?](https://news.ycombinator.com/item?id=40277933) | 2024-05-06 | 179 | Pending |
| [Ask HN: Good books on philosophy of engineering?](https://news.ycombinator.com/item?id=39057219) | 2024-01-19 | 132 | Pending |
| [Ask HN: Good book to learn modern networking?](https://news.ycombinator.com/item?id=38918418) | 2024-01-08 | 131 | Pending |
| [Ask HN: What are good books/blogs to read for a first time CTO?](https://news.ycombinator.com/item?id=38803092) | 2023-12-29 | 142 | Pending |
| [Ask HN: Books you read in 2023 and recommend for 2024?](https://news.ycombinator.com/item?id=38556188) | 2023-12-07 | 234 | Pending |
| [Ask HN: What are you reading these days?](https://news.ycombinator.com/item?id=38237730) | 2023-11-12 | 281 | Already processed |
| [Ask HN: Any interesting books you have read lately?](https://news.ycombinator.com/item?id=37156372) | 2023-08-17 | 520 | Pending |
| [Ask HN: What are your favorite sci-fi books?](https://news.ycombinator.com/item?id=36020597) | 2023-05-21 | 163 | Pending |
| [Ask HN: What are the most eye-opening textbooks you have ever read?](https://news.ycombinator.com/item?id=35929112) | 2023-05-13 | 169 | Pending |
| [Ask HN: What books helped you in your entrepreneurship journey?](https://news.ycombinator.com/item?id=35168647) | 2023-03-15 | 113 | Pending |
| [Ask HN: Math books that made you significantly better at math?](https://news.ycombinator.com/item?id=34439828) | 2023-01-19 | 317 | Pending |
| [Ask HN: Books that teach programming by building a series of small projects?](https://news.ycombinator.com/item?id=34412069) | 2023-01-17 | 203 | Pending |
| [Ask HN: Books you read in 2022 and recommend for 2023](https://news.ycombinator.com/item?id=34160611) | 2022-12-28 | 201 | Pending |
| [Ask HN: What is the best thing you read in 2022?](https://news.ycombinator.com/item?id=34055123) | 2022-12-19 | 258 | Pending |
| [Ask HN: Reading material on how to be a better software engineer?](https://news.ycombinator.com/item?id=33854815) | 2022-12-04 | 127 | Pending |
| [Ask HN: Best books read in 2022?](https://news.ycombinator.com/item?id=33849267) | 2022-12-04 | 249 | Pending |
| [Ask HN: Which books have made you a better thinker and problem solver?](https://news.ycombinator.com/item?id=33797862) | 2022-11-30 | 237 | Pending |
| [Ask HN: Do you recall any book or course that made a topic finally click?](https://news.ycombinator.com/item?id=33593631) | 2022-11-14 | 503 | Pending |
| [Ask HN: What are some of the best books you have read in 2022?](https://news.ycombinator.com/item?id=33381791) | 2022-10-29 | 278 | Pending |
| [Ask HN: Best book to learn C in 2022?](https://news.ycombinator.com/item?id=33130533) | 2022-10-08 | 160 | Pending |
| [Ask HN: Which books you have read till now that were worth investing time in?](https://news.ycombinator.com/item?id=32935412) | 2022-09-22 | 207 | Pending |
| [Ask HN: Which books do you consider real gems in your field of work/study?](https://news.ycombinator.com/item?id=32790064) | 2022-09-10 | 262 | Pending |
| [Ask HN: What book have you re-read 3x or more?](https://news.ycombinator.com/item?id=32712496) | 2022-09-04 | 117 | Pending |
| [Ask HN: Which book would you pick to re-read for the rest of your life?](https://news.ycombinator.com/item?id=31968169) | 2022-07-03 | 188 | Pending |
| [Ask HN: Best beginner friendly linear algebra book?](https://news.ycombinator.com/item?id=31707163) | 2022-06-11 | 126 | Pending |
| [Ask HN: Serious mathematics books that can replace a good teacher?](https://news.ycombinator.com/item?id=31488608) | 2022-05-24 | 168 | Pending |
| [Ask HN: Which book can attract anyone towards your field of study?](https://news.ycombinator.com/item?id=30822339) | 2022-03-27 | 273 | Pending |
| [Ask HN: What book changed your life?](https://news.ycombinator.com/item?id=30734709) | 2022-03-19 | 521 | Pending |
| [Ask HN: The book that did it for you in math and/or CS?](https://news.ycombinator.com/item?id=30485544) | 2022-02-27 | 205 | Pending |
| [Ask HN: What is one book you would recommend everyone to read?](https://news.ycombinator.com/item?id=30241190) | 2022-02-07 | 353 | Pending |
| [Ask HN: Best books on managing software complexity?](https://news.ycombinator.com/item?id=30228261) | 2022-02-06 | 124 | Pending |
| [Ask HN: What's the best book you read in 2021?](https://news.ycombinator.com/item?id=29668228) | 2021-12-24 | 631 | Pending |
| [Ask HN: Life Changing Books?](https://news.ycombinator.com/item?id=29605394) | 2021-12-18 | 223 | Pending |
| [Ask HN: I'm looking for a good book on the fundamentals of CS](https://news.ycombinator.com/item?id=29498220) | 2021-12-09 | 188 | Pending |
| [Ask HN: What are some must read books?](https://news.ycombinator.com/item?id=29462663) | 2021-12-06 | 124 | Pending |

The current counts can be checked with the [official HN API](https://github.com/HackerNews/API),
using `https://hacker-news.firebaseio.com/v0/item/<ID>.json`. Discovery checks make
no Luna requests. Ingestion records actual comment totals, cache reuse, model
request/token counts, unresolved mentions and failures.

## Earlier title-search snapshot

Checked October 3, 2026 against the [HN Search API](https://hn.algolia.com/api)
and the original HN story records. The earlier narrow title filter selected two
threads in the five-year interval:

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

The initial November 2023 addition had an estimated **281 comments**. These
are comment counts, not guaranteed book mentions, and discovery incurs no Luna
requests. A successful ingestion will record the actual tree size, cache reuse,
request/token counts, unresolved matches and failures.

## Run in Coolify

After deployment, use the existing task command (five years and 101+ comments
are defaults, so no flags are required):

```bash
python -m app.ingest reading
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
zero new Luna requests or failures. The completed November 2023 addition
subsequently brought the live library to 749 books across two threads. The
remaining 41 curated threads have not yet been ingested or independently reviewed.

The historical addition starts as model-processed source data. Processing success
and engagement do not establish extraction accuracy. Preserve the selected
regression results, label new-thread samples separately, and continue the
[source review](thread-review.md) without describing the enlarged corpus as
independently verified or treating curated regressions as a held-out benchmark.
The original [roadmap](next-steps.md) is retained unchanged.
