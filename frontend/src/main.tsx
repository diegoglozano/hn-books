import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/dm-sans/600.css";
import "@fontsource/dm-sans/700.css";
import "@fontsource/libre-caslon-text/400.css";
import "@fontsource/libre-caslon-text/400-italic.css";
import {
  StrictMode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowDownWideNarrow,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Library,
  MessageSquare,
  Search,
  SlidersHorizontal,
  Users,
  X,
} from "lucide-react";
import {
  api,
  type Book,
  type Mention,
  type Page,
  type Stats,
  type Tag,
  type Thread,
} from "./api";
import "./styles.css";

const label = (name: string) =>
  name === "ai" ? "AI" : name.replaceAll("-", " ");
const number = (n: number) => n.toLocaleString();
const date = (timestamp: number) =>
  new Date(timestamp * 1000).toLocaleDateString(undefined, {
    month: "short",
    year: "numeric",
  });

function Cover({ book, large = false }: { book: Book; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const colors = [
    "#28483f",
    "#b66544",
    "#314659",
    "#74613f",
    "#784044",
    "#3d5960",
  ];
  return (
    <div
      className={`cover ${large ? "large" : ""}`}
      style={{ backgroundColor: colors[book.id % colors.length] }}
    >
      {!loaded && (
        <div className="cover-type">
          <span className="cover-rule" />
          <strong>{book.canonical_title}</strong>
          <span>{book.authors[0]}</span>
          <BookOpen size={22} strokeWidth={1} />
        </div>
      )}
      {book.cover_url && !failed && (
        <img
          src={book.cover_url}
          alt={`Cover of ${book.canonical_title}`}
          loading="lazy"
          className={loaded ? "loaded" : ""}
          onLoad={() => setLoaded(true)}
          onError={() => {
            setFailed(true);
            setLoaded(false);
          }}
        />
      )}
    </div>
  );
}

function Pagination({
  page,
  total,
  pageSize,
  onPage,
}: {
  page: number;
  total: number;
  pageSize: number;
  onPage: (page: number) => void;
}) {
  const pages = Math.ceil(total / pageSize);
  if (pages < 2) return null;
  return (
    <div className="pagination">
      <button disabled={page === 1} onClick={() => onPage(page - 1)}>
        <ChevronLeft size={16} /> Previous
      </button>
      <span>
        Page {page} of {pages}
      </span>
      <button disabled={page === pages} onClick={() => onPage(page + 1)}>
        Next <ChevronRight size={16} />
      </button>
    </div>
  );
}

function BookCard({ book, rank }: { book: Book; rank: number }) {
  return (
    <a href={`#/books/${book.id}`} className="book-card">
      <div className="book-art">
        <span className="rank">{String(rank).padStart(2, "0")}</span>
        <Cover book={book} />
        <span className="art-link">
          <ArrowRight size={18} />
        </span>
      </div>
      <div className="book-info">
        <div className="book-topic">
          {label(book.tags[0]?.name ?? "From the HN bookshelf")}
        </div>
        <h3>{book.canonical_title}</h3>
        <p className="author">
          {book.authors.join(", ") || "Author unavailable"}
        </p>
        <div className="card-evidence">
          <Users size={14} />
          <strong>{book.independent_recommenders}</strong>
          <span>recommenders</span>
          <span className="evidence-dot">·</span>
          <MessageSquare size={13} />
          <span>{book.mention_count}</span>
        </div>
      </div>
    </a>
  );
}

function BookFeed({
  params,
  active,
  filtered,
  onClear,
  onTotal,
}: {
  params: string;
  active: boolean;
  filtered: boolean;
  onClear: () => void;
  onTotal: (total: number) => void;
}) {
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<Page<Book> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const pending = useRef(false);
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    pending.current = true;
    setLoading(true);
    setError("");
    const timer = window.setTimeout(
      () => {
        api<Page<Book>>(
          `/books?${params}&page=${page}&page_size=12`,
          controller.signal,
        )
          .then((data) => {
            if (controller.signal.aborted) return;
            setResult((previous) => {
              const items = page === 1 ? [] : (previous?.items ?? []);
              const seen = new Set(items.map((book) => book.id));
              return {
                ...data,
                items: [
                  ...items,
                  ...data.items.filter((book) => {
                    if (seen.has(book.id)) return false;
                    seen.add(book.id);
                    return true;
                  }),
                ],
              };
            });
            onTotal(data.total);
            setHasMore(
              data.items.length > 0 && data.page * data.page_size < data.total,
            );
            pending.current = false;
            setLoading(false);
          })
          .catch((e) => {
            if (!controller.signal.aborted) {
              setError((e as Error).message);
              pending.current = false;
              setLoading(false);
            }
          });
      },
      page === 1 ? 220 : 0,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [params, page, retry, onTotal]);
  const loadMore = useCallback(() => {
    if (pending.current || loading || error || !hasMore) return;
    pending.current = true;
    setLoading(true);
    setPage((current) => current + 1);
  }, [loading, error, hasMore]);
  useEffect(() => {
    if (
      !active ||
      loading ||
      error ||
      !hasMore ||
      !sentinel.current ||
      !("IntersectionObserver" in window)
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMore();
      },
      { rootMargin: "400px 0px" },
    );
    observer.observe(sentinel.current);
    return () => observer.disconnect();
  }, [active, loading, error, hasMore, loadMore]);
  const retryLoading = () => {
    if (pending.current) return;
    pending.current = true;
    setLoading(true);
    setRetry((current) => current + 1);
  };
  if (!result && error)
    return (
      <div className="empty-state" role="alert">
        <h3>Couldn’t load the bookshelf</h3>
        <p>{error}</p>
        <button onClick={retryLoading}>Try again</button>
      </div>
    );
  if (!result)
    return (
      <div className="book-grid" aria-label="Loading books" aria-busy="true">
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="skeleton-card">
            <div />
            <span />
            <span />
          </div>
        ))}
      </div>
    );
  if (!result.items.length)
    return (
      <div className="empty-state">
        <BookOpen size={40} strokeWidth={1} />
        <div className="eyebrow">A GOOD LIBRARY STARTS SOMEWHERE</div>
        <h3>
          {filtered
            ? "No books on this shelf yet."
            : "Your next great read is waiting."}
        </h3>
        <p>
          {filtered
            ? "Try a different idea or clear your topic filters."
            : "Ingest a reading thread to turn its conversations into your first collection."}
        </p>
        {filtered ? (
          <button onClick={onClear}>Clear filters</button>
        ) : (
          <a className="primary-button" href="#/about">
            Start your library <ArrowRight size={16} />
          </a>
        )}
      </div>
    );
  return (
    <>
      <div className="book-grid" aria-label="Books" aria-busy={loading}>
        {result.items.map((book, i) => (
          <BookCard key={book.id} book={book} rank={i + 1} />
        ))}
      </div>
      <div
        ref={sentinel}
        className="feed-status"
        aria-live="polite"
        aria-atomic="true"
      >
        {error ? (
          <>
            <p role="alert">Couldn’t load more books. {error}</p>
            <button onClick={retryLoading}>Try again</button>
          </>
        ) : loading ? (
          <p>Loading more books…</p>
        ) : hasMore ? (
          <button onClick={loadMore}>Load more books</button>
        ) : (
          <p>You’ve reached the end of the bookshelf.</p>
        )}
        <span>
          {number(result.items.length)} of {number(result.total)} books
        </span>
      </div>
    </>
  );
}

