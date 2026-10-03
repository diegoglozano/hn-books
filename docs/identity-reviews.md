# Reviewed book identities

The latest-thread review found that exact titles can remain unresolved because
catalog names use a different script or a pseudonym. It also found author lists
containing editors/contributors, and prose-series references resolving to a comic.

[app/data/identity_reviews.json](../app/data/identity_reviews.json) stores explicit
bibliographic decisions with dates, reviewer provenance, reasons and primary source
links. These are Codex source checks, pending independent human review. They are
not a complete manual labeling of the thread or held-out extraction evaluation.

## Current decisions

| Decision | Sources |
| --- | --- |
| Match selected full-name variants of Fyodor Dostoevsky, Haruki Murakami and Cixin Liu across scripts | [Dostoevsky](https://openlibrary.org/authors/OL22242A.json), [Murakami](https://openlibrary.org/authors/OL382524A.json), [Liu](https://openlibrary.org/authors/OL7044246A.json) |
| Match Katherine Addison with Sarah Monette, while preserving the credited pen name for The Goblin Emperor | [Author biography](https://www.katherineaddison.com/bio), [catalog author](https://openlibrary.org/authors/OL1434466A.json) |
| Prefer reviewed work IDs for The Brothers Karamazov, 1Q84, The Goblin Emperor and The Dark Forest | Linked work records in the review file; [The Dark Forest publisher](https://us.macmillan.com/books/9780765377081/thedarkforest/) |
| Credit Donna Tartt on The Goldfinch; retain original extra catalog contributors in metadata | [Publisher](https://www.hachettebookgroup.com/titles/donna-tartt/the-goldfinch/9780316055437/) |
| Credit Donella Meadows as author and retain Diana Wright's editorial contribution in metadata on Thinking in Systems; accept its verified subtitle | [Publisher](https://chelseagreen.co.uk/book/thinking-in-systems/) |
| Prefer the original Anne of Green Gables novel by Lucy Maud Montgomery over duplicate, adapted and omnibus candidates | [Catalog](https://openlibrary.org/works/OL77746W.json), [publisher](https://www.penguinrandomhouse.com/books/557098/anne-of-green-gables-by-l-m-montgomery/9780143131854/) |
| Prefer Neil Gaiman's American Gods prose novel; retain explicit graphic-work links as separate identities | [Catalog](https://openlibrary.org/works/OL679360W.json), [author](https://neilgaiman.com/works/Books/American_Gods) |
| Match Charlie Munger with Charles T. Munger on Poor Charlie's Almanack; collapse duplicate author credits and preserve Peter D. Kaufman's editorial role in source metadata | [Catalog](https://openlibrary.org/works/OL8928012W.json), [publisher](https://press.stripe.com/poor-charlies-almanack) |
| Credit David Foster Wallace as novelist on The Pale King; retain Michael Pietsch's editorial contribution in original metadata | [Publisher](https://www.hachettebookgroup.com/titles/david-foster-wallace/the-pale-king/9780316175296/), [editor attribution](https://www.hachettebookgroup.com/hachette-book-group-news/hbg-big-news-this-week-september-17-21-2018/) |
| Credit Bruce I. Larrimer on Beyond Tube-and-Wing and accept its full subtitle; preserve additional catalog contributor credits | [NASA](https://www.nasa.gov/aeronautics/beyond-tube-and-wing/), [book title page](https://www.nasa.gov/wp-content/uploads/2020/11/beyond_tube-and-wing_tagged.pdf) |
| Credit Patrick Radden Keefe on Say Nothing while preserving additional catalog contributor credits in original metadata | [Publisher](https://www.penguinrandomhouse.com/books/90837/say-nothing-by-patrick-radden-keefe/) |
| Keep implicit James S. A. Corey references to The Expanse unresolved instead of assigning the prose series to the comic work | [Novels](https://www.hachettebookgroup.com/series/james-s-a-corey/the-expanse/), [graphic novel](https://www.boom-studios.com/archives/the-expanse-origins-original-graphic-novel-debuts-in-february-2018/) |

The last exclusion is scoped to one reviewed work and author identity. An actual
grounded work link or specific comic-writer credits can still identify the graphic
novel. Generic graphic subjects are insufficient to reject a record: Open Library
sometimes mixes adaptation subjects into an original novel's work record.

Reviewed work preferences require an extracted author and an exact reviewed title
or title alias. The selected provider work must still have a reviewed title and
usable metadata. Alias matching does not invent missing authors, transliterate
unknown names, collapse distinct volumes, or accept an unrelated same-titled work.
Full-name aliases are deliberately selected rather than importing every alternate
name in a provider's occasionally noisy author record.

## Persistence and reprocessing

The decisions live in Git and are applied whenever stored comments are processed.
They survive staged rebuilds, because the staged database reads the same review
file. The review digest is included in the processing version and canonical metadata;
changing a decision invalidates processing and prevents reuse of stale canonical
rows. The model prompt and extraction cache keys remain unchanged.

Original search documents, work records and source comment excerpts are retained.
Canonical metadata includes `identity_review` and `identity_review_digest`.
Rejected candidates carry the exclusion reason and source links in the authenticated
review export. An unresolved candidate never contributes a library recommendation;
reprocessing rebuilds the rankings and search index after removing the false match.

To add a decision, verify independent bibliographic sources, record its scope and
reason, add a source-linked regression case, and deploy through a PR. After deployment,
execute the existing `python -m app.rebuild --thread 49893157` Coolify task. It takes
a backup and uses cached extraction results while applying these matching decisions.

There is no public correction endpoint. A separate
[source-bound comment correction workflow](comment-reviews.md) handles missed
mentions, rejection and classification changes. This identity file covers
bibliographic aliases, selected work mappings, credited authors and scoped exclusions.

## Validation scope

[tests/fixtures/reviewed_identity_cases.json](../tests/fixtures/reviewed_identity_cases.json)
contains 13 source-linked mention-level metadata regressions. They test the
confirmed identity decisions on saved candidate metadata. Integration tests cover
subtitle lookup, original contributor preservation, cached reprocessing, and explicit
comic links/credits, Munger's nickname, duplicate author records, and catalog-order
independence. These checks measure behavior on curated identity cases, not
full-thread mention precision or recall. Continue the [complete-thread review](thread-review.md)
before extending ingestion.

The October 3 review also verified the existing catalog matches for Outdoor Kids
in an Inside World ([work](https://openlibrary.org/works/OL24784465W.json)) and
A Drop of Corruption ([work](https://openlibrary.org/works/OL42983060W.json)). The
104-comment regression set then had 32 checked identities and ten pending checks.
The pre-correction baseline was recomputed against these expanded identity labels
using the same saved source and prediction snapshot; its eight omissions and
38 false entries are unchanged.

A local projection through the normal resolver, using saved search responses and
checked work/author metadata, resolves six previously unresolved mentions across
the three newly preferred works. It preserves each mention's sentiment, strength
and source evidence. The projection matches all 32 checked identities and author
credits in the selected regression set, with no network or model requests.
The October 3 production export verified all six repairs and all 32 checked
identities after the Coolify rebuild. The live library contains 588 books,
930 resolved and 503 unresolved mentions; all 588 public scores match the corrected
source data. This establishes the repairs, not full-thread accuracy.

The next 30-comment batch expands the regression set to 134 comments and 60
checked identities, with 19 pending checks and four deliberately unresolved
adaptation/edition identities. It also verifies author-versus-contributor roles
on The Pale King, Beyond Tube-and-Wing and Say Nothing. New integration cases
check that original multi-contributor metadata survives the primary-author
correction and that verified records can be reused without network requests.
Separate tests ensure that unspecified manga creators cannot become prose works.
These changes need the next Coolify rebuild; the current projection is an offline
check, not proof that the new batch has been applied in production.
