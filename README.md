# gh-widgets

Self-hosted alternative to `github-readme-stats` and friends. Renders five
static SVGs about your GitHub profile on a cron — no JavaScript, no third-party
service, no flakiness.

<img src="https://nitjsefni.eu/widgets/stats.svg" width="720" alt="stats" /><br/>
<img src="https://nitjsefni.eu/widgets/streak.svg" width="720" alt="streak" /><br/>
<img src="https://nitjsefni.eu/widgets/languages.svg" width="720" alt="languages" /><br/>
<img src="https://nitjsefni.eu/widgets/external.svg" width="720" alt="external" /><br/>
<img src="https://nitjsefni.eu/widgets/impact.svg" width="720" alt="impact" /><br/>

## Why

The popular widget services (vercel-hosted readme-stats, heroku streak
counters, etc.) routinely break — Vercel rate-limits them, the Heroku ones
sleep, GitHub camo caches the broken state, your README looks half-empty.

Render the SVGs yourself instead:

- **Static output.** A cron job (or systemd timer) writes `stats.svg`,
  `streak.svg`, `languages.svg` to a directory. Your web server serves them
  as plain files. No request-time API calls, no runtime dependencies.
- **Fails gracefully.** If a refresh fails (API rate limit, network), the
  previous SVGs keep serving. They can't 404 unless you delete them.
- **No JS, no fonts loaded by the SVG.** Uses `JetBrains Mono, monospace`
  with system fallback so embedding into a GitHub README looks right
  without pulling Google Fonts.
- **Pure Python stdlib.** `urllib` + `json` + `argparse`. Python 3.9+.
  No `pip install` needed.

## Install

```sh
sudo ./install.sh              # -> /usr/local/bin, or pass a directory

# token: a classic PAT with public_repo is enough; read:user is NOT required
# (nothing here reads your email). Save it to a file with mode 600.
echo "ghp_xxx..." | sudo tee /etc/gh-widgets.token
sudo chmod 600 /etc/gh-widgets.token

# customise the service unit (username, output dir, theme)
sudo cp examples/gh-widgets.service /etc/systemd/system/
sudo cp examples/gh-widgets.timer   /etc/systemd/system/
sudoedit /etc/systemd/system/gh-widgets.service   # set GH_USER, OUT_DIR

sudo systemctl daemon-reload
sudo systemctl enable --now gh-widgets.timer

# verify
sudo systemctl start gh-widgets.service
ls -la /var/www/example.com/widgets/
```

Then add the nginx snippet from `examples/nginx.conf` (CORS + cache-control).

### Why `install.sh` rather than `cp`

Both renderers share `ghwidgets_common.py` and assert its `COMMON_VERSION` on
startup, so the three files have to travel together. `install.sh` stages them
into the destination and moves them into place, then starts both entry points
to prove the install is coherent. A hand-rolled `cp` of one file leaves a
renderer that refuses to run — deliberately, since the alternative is rendering
wrong numbers from a stale module.

Note the rename: `render.py` installs as `render-gh-widgets.py` (the name the
units use). The scripts find the shared module relative to their own path, so
the rename is safe.

## Who counts as "you"

The external-contribution and impact cards need to know which repos are
*yours* and which lines are *yours*. Both are **derived from the token's own
account** — `login`, `databaseId`, and the org list — so joining an
organisation needs no config change. Ownership of a line is decided by exact
match against the account's GitHub noreply addresses, not by pattern-matching
a name.

Two escape hatches, both **additive** — neither can remove something the API
reported, because a stale override is exactly the drift this replaced:

| Variable | Use it for |
|---|---|
| `GH_EXTRA_INSIDERS` | Owners to treat as yours that the API will not report — e.g. an org whose membership is private. Comma-separated. |
| `GH_EXTRA_EMAILS` | Commit-author addresses that are yours but are not a GitHub noreply address — e.g. the address you use on a workstation. Comma-separated. |

## Tuning the impact score (`render-impact.py`)

Each table ranks repos by `WilsonLowerBound(mine/total, z) * mine**gamma`. The
defaults are the validated values; override only if you know why.

| Variable | Default | Effect |
|---|---|---|
| `IMPACT_Z` | `2.58` | Confidence level of the lower bound. Higher = more sceptical of small samples. |
| `IMPACT_PR_GAMMA` | `1.0` | How much raw PR volume counts against share. |
| `IMPACT_ISSUE_GAMMA` | `1.75` | Same, for issues. |
| `IMPACT_LOC_GAMMA` | `0.5` | Same, for surviving lines. |

