#!/usr/bin/env python3
"""Render public owned-repository popularity from fresh git history and REST data.

OUT_DIR, THEME, GH_TOKEN and GH_TOKEN_FILE match the other renderers.
There is no cache: every run clones each default branch and reads live traffic.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _load_common():
    path = Path(__file__).resolve().with_name('ghwidgets_common.py')
    if not path.exists():
        raise SystemExit(f'error: render-popularity.py cannot find {path}; deploy with install.sh')
    spec = importlib.util.spec_from_file_location('ghwidgets_common', path)
    if spec is None or spec.loader is None:
        raise SystemExit(f'error: cannot load {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load_common()
REQUIRED_COMMON = 10
common.check_version(REQUIRED_COMMON)
THEMES = common.THEMES
API = 'https://api.github.com'
AGENT_EMAILS = frozenset({'noreply@anthropic.com', 'noreply@openai.com',
                          'noreply@z.ai', 'noreply@openrouter.ai'})


def rest(token, path, retries=3, timeout=20):
    """GET JSON and its Link header, retrying transport and server failures."""
    url = path if path.startswith(API + '/') else API + path
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'gh-widgets/1.0 (+https://github.com/Nitjsefnie/gh-widgets)',
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as result:
                if result.status != 200:
                    raise urllib.error.HTTPError(url, result.status, 'expected HTTP 200',
                                                 result.headers, None)
                return json.loads(result.read()), result.headers.get('Link', '')
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(5 * (attempt + 1))
    raise RuntimeError('unreachable: REST retry loop exhausted')


def rest_pages(token, path):
    """Follow every next Link; a repeated URL fails instead of truncating."""
    items, seen = [], set()
    while path:
        url = urllib.parse.urljoin(API, path)
        if url in seen:
            raise common.PaginationLimitError(f'REST pagination did not advance: {path}')
        seen.add(url)
        data, link = rest(token, path)
        items.extend(data)
        next_link = re.search(r'<([^>]+)>;\s*rel="next"', link)
        path = next_link.group(1) if next_link else ''
    return items


def is_outsider(name, email, user, emails):
    """Exclude exact owner identities, agent addresses and explicit bots."""
    email = email.lower()
    return not (email in emails or email in AGENT_EMAILS
                or email == 'noreply@github.com'
                or '[bot]' in name.lower() or '[bot]' in email
                or name in (user['login'], user['name']))


def parse_authors(data, repository):
    """Read git's NUL-terminated byte records without newline normalization."""
    records = data.split(b'\0')
    if records[-1]:
        raise RuntimeError(f'{repository}: malformed git author record (missing NUL)')
    for record in records[:-1]:
        fields = record.split(b'\x1f')
        if len(fields) != 2:
            raise RuntimeError(f'{repository}: malformed git author record (expected two fields)')
        yield tuple(field.decode('utf-8', errors='surrogateescape') for field in fields)


def outsider_commits(repository, user, emails):
    """Count default-branch authors in a fresh blobless clone, then remove it."""
    name = repository['full_name']
    with tempfile.TemporaryDirectory(prefix='ghwidgets-popularity-') as directory:
        clone = str(Path(directory) / 'repo')
        try:
            subprocess.run(['git', 'clone', '--filter=blob:none', '--single-branch',
                            '--', repository['clone_url'], clone],
                           check=True, capture_output=True)
            refs = subprocess.run(['git', '-C', clone, 'show-ref', '--head'],
                                  check=False, capture_output=True)
            if refs.returncode == 1 and not refs.stdout and not refs.stderr:
                # A broken remote HEAD can also produce a clone with no refs.
                subprocess.run(['git', '-C', clone, 'symbolic-ref', 'HEAD'],
                               check=True, capture_output=True)
                remote = subprocess.run(['git', 'ls-remote', '--', repository['clone_url']],
                                        check=True, capture_output=True)
                if remote.stdout:
                    raise RuntimeError(f'{name}: empty clone but remote has history')
                return 0
            refs.check_returncode()
            branch = 'refs/remotes/origin/' + repository['default_branch']
            log = subprocess.run(['git', '-C', clone, 'log', '-z', '--format=%an%x1f%ae',
                                  branch, '--'],
                                 check=True, capture_output=True, text=False)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f'{name}: git clone/history failed: {exc}') from exc
        return sum(is_outsider(author, email, user, emails)
                   for author, email in parse_authors(log.stdout, name))


