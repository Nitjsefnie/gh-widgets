#!/usr/bin/env python3
"""
gh-widgets — render the self-hosted "External Responsiveness" SVG.

Writes ONE SVG to OUT_DIR:
  responsiveness.svg   one ranked table of the external repos where we do
                       high-volume work AND get fast turnaround: per repo,
                       n = merged PRs authored by the account and t = the
                       MEDIAN hours from PR creation to merge. Score:
                       n**gamma * 1/(1 + t/half_life) with gamma 0.5 and
                       half_life 24 h — i.e. volume with diminishing
                       returns, damped by how long a typical PR sits.
                       The score is DISPLAYED rescaled so the leader reads
                       SCORE_MAX (10.00) and the rest keep their ratios to
                       it; the ranking and the bars are unaffected.

This renderer does NOT talk to GitHub. It reads render-impact.py's cache,
which already carries every authored PR keyed by id, and every one of them
already carries the repo, its privacy flag, its owner, and (since
COMMON_VERSION 2) createdAt/mergedAt. Adding two immutable fields to a query
that was already being made costs nothing; a second crawl of the same PRs
would cost a full extra pass over the account's history for data we already
have. So: no token, no network, no cache of its own — run it after
render-impact.py.

Merged PRs are frozen in that cache, so a cache written before COMMON_VERSION 2
yields nodes with no timestamps until `render-impact.py --resync` refetches
them (the weekly resync timer does). Those PRs are counted as skipped and
reported on stdout, rather than quietly shortening a repo's history.

Configuration (env vars or CLI flags, in that order of precedence):
  OUT_DIR     where to write the SVG (default: ./widgets)
  CACHE_FILE  render-impact.py's cache, read-only
              (default: /var/lib/gh-widgets/impact-cache.json)
  THEME       tokyonight (default) | catppuccin | gruvbox | github-dark

  RESP_GAMMA        volume exponent                (default 0.5)
  RESP_HALF_LIFE_H  hours at which speed hits 0.5  (default 24.0)
  RESP_MIN_PRS      merged PRs needed to be ranked (default 3)

Who counts as external is NOT decided here: the cache stores the insider set
render-impact.py derived from the token's own account (login + orgs +
GH_EXTRA_INSIDERS), and this script reuses common.is_external against it. A
cache with no insider set is a hard error rather than a guess — guessing
"just the account" would count our own orgs' repos as external.

A missing, corrupt, or schema-mismatched cache is a hard error too: there is
nothing to fall back to and no way to fetch, so exiting non-zero leaves the
previously rendered SVG serving, same contract as the other renderers.

Deps: Python stdlib only. Requires Python 3.9+.
"""
import argparse
import importlib.util
import os
import statistics
import sys
from collections import namedtuple
from datetime import datetime, timedelta
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

REQUIRED_COMMON = 2
common.check_version(REQUIRED_COMMON)

# The cache belongs to render-impact.py; this is the schema version THAT
# script writes. Pinned (and tested) rather than accepted blindly: reading a
# newer layout would render wrong numbers instead of failing.
IMPACT_CACHE_VERSION = 1
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/impact-cache.json"

TOP_N = 10
STALE_AFTER_H = 24        # cache older than this earns the stamp

# Re-exported so call sites stay short and patchable, same as render-impact.py.
FONT = common.FONT
THEMES = common.THEMES
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


def parse_ts(s):
    """ISO-8601 from the API ('2026-08-01T12:00:00Z') -> datetime.

    fromisoformat only learned to read the trailing Z in 3.11, and this repo
    supports 3.9+, so the offset is spelled out.
    """
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def turnaround_hours(node):
    """Hours from PR creation to merge, or None if the cache cannot say.

    closedAt stands in for a missing mergedAt: for a merged PR they are the
    same instant, and the cache can hold a node that was last fetched while
    the PR was still open (fetch_pull_requests infers the merge from the PR
    leaving the OPEN/CLOSED result set, which leaves both timestamps unset
    until the next MERGED sweep or --resync).
    """
    created = node.get("createdAt")
    end = node.get("mergedAt") or node.get("closedAt")
    if not created or not end:
        return None
    hours = (parse_ts(end) - parse_ts(created)).total_seconds() / 3600
    return max(hours, 0.0)  # clock skew must not manufacture a negative wait


def turnaround_by_repo(prs, insiders):
    """{repo: [hours, ...]} over MERGED PRs in external repos, plus the count
    of merged external PRs the cache carries no usable timestamps for.

    The skipped count is returned rather than swallowed: it is the difference
    between "this repo is slow" and "we do not know yet", and the card says
    which it is.
    """
    by_repo = {}
    skipped = 0
    for node in prs:
        if not node.get("merged") or not common.is_external(node, insiders):
            continue
        hours = turnaround_hours(node)
        if hours is None:
            skipped += 1
            continue
        by_repo.setdefault(node["repository"]["nameWithOwner"], []).append(hours)
    return by_repo, skipped


def volume(n, knobs):
    """Merged-PR count with diminishing returns, mirroring the live-code
    table's gamma in render-impact.py: the 30th PR in a repo says less about
    the relationship than the 3rd did."""
    return n ** knobs["gamma"]


def speed(hours, knobs):
    """Turnaround damping: 1.0 for an instant merge, 0.5 at the half-life
    (24 h), 0.25 at three times it. Hyperbolic rather than exponential so a
    genuinely slow repo still scores something instead of vanishing."""
    return 1.0 / (1.0 + hours / knobs["half_life"])