An unparseable value aborts the run rather than silently reverting to the
default — a mis-typed knob that quietly renders wrong numbers is worse than a
failed run.

## Usage (manual run)

```sh
GH_USER=octocat GH_TOKEN=ghp_xxx OUT_DIR=./widgets ./render.py
GH_USER=octocat GH_TOKEN_FILE=/etc/gh-widgets.token OUT_DIR=/var/www/example/widgets THEME=catppuccin ./render.py
```

## Caching

GitHub rejects a full-year contribution-calendar query with
`RESOURCE_LIMITS_EXCEEDED` once an account's history is large enough, so the
renderer caches the data that cannot change and refetches only what can.

- `CACHE_FILE` — cache location, default `/var/lib/gh-widgets/cache.json`.
  Missing, unreadable, corrupt, or schema-mismatched caches are not errors: the
  run falls back to a full fetch.
- Settled calendar days and `MERGED` pull requests are cached. Open and closed
  PRs, issues, and the trailing 7 days of calendar are refetched every run —
  closed items can be reopened, so they are never treated as final.
- A cold cache backfills the year in 12 monthly windows rather than one
  full-year request, so the first run cannot trip the node limit.
- If a fetch fails and a cache exists, the widgets render from cache and the
  card shows the cache timestamp, so staleness is visible rather than silent.
- `--resync` discards the cache and rebuilds it. Run it periodically (a weekly
  timer is enough) so nothing depends indefinitely on `MERGED` being one-way.

## Themes

Four built-in palettes. Pick one in `THEME=` or via `--theme`:

- `tokyonight` (default)
- `catppuccin`
- `gruvbox`
- `github-dark`

Adding a new theme is ~12 lines — just add a dict to `THEMES` in `render.py`.

## Embedding

```html
<img src="https://your-domain/widgets/stats.svg"     width="720" alt="GitHub stats" />
<img src="https://your-domain/widgets/streak.svg"    width="720" alt="Contribution streak" />
<img src="https://your-domain/widgets/languages.svg" width="720" alt="Top languages" />
<img src="https://your-domain/widgets/external.svg"  width="720" alt="External contributions" />
<img src="https://your-domain/widgets/impact.svg"    width="720" alt="External impact" />
```

Works in GitHub READMEs (it goes through GitHub's camo proxy — note camo
caches images for ~10 minutes, so README updates lag by that much).

## The `external` widget

`external.svg` answers "what have I done in *other people's* repos" — the
number profile stats usually bury, since a repo you own and a repo you
contributed to both just count as "a repo". It has two parallel rows,
**pull requests** and **issues**, each with three figures.

Pull requests:

- **opened** — PRs you authored in repos owned by neither you nor any org you
  belong to.
- **merged** — how many of those were merged. Merged is what the API actually
  records, so that's what it says.
- **repos** — how many distinct external repos you reached.

Issues (the same external predicate — repos owned by neither you nor your
orgs, private repos excluded):

- **opened** — issues you filed in those external repos.
- **maintainer-accepted** — how many the maintainer closed as completed
  (state `CLOSED` + stateReason `COMPLETED`; `NOT_PLANNED` closures don't
  count). It's the issue analog of a merged PR — and the wording is
  deliberate: *you* opened the report, the *maintainer* accepted it, so it
  doesn't claim you did the fixing.
- **repos** — how many distinct external repos those issues touched.

Issues come from the GraphQL `user.issues` connection, which returns issues
only, so pull requests are never double-counted. The footer pairs the two
rows' success rates: PR merge rate and the maintainer-accepted rate.

Your orgs are read from the API and excluded automatically, so it keeps working
when you join or leave one. Private repos are left out — nobody looking at the
SVG could verify them.

It pages through your whole PR *and* issue history (100 per request), so a run
costs a few extra API calls. A non-issue on an hourly timer; worth knowing if
you run it every minute.

## Caveats

- The streak counter walks newest→oldest contribution days and skips a single
  leading zero (today might not be logged yet, or your timezone is ahead of
  UTC). A **second** zero is a real gap and ends the streak. It does **not**
  try to be cleverer than that.
- Token only needs read access. If you accidentally grant `repo` write
  scope, that's on you.

## Tests

```sh
python3 -m unittest discover -v
```

Stdlib `unittest`, no network — the streak and external-contribution maths run
against hand-built calendars. Nothing to install.

## License

MIT. See `LICENSE`.
