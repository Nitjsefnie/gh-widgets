#!/usr/bin/env python3
"""Render cumulative star history for public repositories (stdlib only).

GH_STAR_REPOS / --repos: comma-separated owner/repo names.
OUT_DIR, THEME, GH_TOKEN and GH_TOKEN_FILE match the other renderers.
STARS_CACHE_FILE / --cache-file defaults to /var/lib/gh-widgets/stars-cache.json;
CACHE_FILE is deliberately ignored (the service uses it for the profile).

GitHub supplies only CURRENT stargazers: users who un-starred are absent.
The list is capped at 40,000; the graph discloses truncation, while the current
repository total remains separate. No attempt is made to bypass that cap.
Every run refetches the whole list because old stars can disappear. The
separate, locked, atomic cache is for failed-fetch fallback only, always stamped
on the SVG. Explicitly private or missing repositories never use fallback.
"""
import argparse
import importlib.util
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def _load_common():
    path = Path(__file__).resolve().with_name('ghwidgets_common.py')
    if not path.exists():
        raise SystemExit(f'error: render-stars.py cannot find {path}; deploy with install.sh')
    spec = importlib.util.spec_from_file_location('ghwidgets_common', path)
    if spec is None or spec.loader is None:
        raise SystemExit(f'error: cannot load {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load_common()
REQUIRED_COMMON = 14
common.check_version(REQUIRED_COMMON)
THEMES = common.THEMES
gql = common.gql
CACHE_VERSION = 1
MAX_STARS = 40000
DEFAULT_CACHE_FILE = '/var/lib/gh-widgets/stars-cache.json'
STARS_QUERY = '''
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    isPrivate
    nameWithOwner
    stargazerCount
    stargazers(first: 100, after: $cursor,
               orderBy: {field: STARRED_AT, direction: ASC}) {
      edges { starredAt }
      pageInfo { hasNextPage endCursor }
    }
  }
}
'''


class RepositoryUnavailableError(ValueError):
    """A missing/private repository must not be published, even from cache."""


def parse_ts(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('star timestamps must have a timezone')
    return stamp.astimezone(timezone.utc)


def validate_history(history):
    """Reject incomplete/corrupt data before rendering or caching it."""
    total = history['total']
    dates = history['starred_at']
    if (isinstance(total, bool) or not isinstance(total, int) or total < 0
            or not isinstance(dates, list) or len(dates) != min(total, MAX_STARS)):
        raise ValueError('incomplete star history')
    for date in dates:
        parse_ts(date)
    return history


def fetch_stars(token, repo):
    owner, name = repo.split('/')
    dates, seen = [], set()
    cursor = None
    total = 0
    for _ in range(MAX_STARS // 100):
        node = gql(token, STARS_QUERY, {'owner': owner, 'name': name,
                                        'cursor': cursor})['repository']
        if not node or node['isPrivate']:
            raise RepositoryUnavailableError('star history requires a public repository')
        total = node['stargazerCount']
        connection = node['stargazers']
        dates.extend(edge['starredAt'] for edge in connection['edges'])
        if len(dates) > MAX_STARS:
            raise ValueError('star history exceeds GitHub cap')
        info = connection['pageInfo']
        if not info['hasNextPage'] or len(dates) == MAX_STARS:
            history = validate_history({'total': total, 'starred_at': dates,
                                        'nameWithOwner': node['nameWithOwner']})
            history['starred_at'].sort(key=parse_ts)
            return history
        cursor = info['endCursor']
        if not cursor or cursor in seen:
            raise common.PaginationLimitError('star history cursor did not advance')
        seen.add(cursor)
    raise common.PaginationLimitError('star history did not reach the final page')


# Geometry is kept together so the labels and step line use the same axes.
# pylint: disable=too-many-locals
def render_stars(C, repo, history, now=None):
    """Draw a step for each star, with elapsed UTC time on the x axis."""
    repo = history.get('nameWithOwner', repo)
    now = now or datetime.now(timezone.utc)
    dates = sorted(min(parse_ts(s), now) for s in history['starred_at'])
    start = dates[0] if dates else now
    span = max((now - start).total_seconds(), 1.0)
    total = history['total']
    ceiling = max(total, 1)
    left, right, top, bottom = 52, 500, 82, 224
    path = [f'M {left:.2f} {bottom:.2f}']
    for count, date in enumerate(dates, 1):
        x = left + (date - start).total_seconds() / span * (right - left)
        y = bottom - count / ceiling * (bottom - top)
        path.append(f'H {x:.2f} V {y:.2f}')
    path.append(f'H {right:.2f}')
    display_repo = repo if len(repo) <= 68 else repo[:65] + '...'
    parts = [
        f'<text x="20" y="34" fill="{C["gold"]}" font-size="14" font-weight="600">star history</text>',
        f'<text x="500" y="34" text-anchor="end" fill="{C["cyan"]}" font-size="14">{total:,} stars</text>',
        f'<text x="20" y="52" fill="{C["dim"]}" font-size="11">{common.xml_escape(display_repo)}</text>',
        f'<line x1="20" y1="64" x2="500" y2="64" stroke="{C["border"]}"/>',
    ]
    for count in sorted({0, total // 2, total}):
        y = bottom - count / ceiling * (bottom - top)
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="{C["border"]}"/>'
            f'<text x="44" y="{y + 4:.2f}" text-anchor="end" fill="{C["dim"]}" font-size="10">{common.fmt_short(count)}</text>')
    parts.append(f'<path d="{" ".join(path)}" fill="none" stroke="{C["green"]}" stroke-width="2"/>')
    if not dates:
        parts.append(f'<text x="276" y="150" text-anchor="middle" fill="{C["dim"]}" font-size="11">no stars yet</text>')
    note = 'current stargazers only'
    if total > MAX_STARS:
        note += '; first 40,000 shown (GitHub cap)'
    parts.extend([
        f'<text x="{left}" y="244" fill="{C["dim"]}" font-size="10">{start:%Y-%m-%d}</text>',
        f'<text x="{right}" y="244" text-anchor="end" fill="{C["dim"]}" font-size="10">{now:%Y-%m-%d}</text>',
        f'<text x="20" y="262" fill="{C["dim"]}" font-size="10">{note}</text>',
    ])
    desc = (f'{repo}: {total:,} current stars. Cumulative stars from '
            f'{start:%Y-%m-%d} to {now:%Y-%m-%d}; {note}. '
            'Users who un-starred are absent.')
    return common.base_card(C, 520, 288, '\n  '.join(parts), card='stars',
                            title=f'Star history for {repo}', desc=desc)


def load_inputs(path, token, repo):
    try:
        history = fetch_stars(token, repo)
    except RepositoryUnavailableError:
        raise
    except Exception as exc:
        cached = common.load_cache(path, CACHE_VERSION).get(repo.lower())
        try:
            validate_history(cached)
            parse_ts(cached['fetched_at'])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise exc from None
        return cached, common.CacheFallback(cached['fetched_at'], 'fetch_stars', exc)
    history = {**history, 'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    common.merge_cache(path, CACHE_VERSION, {repo.lower(): history})
    return history, None


def parse_args():
    p = argparse.ArgumentParser(description='Render public repository star-history SVGs.')
    p.add_argument('--repos', default=os.environ.get('GH_STAR_REPOS', ''),
                   help='Comma-separated owner/repo list (env: GH_STAR_REPOS)')
    p.add_argument('--token', default=os.environ.get('GH_TOKEN'), help='GitHub PAT (env: GH_TOKEN)')
    p.add_argument('--token-file', default=os.environ.get('GH_TOKEN_FILE'), help='Token file (env: GH_TOKEN_FILE)')
    p.add_argument('--out-dir', default=os.environ.get('OUT_DIR', './widgets'), help='SVG directory (env: OUT_DIR)')
    p.add_argument('--theme', default=os.environ.get('THEME', 'tokyonight'), choices=list(THEMES), help='Palette (env: THEME)')
    p.add_argument('--cache-file', default=os.environ.get('STARS_CACHE_FILE', DEFAULT_CACHE_FILE),
                   help='Separate star cache (env: STARS_CACHE_FILE; ignores CACHE_FILE)')
    args = p.parse_args()
    repos = [repo.strip() for repo in args.repos.split(',') if repo.strip()]
    if not repos or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repo)
                        or repo.split('/')[1] in ('.', '..') for repo in repos):
        p.error('--repos (or GH_STAR_REPOS) must list owner/repo names')
    configured_names = {}
    for repo in repos:
        configured_names.setdefault(repo.lower(), repo)
    args.repos = list(configured_names.values())
    if len({repo.lower().replace('/', '-') for repo in args.repos}) != len(args.repos):
        p.error('configured repositories produce colliding SVG filenames')
    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding='utf-8').strip()
    if not token:
        p.error('--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required')
    return args, token


@common.timed_run('render-stars')
def main():
    """Fetch and publish each repository with a separate stars cache.

    Input time includes paging and cache writes, net of GraphQL waits.
    Rendering measures local star-history geometry; timings stay out of SVGs.
    """
    args, token = parse_args()
    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for repo in args.repos:
        with common.timing.track('phase', 'load_inputs'):
            history, fallback = load_inputs(args.cache_file, token, repo)
        with common.timing.track('phase', 'render_svg'):
            svg = render_stars(C, repo, history)
        if fallback:
            print(f'fetch failed at {fallback.phase}: {fallback.message}; '
                  f'rendering {repo} from cache (fetched_at={fallback.fetched_at})')
            svg = common.stamp_cache_notice(C, svg, fallback.fetched_at)
        path = out / ('stars-' + repo.lower().replace('/', '-') + '.svg')
        common.atomic_write_text(path, svg)
        print(f'wrote {path} ({history["total"]} stars)')


if __name__ == '__main__':
    main()