function LibraryView({
  tags,
  stats,
  active,
}: {
  tags: Tag[];
  stats: Stats | null;
  active: boolean;
}) {
  const [query, setQuery] = useState("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [sort, setSort] = useState("all-time");
  const [total, setTotal] = useState<number | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const params = new URLSearchParams({ q: query, sort });
  selectedTags.forEach((tag) => params.append("tag", tag));
  const feedKey = params.toString();
  useEffect(() => setTotal(null), [feedKey]);
  function toggleTag(name: string) {
    setSelectedTags((prev) =>
      prev.includes(name) ? prev.filter((t) => t !== name) : [...prev, name],
    );
  }
  const activeTags = tags.filter((tag) => (tag.book_count ?? 0) > 0);
  return (
    <>
      <section className="hero">
        <div className="eyebrow">
          <span /> THE HACKER NEWS BOOKSHELF
        </div>
        <h1>
          Good books.
          <br />
          <em>Real conversations.</em>
        </h1>
        <p>
          A library shaped by curious people.
          <br className="mobile-break" /> Discover the books Hacker News keeps
          coming back to.
        </p>
        <div className="hero-footer">
          <div className="hero-note">
            <span className="hn-mini">Y</span>
            <span>Every book has a conversation behind it.</span>
          </div>
          <a href="#/about">
            How this library works <ArrowRight size={16} />
          </a>
        </div>
        <div className="hero-decoration" aria-hidden="true">
          <div className="drawn-book book-one">
            <span>
              STAY
              <br />
              CURIOUS
            </span>
          </div>
          <div className="drawn-book book-two">
            <span>
              READ
              <br />
              DEEPER.
            </span>
          </div>
          <div className="drawn-book book-three">
            <BookOpen size={36} strokeWidth={1} />
          </div>
          <span className="shelf" />
        </div>
      </section>
      <section className="library-section">
        <div className="section-heading">
          <div>
            <div className="eyebrow">THE COLLECTION</div>
            <h2>
              Find your next rabbit hole<span>.</span>
            </h2>
          </div>
          <span className="collection-count">
            {number(total ?? stats?.books ?? 0)} books, many perspectives
          </span>
        </div>
        <div className="search-toolbar">
          <label className="search-box">
            <Search size={20} />
            <input
              aria-label="Search the library"
              placeholder="Search books, authors, or ideas…"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
              }}
            />
            {query && (
              <button
                aria-label="Clear search"
                onClick={() => {
                  setQuery("");
                }}
              >
                <X size={17} />
              </button>
            )}
          </label>
          <button
            className="filter-toggle"
            onClick={() => setFiltersOpen(!filtersOpen)}
          >
            <SlidersHorizontal size={18} /> Topics
          </button>
          <label className="sort-control">
            <ArrowDownWideNarrow size={17} />
            <select
              aria-label="Sort books"
              value={sort}
              onChange={(e) => {
                setSort(e.target.value);
              }}
            >
              <option value="all-time">All-time favorites</option>
              <option value="recent">Recent favorites</option>
              <option value="mentions">Most mentioned</option>
              <option value="recommendations">Most recommended</option>
            </select>
          </label>
        </div>
        <div className="collection-layout">
          <aside className={`topic-sidebar ${filtersOpen ? "open" : ""}`}>
            <div className="sidebar-heading">
              EXPLORE BY TOPIC <SlidersHorizontal size={13} />
            </div>
            <button
              className={`topic-option ${!selectedTags.length ? "selected" : ""}`}
              onClick={() => {
                setSelectedTags([]);
              }}
            >
              <span>
                <Library size={15} /> All books
              </span>
              <span>{stats?.books ?? 0}</span>
            </button>
            {activeTags.map((tag) => (
              <button
                key={tag.name}
                className={`topic-option ${selectedTags.includes(tag.name) ? "selected" : ""}`}
                onClick={() => toggleTag(tag.name)}
              >
                <span>{label(tag.name)}</span>
                <span>
                  {selectedTags.includes(tag.name) ? (
                    <Check size={13} />
                  ) : (
                    tag.book_count
                  )}
                </span>
              </button>
            ))}
            <div className="sidebar-note">
              <MessageSquare size={20} strokeWidth={1.5} />
              <p>Recommendations with receipts.</p>
              <span>Read the original comments. Make up your own mind.</span>
            </div>
          </aside>
          <div className="collection-main">
            {selectedTags.length > 0 && (
              <div className="selected-filters">
                {selectedTags.map((tag) => (
                  <button key={tag} onClick={() => toggleTag(tag)}>
                    {label(tag)} <X size={12} />
                  </button>
                ))}
              </div>
            )}
            <div className="results-heading">
              <span>
                {query ? `Results for “${query}”` : "The community’s bookshelf"}
              </span>
              <span>
                <span className="status-dot" /> HN recommendations
              </span>
            </div>
            <BookFeed
              key={feedKey}
              params={feedKey}
              active={active}
              filtered={Boolean(query || selectedTags.length)}
              onClear={() => {
                setQuery("");
                setSelectedTags([]);
              }}
              onTotal={setTotal}
            />
          </div>
        </div>
      </section>
    </>
  );
}