def responsiveness_rows(by_repo, knobs):
    """Rank repos by volume(n) * speed(median turnaround).

    The turnaround is a MEDIAN, deliberately. Measured over the 332 external
    merged PRs on 2026-08-03: mean 24.3 h, median 3.2 h, sigma 4.9 days, max
    77 days — 83% merge inside a day. A mean therefore ranks a repo by its
    single worst outlier (one PR parked over a holiday) rather than by what
    a contributor should expect, which is the whole question this card asks.

    Repos below the floor are not ranked at all: a median over one or two
    samples is not an estimate of anything. Returns (rows, excluded_repos,
    excluded_prs) so the card can disclose the tail instead of hiding it.
    """
    rows, excluded_repos, excluded_prs = [], 0, 0
    for repo, hours in by_repo.items():
        if len(hours) < knobs["min_prs"]:
            excluded_repos += 1
            excluded_prs += len(hours)
            continue
        t = statistics.median(hours)
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
COL_N = 300        # merged PRs, right-anchored
COL_TIME = 390     # median turnaround, right-anchored
COL_SCORE = 500    # responsiveness score, right-anchored
BAR_X = 20
BAR_MAX_W = CARD_W - 2 * BAR_X  # rating bar at 100% = top score
SCORE_MAX = 10.0  # displayed score of the leader; the rest scale to it
REPO_CHARS = 34  # owner/name truncation budget (~225px at font-size 11 mono)
ROW_H = 22


def truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def render_rows(C, y, scored):
    parts = [
        f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="10">repo</text>'
        f'<text x="{COL_N}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">merged</text>'
        f'<text x="{COL_TIME}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">median merge</text>'
        f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">responsiveness</text>']
    y += 17
    if not scored:
        parts.append(f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="11">'
                     f'no external merged PRs yet</text>')
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
        description="Render the self-hosted External Responsiveness SVG "
                    "from render-impact.py's cache. No network access.")
    p.add_argument("--out-dir", default=os.environ.get("OUT_DIR", "./widgets"),
                   help="Where to write the SVG (env: OUT_DIR)")
    p.add_argument("--theme", default=os.environ.get("THEME", "tokyonight"),
                   choices=list(THEMES),
                   help="Color theme (env: THEME)")
    p.add_argument("--cache-file",
                   default=os.environ.get("CACHE_FILE", DEFAULT_CACHE_FILE),
                   help="render-impact.py's JSON cache, read-only "
                        "(env: CACHE_FILE)")
    return p.parse_args()


def read_impact_cache(path):
    """Return (prs, insiders, fetched_at) from render-impact.py's cache.

    Every failure here is fatal by design: this renderer has no fetch path to
    degrade to, so a bad cache must leave the last good SVG in place.
    """
    cache = common.load_cache(path, IMPACT_CACHE_VERSION)
    if not cache or not cache.get("prs"):
        raise SystemExit(
            f"error: no usable impact cache at {path} (missing, corrupt, or "
            f"not schema version {IMPACT_CACHE_VERSION}) — run "
            f"render-impact.py first")
    if not cache.get("insiders"):
        raise SystemExit(
            f"error: impact cache {path} carries no insider set — refetch it "
            f"with render-impact.py; guessing would count our own orgs' "
            f"repos as external")
    return (list(cache["prs"].values()), frozenset(cache["insiders"]),
            cache.get("fetched_at"))


def cache_is_stale(fetched_at, now=None):
    """True when the cache is old enough that the card should say so.

    render-impact refreshes twice daily, so anything past a day means its
    timer has missed at least one run.
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
    """Score the cached PRs and render the card.

    Returns (svg, notes) — `notes` is what the card deliberately does not say,
    carried out to the run's stdout instead.
    """
    by_repo, skipped = turnaround_by_repo(prs, insiders)
    rows, excluded_repos, excluded_prs = responsiveness_rows(by_repo, knobs)
    scored = rescale(rows)[:TOP_N]
    notes = {"shown": len(scored), "ranked": len(rows),
             "excluded_repos": excluded_repos, "excluded_prs": excluded_prs,
             "skipped": skipped, "min_prs": knobs["min_prs"]}
    return render_responsiveness(C, scored), notes


def main():
    args = parse_args()

    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    prs, insiders, fetched_at = read_impact_cache(args.cache_file)
    svg, notes = build_card(C, prs, insiders, metric_knobs())
    if cache_is_stale(fetched_at):
        # Same rule as the other renderers: the stamp is a CAVEAT, shown only
        # when the data is old enough to mislead. render-impact refreshes this
        # cache twice a day, so a fresh render carries no stamp at all.
        svg = stamp_cache_notice(C, svg, fetched_at)
    (out / "responsiveness.svg").write_text(svg)

    print(f"wrote {out}/responsiveness.svg "
          f"(showing {notes['shown']} of {notes['ranked']} ranked)")
    # What the card does not say, said here instead: an operator checking a
    # thin-looking board needs to know whether repos fell below the floor or
    # simply had no cached merge time.
    if notes["excluded_repos"]:
        print(f"  {plural(notes['excluded_repos'], 'repo')} "
              f"({plural(notes['excluded_prs'], 'merged PR')}) below the "
              f"n >= {notes['min_prs']} floor")
    if notes["skipped"]:
        print(f"  {plural(notes['skipped'], 'merged PR')} skipped: no cached "
              f"merge time (run render-impact.py --resync)")


if __name__ == "__main__":
    # No HTTPError branch, unlike the other two renderers: this one never
    # touches the network. SystemExit (a bad cache) passes straight through.
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