# The acquisition sequence keeps the eligible population and score inputs together.
# pylint: disable=too-many-locals
def fetch_scores(token):
    """Acquire all eligible user/org repositories and their complete scores."""
    user, _ = rest(token, '/user')
    emails = common.our_emails(user['login'], user['id'])
    if user.get('email'):
        emails = emails | {user['email'].lower()}
    orgs = rest_pages(token, '/user/orgs?per_page=100')
    repositories = rest_pages(token, '/user/repos?affiliation=owner&per_page=100')
    for org in orgs:
        login = urllib.parse.quote(org['login'], safe='')
        repositories.extend(rest_pages(token, f'/orgs/{login}/repos?per_page=100'))
    scores, seen = [], set()
    for repository in repositories:
        name = repository['full_name']
        if (repository['visibility'] != 'public' or repository['fork']
                or repository['archived'] or name.lower() in seen):
            continue
        seen.add(name.lower())
        traffic = {}
        for endpoint in ('clones', 'views'):
            try:
                data, _ = rest(token, f'/repos/{name}/traffic/{endpoint}')
                traffic[endpoint] = data['uniques']
            except (urllib.error.URLError, ValueError, KeyError) as exc:
                raise RuntimeError(f'{name}: unreadable traffic/{endpoint}: {exc}') from exc
        outsiders = outsider_commits(repository, user, emails)
        score = (repository['stargazers_count'] * 50 + repository['forks_count'] * 40
                 + outsiders * 5 + traffic['clones'] * 2 + traffic['views'])
        scores.append((name, score))
    return user, scores
# pylint: enable=too-many-locals


def rank_repositories(scores):
    """Return the five strongest rows, breaking ties by canonical repo name."""
    ranked = sorted(scores, key=lambda item: (-item[1], item[0].lower(), item[0]))[:5]
    top = ranked[0][1] if ranked else 0
    return [(name, f'{raw * 10 / top if top else 0:.2f}') for name, raw in ranked]


def render_popularity(C, user, scores):
    """Draw rank, repository name and relative score with shared SVG chrome."""
    login = user['login']
    rows = rank_repositories(scores)
    parts = [
        f'<text x="20" y="34" fill="{C["gold"]}" font-size="14" font-weight="600">repo popularity</text>',
        f'<text x="500" y="34" text-anchor="end" fill="{C["cyan"]}" font-size="14">{common.fmt_short(len(scores))} repos</text>',
        f'<text x="20" y="54" fill="{C["dim"]}" font-size="11">{common.xml_escape(login)} - top 5 / relative score</text>',
        f'<line x1="20" y1="66" x2="500" y2="66" stroke="{C["border"]}"/>',
    ]
    for rank, (name, score) in enumerate(rows, 1):
        y = 92 + (rank - 1) * 34
        label = name if len(name) <= 47 else name[:44] + '...'
        parts.extend([
            f'<text x="24" y="{y}" fill="{C["dim"]}" font-size="12">{rank}</text>',
            f'<text x="52" y="{y}" fill="{C["fg"]}" font-size="12">{common.xml_escape(label)}</text>',
            f'<text x="500" y="{y}" text-anchor="end" fill="{C["green"]}" font-size="12">{score}</text>',
        ])
    if not rows:
        parts.append(f'<text x="20" y="92" fill="{C["dim"]}" font-size="12">no public repositories</text>')
    parts.append(f'<text x="20" y="266" fill="{C["dim"]}" font-size="10">traffic: 14-day uniques / leader = 10.00</text>')
    summary = '; '.join(f'{name}: {score}' for name, score in rows) or 'no public repositories'
    desc = (f'Top repositories for {login}. {summary}. '
            'Scores relative to the leader; traffic covers the last 14 days.')
    return common.base_card(C, 520, 288, '\n  '.join(parts), card='popularity',
                            title=f'Repository popularity for {login}', desc=desc)


def parse_args():
    p = argparse.ArgumentParser(description='Render public repository popularity SVG.')
    p.add_argument('--token', default=os.environ.get('GH_TOKEN'), help='GitHub PAT (env: GH_TOKEN)')
    p.add_argument('--token-file', default=os.environ.get('GH_TOKEN_FILE'), help='Token file (env: GH_TOKEN_FILE)')
    p.add_argument('--out-dir', default=os.environ.get('OUT_DIR', './widgets'), help='SVG directory (env: OUT_DIR)')
    p.add_argument('--theme', default=os.environ.get('THEME', 'tokyonight'), choices=list(THEMES), help='Palette (env: THEME)')
    args = p.parse_args()
    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding='utf-8').strip()
    if not token:
        p.error('--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required')
    return args, token


def main():
    args, token = parse_args()
    user, scores = fetch_scores(token)
    svg = render_popularity(THEMES[args.theme], user, scores)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'popularity.svg'
    common.atomic_write_text(path, svg)
    print(f'wrote {path} ({len(scores)} repositories)')


if __name__ == '__main__':
    main()