function BookDetail({ id }: { id: number }) {
  const [book, setBook] = useState<Book | null>(null);
  const [mentions, setMentions] = useState<Page<Mention> | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api<Book>(`/books/${id}`, controller.signal),
      api<Page<Mention>>(
        `/books/${id}/mentions?page=${page}`,
        controller.signal,
      ),
    ])
      .then(([b, m]) => {
        setBook(b);
        setMentions(m);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      });
    return () => controller.abort();
  }, [id, page]);
  if (error)
    return (
      <div className="empty-state" role="alert">
        <h2>{error}</h2>
        <a href="#/">Return to the library</a>
      </div>
    );
  if (!book)
    return (
      <div className="empty-state" aria-busy="true">
        Opening the book…
      </div>
    );
  return (
    <div className="detail-page">
      <a className="back-link" href="#/">
        <ArrowLeft size={16} /> Back to the library
      </a>
      <section className="detail-intro">
        <div className="detail-cover">
          <Cover book={book} large />
        </div>
        <div>
          <div className="eyebrow">ON THE HN BOOKSHELF</div>
          <h1>{book.canonical_title}</h1>
          <p className="detail-author">
            {book.authors.join(", ")}{" "}
            {book.publication_year && <span>· {book.publication_year}</span>}
          </p>
          <div className="tag-pills">
            {book.tags.map((tag) => (
              <span key={tag.name}>{label(tag.name)}</span>
            ))}
          </div>
          <p className="description">
            {book.description ||
              "Open Library has no description for this work yet. The HN conversations below tell their own story."}
          </p>
          <a
            className="external-link"
            href={`https://openlibrary.org${book.openlibrary_id}`}
            target="_blank"
            rel="noreferrer"
          >
            Book metadata from Open Library <ExternalLink size={14} />
          </a>
        </div>
      </section>
      <div className="detail-stats">
        <div>
          <strong>{book.independent_recommenders}</strong>
          <span>independent recommenders</span>
        </div>
        <div>
          <strong>{book.mention_count}</strong>
          <span>HN mentions</span>
        </div>
        <div>
          <strong>{book.thread_count}</strong>
          <span>conversations</span>
        </div>
        <div>
          <strong>{book.all_time_score.toFixed(1)}</strong>
          <span>all-time HN score</span>
        </div>
      </div>
      <div className="detail-columns">
        <section>
          <div className="eyebrow">THE RECOMMENDATIONS, IN THEIR OWN WORDS</div>
          <h2>
            Behind the bookshelf<span>.</span>
          </h2>
          <p className="section-description">
            The original context. The thoughtful praise. The occasional
            disagreement.
          </p>
          {mentions?.items.map((mention) => (
            <article className="mention" key={mention.id}>
              <div className="mention-heading">
                <span className="avatar">
                  {(mention.author || "?")[0].toUpperCase()}
                </span>
                <strong>{mention.author || "Deleted user"}</strong>
                <span>{date(mention.hn_created_at)}</span>
                <span
                  className={`sentiment ${mention.sentiment > 0 ? "positive" : ""}`}
                >
                  {mention.sentiment < 0
                    ? "Critical"
                    : mention.recommendation_strength >= 0.6
                      ? "Recommended"
                      : "Mentioned"}
                </span>
              </div>
              <p className="comment-text">{mention.context_text}</p>
              <div className="mention-footer">
                <a href={mention.thread_url} target="_blank" rel="noreferrer">
                  {mention.thread_title}
                </a>
                <a href={mention.hn_url} target="_blank" rel="noreferrer">
                  Original comment <ExternalLink size={13} />
                </a>
              </div>
            </article>
          ))}
          {mentions && (
            <Pagination
              page={page}
              total={mentions.total}
              pageSize={20}
              onPage={setPage}
            />
          )}
        </section>
        <aside className="detail-aside">
          <div className="eyebrow">RECOMMENDATION HISTORY</div>
          <h3>A lasting conversation</h3>
          <div className="timeline">
            {book.timeline?.map((point) => (
              <div key={point.month}>
                <span>{point.month}</span>
                <div className="timeline-track">
                  <i
                    style={{
                      width: `${Math.max(6, (point.mentions / Math.max(...(book.timeline ?? []).map((p) => p.mentions))) * 100)}%`,
                    }}
                  />
                </div>
                <strong>{point.mentions}</strong>
              </div>
            ))}
          </div>
          <p>Mentions by month, from the original HN timestamps.</p>
          <hr />
          <h3>A score you can inspect</h3>
          <p>
            {(book.score_details.formula_version ?? 1) >= 2
              ? "Each reader’s latest opinion counts once. Strong recommendations add weight; criticism reduces it. Support from several readers counts more than an isolated endorsement."
              : "Positive recommendations count once per user, thread, and day. Scores reward independent contexts and explanations."}
          </p>
          <dl>
            <dt>Supporting opinions</dt>
            <dd>{book.score_details.independent_contexts}</dd>
            {(book.score_details.formula_version ?? 1) >= 2 && (
              <>
                <dt>Critical readers</dt>
                <dd>{book.score_details.negative_users ?? 0}</dd>
                <dt>Support weight</dt>
                <dd>{(book.score_details.positive_weight ?? 0).toFixed(2)}</dd>
                <dt>Criticism weight</dt>
                <dd>{(book.score_details.negative_weight ?? 0).toFixed(2)}</dd>
              </>
            )}
            <dt>Recent score</dt>
            <dd>{book.recent_score.toFixed(1)}</dd>
          </dl>
          <p>
            Recent scores use a gentle 365-day half-life. Read the source
            comments to judge each recommendation.
          </p>
        </aside>
      </div>
    </div>
  );
}

