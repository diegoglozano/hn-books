# Ranking validation

Version 2 makes independent reader support the primary signal. It replaces
user/thread/day contributions with each reader's latest non-neutral opinion per
book. Criticism reduces score, repeated posts cannot add votes, and a long list
or popular HN thread no longer makes its books stronger recommendations. The
[formula](architecture.md#ranking-version-2) and API score factors remain inspectable.
The detail page shows supportive opinions, critical readers and weighted support
and criticism. Historical recommendation counts remain separate from current
independent recommenders.

## Sensitivity on the latest thread

The comparison used the corrected thread 49893157 outputs published on October
2, 2026 at 23:50 UTC: 870 comments, 924 resolved mentions, 585 visible books,
and all 32 applied comment corrections. It found 34 books with resolved criticism
that version 1 ignored
and three books where selected positive contexts were reduced under the new
reader policy. Scores were compared at a fixed timestamp. These are behavior
checks on existing outputs, not independent relevance judgments.

| Book | Previous rank | Version 2 rank | Supportive readers | Critical readers |
| --- | ---: | ---: | ---: | ---: |
| Dungeon Crawler Carl | 1 | 1 | 11 | 1 |
| Lonesome Dove | 2 | 2 | 10 | 0 |
| The Count of Monte Cristo | 3 | 3 | 9 | 3 |
| Children of Time | 5 | 4 | 6 | 0 |
| Hyperion | 7 | 5 | 6 | 0 |
| The Road | 4 | 6 | 6 | 1 |
| Piranesi | 6 | 7 | 6 | 1 |

Dungeon Crawler Carl and Lonesome Dove remain the first two books. Dungeon
Crawler Carl has more supportive readers as well as one critic. Equal scores
use alphabetical title order; an alphabetical tie-break is not a relevance
judgment. Comparison with both the preceding uncorrected export and a local
correction simulation produced the same top seven under version 2. These checks
do not establish full-thread extraction accuracy or which ranking readers prefer.

## Regression checks

Tests verify that one reader posting across 30 threads/dates has the same score
as their latest single opinion; two independent moderate recommendations outrank
one strong endorsement. Independent criticism lowers score once per reader,
without allowing negative scores. A changed opinion replaces the old one,
neutral reading updates do not erase it, and contradictory timestamp ties abstain.

Further checks cover bounded unidentified votes, real usernames that resemble
the anonymous bucket, input-order stability, immunity to unrelated word count or
thread score, a maximum 25% breadth bonus, and decay of both support and criticism.
API tests verify that “Most recommended” ranks by distinct readers while “Most
mentioned” retains raw frequency, including stable alphabetical ties.

Deployment tests verify that stale stored scores upgrade on web startup without
changing comments, mentions, extraction cache or search entries. A repeat startup
does not rewrite current scores. A failure halfway through the upgrade rolls
back every book update. Ranking deployment needs no extra scheduled command or
model call. Source-bound extraction corrections still need the existing rebuild
task to publish corrected mentions.

## Remaining evaluation

The 0.5 criticism weight, quarter-weight unidentified vote, isolated-support
multiplier and breadth caps are explicit heuristic choices. They are not tuned
against held-out reader preferences. Next, finish source and sentiment review,
collect independent pairwise relevance judgments, and compare ranking versions
on the same verified corpus. Keep both all-time and recent views; do not infer
ranking quality from the numeric scale or selected examples alone.
