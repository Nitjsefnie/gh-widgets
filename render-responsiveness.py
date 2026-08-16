#!/usr/bin/env python3
"""
gh-widgets — render the self-hosted "External Responsiveness" SVG.

Writes ONE SVG to OUT_DIR:
  responsiveness.svg   one ranked table of the external repos where we do
                       high-volume work AND get fast turnaround: per repo,
                       n = PRs authored by the account that are merged or
                       still open, and t = the TRIMMED-MEAN hours they waited
                       — to the merge, or to NOW for one still open. Score:
                       n**gamma * 1/(1 + t/half_life) with gamma 0.5 and
                       half_life 24 h — i.e. volume with diminishing
                       returns, damped by how long a typical PR sits.
                       The score is DISPLAYED rescaled so the leader reads
                       SCORE_MAX (10.00) and the rest keep their ratios to
                       it; the ranking and the bars are unaffected.

This renderer OWNS its data. It fetches the account's authored pull requests
itself — one cheap paginated GraphQL connection, the same
common.fetch_pull_requests the other two renderers use — and MERGES the result
into render-impact.py's cache, which is the shared home of that PR set.
It used to read that cache read-only, which pinned it to render-impact.py's
twice-daily schedule: the card could never be fresher than a job it did not
depend on. Nothing here needs render-impact.py's expensive machinery (the
per-repo git-blame walk is what makes THAT script slow), so nothing here
should wait for it. Runs hourly.

Merging, not overwriting, is the whole hazard of writing to a cache someone
else owns. This script replaces the PR half and preserves every other key
byte-for-byte — above all `ourloc`, the git-blame result render-impact.py
cannot cheaply rebuild. common.merge_cache does the load-modify-write inside
one cache_lock hold so a concurrent render-impact save cannot be discarded,
and it writes via a temp file plus os.replace so a partial cache is never
observable. render-impact.py's own `fetched_at` is deliberately left alone:
it stamps THAT script's fetch, and moving it would under-report how old
`ourloc` is. This script stamps `prs_fetched_at` instead.

Merged PRs are frozen in that cache, so a cache written before their
timestamps existed yields nodes with no createdAt/mergedAt until
`render-impact.py --resync` refetches them (the weekly resync timer does).
Those PRs are counted as skipped and reported on stdout, rather than quietly
shortening a repo's history.

Configuration (env vars or CLI flags, in that order of precedence):
  GH_USER     (required) GitHub username
  GH_TOKEN    (required) Personal access token with `public_repo`. `read:user`
              is NOT needed: identity comes from login + databaseId + orgs.
              (can also be read from a file via --token-file or GH_TOKEN_FILE)
  OUT_DIR     where to write the SVG (default: ./widgets)
  CACHE_FILE  the PR cache, shared with render-impact.py
              (default: /var/lib/gh-widgets/impact-cache.json)
  THEME       tokyonight (default) | catppuccin | gruvbox | github-dark

  GH_EXTRA_INSIDERS  extra owner logins to treat as ours (comma-separated)
  RESP_GAMMA         volume exponent                (default 0.5)
  RESP_HALF_LIFE_H   hours at which speed hits 0.5  (default 24.0)
  RESP_MIN_PRS       PRs needed to be ranked         (default 3)

Who counts as external is DERIVED from the token's own account (login + orgs +
GH_EXTRA_INSIDERS) via common.fetch_identity, exactly as render-impact.py
derives it — so a cache carrying no insider set is no longer a hard error, it
is simply not consulted. Only the degraded path below still reads the cached
set. GH_EXTRA_INSIDERS must match render-impact.py's, since both write the
derived set into the same cache key.

Fails gracefully, same contract as the other two renderers: a failed fetch
renders from the cache, prints `fetch failed; rendered ... from cache`, leaves
the cache untouched, and exits 0. With no usable cache to fall back on it
exits non-zero and the previously rendered SVG keeps serving.

Deps: Python stdlib only. Requires Python 3.9+.
"""
import argparse
import importlib.util
import os
import sys
import urllib.error
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_common():
    """Load ghwidgets_common.py from beside this script — see render.py."""
    path = Path(__file__).resolve().with_name("ghwidgets_common.py")
    if not path.exists():
        raise SystemExit(
            f"error: render-responsiveness.py cannot find its "
            f"ghwidgets_common.py at {path} (install.sh copies all of them; "
            f"a partial copy is not usable)")
    spec = importlib.util.spec_from_file_location("ghwidgets_common", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load_common()

REQUIRED_COMMON = 3
common.check_version(REQUIRED_COMMON)

# The cache is shared with render-impact.py; this is the schema version THAT
# script writes, since it writes the sections this one does not. Pinned (and
# tested) rather than accepted blindly: writing a newer layout's keys into an
# older one would corrupt it instead of failing.
IMPACT_CACHE_VERSION = 1
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/impact-cache.json"

TOP_N = 10
STALE_AFTER_H = 24        # cache older than this earns the stamp

# Re-exported so call sites stay short and patchable, same as render-impact.py.
FONT = common.FONT
THEMES = common.THEMES
gql = common.gql
xml_escape = common.xml_escape
base_card = common.base_card
stamp_cache_notice = common.stamp_cache_notice

Row = namedtuple("Row", "raw n hours repo")
Scored = namedtuple("Scored", "score n hours repo")


def metric_knobs():
    """The ranking knobs, read from the environment at call time.

    Defaults are the values validated when the metric was designed; they are
    configurable so a run can be re-scored without editing source, NOT because
    they are expected to change. An unparseable value aborts the run rather
    than silently reverting to the default — see common.env_float.
    """
    return {
        "gamma":     common.env_float("RESP_GAMMA", 0.5),
        "half_life": common.env_float("RESP_HALF_LIFE_H", 24.0),
        "min_prs":   int(common.env_float("RESP_MIN_PRS", 3)),
    }


def fetch_pull_requests(token, login, cached_prs=None, max_pages=50):
    """Thin wrapper over the shared implementation, passing THIS module's
    `gql` so a test patch of it still intercepts the calls."""
    return common.fetch_pull_requests(token, login, cached_prs, max_pages,
                                      gql_fn=gql)


def fetch_prs(token, user, cached_prs):
    """Resolve identity and fetch the account's authored PRs.

    Identity first, and from the token's own account: the insider set decides
    which repos count as external, and deriving it here is what lets this
    script run without waiting for render-impact.py to write one into the
    cache. Returns (insiders, prs, prs_by_id) — the keyed mapping is what gets
    persisted, matching what render-impact.py stores under the same key.
    """
    me = common.fetch_identity(token, user, gql_fn=gql)
    prs, prs_by_id = fetch_pull_requests(token, me.login, cached_prs)
    return me.insiders, prs, prs_by_id


def parse_ts(s):
    """ISO-8601 from the API ('2026-08-01T12:00:00Z') -> datetime.

    fromisoformat only learned to read the trailing Z in 3.11, and this repo
    supports 3.9+, so the offset is spelled out.
    """
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def turnaround_hours(node, now):
    """Hours the PR waited: creation to merge, or creation to `now` if it is
    still open. None if the cache cannot say.

    An open PR is counted at its CURRENT age on purpose. Scoring merges alone
    measures only the requests that were eventually answered, so a repo that
    never answers at all looks perfect — it has no slow merges — while one
    that answers late looks worse than it is. A PR still sitting there is the
    repo's turnaround, measured so far, and it lengthens on every render until
    somebody acts on it.

    closedAt stands in for a missing mergedAt: for a merged PR they are the
    same instant, and the cache can hold a node that was last fetched while
    the PR was still open (fetch_pull_requests infers the merge from the PR
    leaving the OPEN/CLOSED result set, which leaves both timestamps unset
    until the next MERGED sweep or --resync).
    """
    created = node.get("createdAt")
    if not created:
        return None
    if node.get("merged"):
        end = node.get("mergedAt") or node.get("closedAt")
        if not end:
            return None
        end = parse_ts(end)
    else:
        end = now
    hours = (end - parse_ts(created)).total_seconds() / 3600
    return max(hours, 0.0)  # clock skew must not manufacture a negative wait


def turnaround_by_repo(prs, insiders, now=None):
    """{repo: [hours, ...]} over merged AND still-open PRs in external repos,
    plus the count of those the cache carries no usable timestamps for.

    A PR that is closed without merging is neither: it was answered, with a
    no, and its wait says nothing about how long this repo leaves work
    hanging.

    The skipped count is returned rather than swallowed: it is the difference
    between "this repo is slow" and "we do not know yet", and the card says
    which it is.
    """
    now = now or datetime.now(timezone.utc)
    by_repo = {}
    skipped = 0
    for node in prs:
        live = node.get("state") == "OPEN" and not node.get("merged")
        if not (node.get("merged") or live):
            continue
        if not common.is_external(node, insiders):
            continue
        hours = turnaround_hours(node, now)
        if hours is None:
            skipped += 1
            continue
        by_repo.setdefault(node["repository"]["nameWithOwner"], []).append(hours)
    return by_repo, skipped


def _percentile(sorted_values, q):
    """Linear-interpolation percentile on a sorted sequence.

    q is in [0, 1].  Equivalent to numpy.percentile(..., method="linear"):
    index = (n - 1) * q, then interpolate between the neighbouring elements.
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile of empty sequence")
    if n == 1:
        return sorted_values[0]
    idx = (n - 1) * q
    low = int(idx)
    high = low + 1
    if high >= n:
        return sorted_values[-1]
    weight = idx - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def trimmed_mean(values):
    """Arithmetic mean of values inside the p10..p90 range (inclusive).

    Falls back to the plain arithmetic mean of all values when the list has
    fewer than 4 values or when the trim would select an empty set.
    """
    if len(values) < 4:
        return sum(values) / len(values)
    sorted_values = sorted(values)
    p10 = _percentile(sorted_values, 0.1)
    p90 = _percentile(sorted_values, 0.9)
    included = [v for v in sorted_values if p10 <= v <= p90]
    if not included:
        return sum(values) / len(values)
    return sum(included) / len(included)


def volume(n, knobs):
    """PR count with diminishing returns, mirroring the live-code table's
    gamma in render-impact.py: the 30th PR in a repo says less about the
    relationship than the 3rd did. Open PRs count here too, but they drag
    their own weight: each one enters the turnaround at its full current age,
    and the speed damping falls off far faster than gamma 0.5 climbs."""
    return n ** knobs["gamma"]


def speed(hours, knobs):
    """Turnaround damping: 1.0 for an instant merge, 0.5 at the half-life
    (24 h), 0.25 at three times it. Hyperbolic rather than exponential so a
    genuinely slow repo still scores something instead of vanishing."""
    return 1.0 / (1.0 + hours / knobs["half_life"])


def responsiveness_rows(by_repo, knobs):
    """Rank repos by volume(n) * speed(trimmed-mean turnaround).

    The turnaround is a TRIMMED MEAN over the p10..p90 range, deliberately.
    It keeps the bulk of PRs that define the typical experience while
    discarding the worst holiday-weekend outliers that dominate a plain mean.
    For very small samples (fewer than 4 PRs) it falls back to the plain mean
    so the trim never collapses to a single value or an empty set.

    Repos below the floor are not ranked at all: a summary over one or two
    samples is not an estimate of anything. Returns (rows, excluded_repos,
    excluded_prs) so the card can disclose the tail instead of hiding it.
    """
    rows, excluded_repos, excluded_prs = [], 0, 0
    for repo, hours in by_repo.items():
        if len(hours) < knobs["min_prs"]:
            excluded_repos += 1
            excluded_prs += len(hours)
            continue
        t = trimmed_mean(hours)
        rows.append(Row(volume(len(hours), knobs) * speed(t, knobs),
                        len(hours), t, repo))
    # Ties: more merged PRs first, then repo name, so the order is total and
    # a re-render cannot reshuffle equal rows.
    rows.sort(key=lambda r: (-r.raw, -r.n, r.repo))
    return rows, excluded_repos, excluded_prs


def rescale(rows):
    """Restate the raw scores so the leader reads exactly SCORE_MAX and the
    rest keep their ratios to it — the same display convention as the impact
    card's sections."""
    if not rows:
        return []
    best = rows[0].raw or 1
    return [Scored(r.raw / best * SCORE_MAX, r.n, r.hours, r.repo)
            for r in rows]


def fmt_duration(hours):
    """Human-readable turnaround: '12 min' / '3.1 h' / '2.4 d'.

    Each boundary is decided on the ROUNDED value, so the card never prints
    '60 min' or '24.0 h' — the unit changes exactly when the number would.
    """
    minutes = round(hours * 60)
    if minutes < 60:
        return f"{minutes} min" if minutes else "<1 min"
    if round(hours, 1) < 24:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} d"