function ThreadsView() {
  const [result, setResult] = useState<Page<Thread> | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api<Page<Thread>>(`/threads?page=${page}`, controller.signal)
      .then(setResult)
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      });
    return () => controller.abort();
  }, [page]);
  return (
    <section className="simple-page">
      <div className="eyebrow">THE SOURCE MATERIAL</div>
      <h1>
        It starts with a conversation<span>.</span>
      </h1>
      <p className="section-description">
        The reading threads behind your library. Every comment tree is preserved
        locally.
      </p>
      {error && <p role="alert">{error}</p>}
      {!result && !error ? (
        <p>Loading conversations…</p>
      ) : result?.items.length ? (
        <>
          {result.items.map((thread) => (
            <a
              className="thread-row"
              href={`https://news.ycombinator.com/item?id=${thread.id}`}
              target="_blank"
              rel="noreferrer"
              key={thread.id}
            >
              <MessageSquare size={22} />
              <div>
                <h3>{thread.title}</h3>
                <p>
                  {thread.author} · {date(thread.created_at)} · {thread.score}{" "}
                  points · {thread.stored_comments} stored comments
                </p>
              </div>
              <ExternalLink size={17} />
            </a>
          ))}
          <Pagination
            page={page}
            total={result.total}
            pageSize={20}
            onPage={setPage}
          />
        </>
      ) : (
        !error && (
          <div className="empty-state">
            <MessageSquare size={35} strokeWidth={1} />
            <h3>The conversation hasn’t started yet.</h3>
            <p>Add your first reading thread to populate the library.</p>
            <a className="primary-button" href="#/about">
              Get started <ArrowRight size={16} />
            </a>
          </div>
        )
      )}
    </section>
  );
}

