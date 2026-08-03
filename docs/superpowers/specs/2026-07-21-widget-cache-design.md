# gh-widgets — incremental cache design

**Date:** 2026-07-21
**Status:** approved
**HTML:** `2026-07-21-widget-cache-design.html` · docs-hub `analyst/gh-widgets-cache-design`

## Context

`render.py` is stateless: every hourly run refetches a full year of contribution
calendar, every authored PR, and every authored issue. GitHub now rejects the
calendar query with `RESOURCE_LIMITS_EXCEEDED` on most runs, so the widgets
survive on intermittent luck.

Commit `5d66691` already split the calendar into its own request and added
transient retries; the retries are exhausting. The calendar-only query alone now
exceeds the node limit, so splitting further is not a fix.

## Decision

Persist a JSON cache. Treat only settled calendar days and `MERGED` PRs as
immutable; refetch everything mutable every run. Two layers:

1. **Durability** — a failed fetch renders from cache and exits 0, instead of
   exiting non-zero and leaving the widgets to rot.
2. **Reduction** — the calendar is fetched as a trailing 7-day window merged onto
   cached history; PRs are split so `MERGED` ones are never refetched.

The cutoff is **state, not age**, everywhere except the calendar.

## Verified API facts (tested, not assumed)

| Assumption | Reality |
|---|---|
| `CLOSED` is terminal | False. `IssueStateReason` enum contains `REOPENED`; a closed PR can reopen and later merge. Only `MERGED` is plausibly one-way. |
| PRs can be windowed by date | False. `pullRequests` accepts only `states, labels, headRefName, baseRefName, orderBy, first/last/after/before`. No date argument. |
| Calendar can be windowed | True. `contributionsCollection(from: DateTime, to: DateTime)` exists. |

## Cacheability (external contributions, widget's own token)

| Data | Cacheable | Why |
|---|---|---|
| Calendar days > 7d old | Yes, permanently | A past day's count cannot change |
| Calendar days ≤ 7d | No | Still moving; window is cheap |
| PRs `MERGED` | Yes, with weekly resync | Merge believed one-way; resync removes the need to be sure |
| PRs `CLOSED` | No | Reopenable, and can still merge afterwards |
| PRs `OPEN` | No | Mutable by definition |
| Issues, all states | No | `REOPENED` exists; `accepted` = CLOSED+COMPLETED can flip back |

Parity target: external opened and merged counts must match the live SVG's
values before this change.

## Changes

1. **`render.py` — cache module.** JSON load/save. Path from `CACHE_FILE`,
   default `/var/lib/gh-widgets/cache.json`. Schema-versioned. Missing or
   unreadable cache degrades to full fetch, never an error. Atomic write
   (temp + rename).
2. **`render.py` — durability layer.** Fetch failure after retries renders from
   cache, exits 0, and stamps the cache timestamp on the card so staleness is
   visible rather than silent.
3. **`render.py` — `fetch()`.** Calendar via `contributionsCollection(from:, to:)`
   for the trailing 7 days, merged onto cached days keyed by date.

3b. **`render.py` — cold-start backfill (amendment, 2026-07-21).** A cold cache
   has no history to merge, so the run must fetch a full year — which is the
   query that exceeds the node limit. Bootstrapping by luck is not acceptable,
   and every `--resync` re-enters the cold state. **On a cold cache, fetch the
   year in sequential `contributionsCollection(from:, to:)` monthly windows**
   rather than one full-year request, and merge them into the day map. Each
   window is far below the node limit, making bootstrap deterministic. The 7-day
   warm path is unchanged. Surfaced by the implementer during the first dispatch;
   the original spec omitted the bootstrap path entirely.
4. **`render.py` — `fetch_pull_requests()`.** Query `states:[OPEN, CLOSED]` live
   each run and union with the cached `MERGED` set. A PR entering `MERGED`
   appears in the live half until first seen merged, then moves to cache.
5. **`render.py` — `--resync` flag.** Discards cache, full refetch. Wired weekly
   so no correctness claim rests permanently on `MERGED` being one-way.
6. **`test_render.py`.** Cached-day merge equals full fetch; fetch failure renders
   from cache and exits 0; `OPEN → MERGED` counted exactly once; corrupt cache
   falls back. Stdlib `unittest`, no network.
7. **`render-gh-widgets.service`.** Add `StateDirectory=gh-widgets` and
   `CACHE_FILE` to the environment block.

## Verification

- [ ] `cd /root/gh-widgets && python3 -m unittest discover`
- [ ] Cold-cache render to scratch `OUT_DIR` → opened / merged counts match the pre-change SVG
- [ ] Warm-cache render → SVGs byte-identical to cold run apart from timestamp
- [ ] Invalid token → exits 0, renders from cache, card shows cache age
- [ ] Calendar request covers 7 days; no `RESOURCE_LIMITS_EXCEEDED`
- [ ] `install -m 755 render.py /usr/local/bin/render-gh-widgets.py`, then `diff`
      the two (deploy is a copied file under a different name; they have drifted
      silently before)
- [ ] Two consecutive scheduled runs green in `journalctl -u render-gh-widgets.service`

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Cold cache cannot bootstrap — the full-year calendar query is the failing one | HIGH | **Amended:** monthly windowed backfill on cold start (change 3b). Was unmitigated in v1 |
| `MERGED` not actually one-way (unverified — two doc pages failed to confirm) | MED | Weekly `--resync` bounds drift to 7 days; design does not depend on it |
| Vanished-from-OPEN/CLOSED treated as merged; a deleted PR or repo-gone-private misclassifies | LOW | Weekly `--resync` rebuilds the set; same bound as the `MERGED` assumption |
| Durability masks a permanently broken fetch | MED | Cache timestamp rendered on the card |
| Cache corrupt / partially written | LOW | Atomic write, schema version, degrade to full fetch |
| Repo/production drift | LOW | Explicit `diff` step before and after deploy |

## Out of scope

- **Caching issue outcomes** — `REOPENED` makes no issue outcome safe to freeze,
  and the issue set is one page.
- **Caching repo/star/language data** — `repositories(isFork: false)` is not the
  failing path and is small.