CARD_W = 520
COL_N = 300        # merged + still-open PRs, right-anchored
COL_TIME = 390     # mean wait, right-anchored
COL_SCORE = 500    # responsiveness score, right-anchored
BAR_X = 20
BAR_MAX_W = CARD_W - 2 * BAR_X  # rating bar at 100% = top score
SCORE_MAX = 10.0  # displayed score of the leader; the rest scale to it
REPO_CHARS = 34  # owner/name truncation budget
ROW_H = 22


def truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def render_rows(C, y, scored):
    parts = [
        f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="10">repo</text>'
        f'<text x="{COL_N}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">PRs</text>'
        f'<text x="{COL_TIME}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">avg wait</text>'
        f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">responsiveness</text>']
    y += 17
    if not scored:
        parts.append(f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="11">'
                     f'no external PRs yet</text>')
        return parts, y + 16
    for row in scored:
        bar_w = max(row.score / SCORE_MAX * BAR_MAX_W, 2)
        parts.append(
            f'<text x="20" y="{y}" fill="{C["fg"]}" font-size="11">'
            f'{xml_escape(truncate(row.repo, REPO_CHARS))}</text>'
            f'<text x="{COL_N}" y="{y}" text-anchor="end" fill="{C["cyan"]}" font-size="11">{row.n}</text>'
            f'<text x="{COL_TIME}" y="{y}" text-anchor="end" fill="{C["gold"]}" font-size="11">{fmt_duration(row.hours)}</text>'
            f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{C["green"]}" font-size="11" font-weight="500">{row.score:.2f}</text>'
            # rating bar: length = score normalized to the leader (row #1 =
            # full width), same Dota-hero-stats style as the impact card
            f'<rect x="{BAR_X}" y="{y + 5}" width="{BAR_MAX_W}" height="3" rx="1.5" fill="{C["border"]}" fill-opacity="0.35"/>'
            f'<rect x="{BAR_X}" y="{y + 5}" width="{bar_w:.1f}" height="3" rx="1.5" fill="{C["green"]}"/>')
        y += ROW_H
    return parts, y


def render_responsiveness(C, scored):
    """The card is a leaderboard, so it shows the leaders and nothing else.

    The floor and the top-N cut are deliberately NOT disclosed here: this is a
    personal stat card, "top N" is self-evident, and the counts belong in the
    run's output where an operator looks, not on the graphic. They are printed
    by main().
    """
    y = 88
    body = f"""
  <text x="20" y="34" fill="{C['gold']}" font-size="14" font-weight="600">external responsiveness</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">how fast my work lands outside my own account</text>
  <line x1="20" y1="64" x2="{CARD_W - 20}" y2="64" stroke="{C['border']}"/>"""
    parts, y = render_rows(C, y, scored)
    body += "\n  " + "\n  ".join(parts)
    # No closing rule: the card ends on its last row, like impact.svg. A rule
    # here separated the rows from a footer that no longer exists.
    return base_card(C, CARD_W, y + 10, body)


def parse_args():
    p = argparse.ArgumentParser(
        description="Render the self-hosted External Responsiveness SVG.")
    p.add_argument("--user", default=os.environ.get("GH_USER"),
                   help="GitHub username (env: GH_USER)")
    p.add_argument("--token", default=os.environ.get("GH_TOKEN"),
                   help="GitHub PAT (env: GH_TOKEN)")
    p.add_argument("--token-file", default=os.environ.get("GH_TOKEN_FILE"),
                   help="Read token from a file (env: GH_TOKEN_FILE)")
    p.add_argument("--out-dir", default=os.environ.get("OUT_DIR", "./widgets"),
                   help="Where to write the SVG (env: OUT_DIR)")
    p.add_argument("--theme", default=os.environ.get("THEME", "tokyonight"),
                   choices=list(THEMES),
                   help="Color theme (env: THEME)")
    p.add_argument("--cache-file",
                   default=os.environ.get("CACHE_FILE", DEFAULT_CACHE_FILE),
                   help="JSON cache path, shared with render-impact.py "
                        "(env: CACHE_FILE)")
    args = p.parse_args()

    if not args.user:
        p.error("--user (or env GH_USER) is required")
    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        p.error("--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required")
    return args, token


def read_impact_cache(path, user):
    """(prs, insiders, fetched_at) from the cache, or None when the cache
    cannot stand in — for the DEGRADED path only, a run whose fetch failed.

    None rather than an error of its own: the caller re-raises the FETCH's
    exception, which says what actually went wrong (HTTP 401, a timeout)
    instead of burying it under a complaint about the cache. Exiting non-zero
    leaves the last good SVG in place, same contract as the other renderers.

    A cache with no insider set is usable, though it once was not. That was
    fatal back when the cached set was the ONLY way this script could know who
    is an insider; now identity is fetched from the token on every successful
    run, and a fetch failure is not a reason to refuse to render. It degrades
    to the account itself for one render — which can over-report externals
    until the next successful run repairs it — exactly as render-impact.py
    does on the same path.
    """
    cache = common.load_cache(path, IMPACT_CACHE_VERSION)
    if not cache or not cache.get("prs"):
        return None
    insiders = (frozenset(cache["insiders"]) if cache.get("insiders")
                else common.insider_set(user, []))
    return (list(cache["prs"].values()), insiders,
            cache.get("prs_fetched_at") or cache.get("fetched_at"))


def update_pr_cache(path, insiders, prs_by_id):
    """Merge the freshly fetched PR half into the shared cache.

    ONLY the keys this script owns are written; `issues`, `totals` and above
    all `ourloc` — the git-blame result render-impact.py produces — are carried
    through untouched by common.merge_cache, which does the whole read-modify-write
    under the cache lock.

    `fetched_at` is one of the keys left alone: it is render-impact.py's stamp
    for the sections THIS script does not refresh, and moving it hourly would
    make a stale `ourloc` look fresh. The PR half gets its own stamp.
    """
    return common.merge_cache(path, IMPACT_CACHE_VERSION, {
        "prs_fetched_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "insiders": sorted(insiders),
        "prs": prs_by_id,
    })


def cache_is_stale(fetched_at, now=None):
    """True when the cache is old enough that the card should say so.

    Reached only on the degraded path, where the card is drawn from the last
    cached fetch. The PR data refreshes hourly now, so a cache past a day
    means roughly a day of consecutive failed runs — a real outage rather than
    the single blip a stamp would only add noise to.
    """
    if not fetched_at:
        return False
    try:
        stamped = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    now = now or datetime.now(stamped.tzinfo)
    return (now - stamped) > timedelta(hours=STALE_AFTER_H)


def build_card(C, prs, insiders, knobs):
    """Score the PRs and render the card.

    Returns (svg, notes) — `notes` is what the card deliberately does not say,
    carried out to the run's stdout instead.
    """
    by_repo, skipped = turnaround_by_repo(prs, insiders)  # ages open PRs to now
    rows, excluded_repos, excluded_prs = responsiveness_rows(by_repo, knobs)
    scored = rescale(rows)[:TOP_N]
    notes = {"shown": len(scored), "ranked": len(rows),
             "excluded_repos": excluded_repos, "excluded_prs": excluded_prs,
             "skipped": skipped, "min_prs": knobs["min_prs"]}
    return render_responsiveness(C, scored), notes


def load_inputs(args, token):
    """Fetch the PRs (updating the shared cache) or, on failure, recover them
    from that cache. Returns (prs, insiders, stale), where `stale` is the
    cached fetch time when rendering from cache and None after a live fetch.
    """
    cache = common.load_cache(args.cache_file, IMPACT_CACHE_VERSION)
    try:
        insiders, prs, prs_by_id = fetch_prs(token, args.user,
                                             cache.get("prs") or None)
    except Exception:
        # Durability layer: a failed fetch (after gql's retries) renders from
        # the cache and exits 0 — but only with a usable cache. Without one,
        # exiting non-zero with the fetch's own error is still correct. The
        # cache is never written on this path: what it holds is the only data
        # there is.
        cached = read_impact_cache(args.cache_file, args.user)
        if cached is None:
            raise
        prs, insiders, stale = cached
    else:
        update_pr_cache(args.cache_file, insiders, prs_by_id)
        stale = None
    return prs, insiders, stale


def main():
    args, token = parse_args()

    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    prs, insiders, stale = load_inputs(args, token)
    svg, notes = build_card(C, prs, insiders, metric_knobs())
    if stale and cache_is_stale(stale):
        # The stamp is a CAVEAT, not a label: it appears only when the data is
        # old enough to mislead. A successful hourly fetch carries no stamp at
        # all, and neither does one blip recovered from a cache written an
        # hour ago.
        svg = stamp_cache_notice(C, svg, stale)
    (out / "responsiveness.svg").write_text(svg)

    if stale:
        print(f"fetch failed; rendered {out}/responsiveness.svg from cache "
              f"(fetched_at={stale}, showing {notes['shown']} of "
              f"{notes['ranked']} ranked)")
    else:
        print(f"wrote {out}/responsiveness.svg "
              f"(showing {notes['shown']} of {notes['ranked']} ranked)")
    # What the card does not say, said here instead: an operator checking a
    # thin-looking board needs to know whether repos fell below the floor or
    # simply had no cached merge time.
    if notes["excluded_repos"]:
        print(f"  {plural(notes['excluded_repos'], 'repo')} "
              f"({plural(notes['excluded_prs'], 'PR')}) below the "
              f"n >= {notes['min_prs']} floor")
    if notes["skipped"]:
        print(f"  {plural(notes['skipped'], 'merged PR')} skipped: no cached "
              f"merge time (run render-impact.py --resync)")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}",
              file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