function AboutView() {
  return (
    <section className="simple-page about-page">
      <div className="eyebrow">A SMALL LIBRARY WITH A LONG MEMORY</div>
      <h1>
        Books with a conversation
        <br />
        behind them<span>.</span>
      </h1>
      <p className="about-lead">
        Some of the best book recommendations are buried in a comment thread.
        This library gives them a shelf.
      </p>
      <div className="about-grid">
        <article>
          <span className="step-number">01</span>
          <h3>Follow the discussion</h3>
          <p>
            Reading threads from Hacker News become a local corpus. Original
            comments, authors, timestamps, and links stay attached to every
            book.
          </p>
        </article>
        <article>
          <span className="step-number">02</span>
          <h3>Keep the evidence</h3>
          <p>
            Books resolve to Open Library works. Uncertain matches are held for
            inspection. Bibliographic metadata stays separate from what HN
            readers say.
          </p>
        </article>
        <article>
          <span className="step-number">03</span>
          <h3>Reward independent voices</h3>
          <p>
            Repeated replies count less than independent recommendations.
            Positive, neutral, and critical mentions are distinguished with an
            inspectable baseline.
          </p>
        </article>
      </div>
      <div className="setup-note">
        <BookOpen size={27} strokeWidth={1.5} />
        <div>
          <h3>Start with one reading thread</h3>
          <p>From your application container or local checkout, run:</p>
          <code>uv run python -m app.ingest thread &lt;HN_ID&gt;</code>
          <p>
            Then reload the library. Schedule <code>python -m app.ingest</code>{" "}
            daily to refresh known threads. See the repository README for setup
            and deployment.
          </p>
        </div>
      </div>
      <p className="about-footnote">
        No accounts. No runtime LLM. Just a small, self-hosted library, and the
        conversations that make it worth exploring.
      </p>
    </section>
  );
}

function App() {
  const [hash, setHash] = useState(window.location.hash);
  const [stats, setStats] = useState<Stats | null>(null);
  const [tags, setTags] = useState<Tag[]>([]);
  const libraryScroll = useRef(0);
  const currentView = useRef("library");
  useEffect(() => {
    const handler = () => {
      if (currentView.current === "library")
        libraryScroll.current = window.scrollY;
      setHash(window.location.hash);
    };
    window.addEventListener("hashchange", handler);
    const controller = new AbortController();
    api<Stats>("/stats", controller.signal)
      .then(setStats)
      .catch(() => {});
    api<Tag[]>("/tags", controller.signal)
      .then(setTags)
      .catch(() => {});
    return () => {
      window.removeEventListener("hashchange", handler);
      controller.abort();
    };
  }, []);
  const detail = hash.match(/^#\/books\/(\d+)$/);
  const view =
    hash === "#/threads"
      ? "threads"
      : hash === "#/about"
        ? "about"
        : detail
          ? "detail"
          : "library";
  useLayoutEffect(() => {
    currentView.current = view;
    window.scrollTo(0, view === "library" ? libraryScroll.current : 0);
  }, [view]);
  return (
    <>
      <header>
        <div className="header-inner">
          <a href="#/" className="brand">
            <span className="brand-mark">Y</span>
            <span>
              HN <strong>Library</strong>
              <small>OPINIONATED BY THE COMMUNITY</small>
            </span>
          </a>
          <nav aria-label="Main navigation">
            <a
              className={
                view === "library" || view === "detail" ? "active" : ""
              }
              href="#/"
            >
              <Library size={16} /> Library
            </a>
            <a className={view === "threads" ? "active" : ""} href="#/threads">
              <MessageSquare size={16} /> Conversations
            </a>
            <a className={view === "about" ? "active" : ""} href="#/about">
              About <ArrowRight size={14} />
            </a>
          </nav>
          <span className="header-note">
            <span className="status-dot" /> From Hacker News
          </span>
        </div>
      </header>
      <main>
        <div hidden={view !== "library"}>
          <LibraryView tags={tags} stats={stats} active={view === "library"} />
        </div>
        {view === "detail" && detail ? (
          <BookDetail key={detail[1]} id={Number(detail[1])} />
        ) : view === "threads" ? (
          <ThreadsView />
        ) : view === "about" ? (
          <AboutView />
        ) : null}
      </main>
      <footer>
        <a href="#/" className="footer-brand">
          <span className="hn-mini">Y</span> A little less scrolling. A little
          more reading.
        </a>
        <span>
          Built from HN conversations · Metadata by{" "}
          <a href="https://openlibrary.org" target="_blank" rel="noreferrer">
            Open Library <ExternalLink size={11} />
          </a>
        </span>
      </footer>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
