# gh-widgets

Self-hosted alternative to `github-readme-stats` and friends. Renders four
static SVGs about your GitHub profile on a cron — no JavaScript, no third-party
service, no flakiness.

![stats](https://nitjsefni.eu/widgets/stats.svg)
![streak](https://nitjsefni.eu/widgets/streak.svg)
![languages](https://nitjsefni.eu/widgets/languages.svg)
![external](https://nitjsefni.eu/widgets/external.svg)

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
sudo cp render.py /usr/local/bin/render.py
sudo chmod +x /usr/local/bin/render.py

# token: classic PAT with read:user + public_repo, or a fine-grained token
# with read-only profile access. Save to a file with mode 600.
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
<img src="https://your-domain/widgets/stats.svg"     width="420" alt="GitHub stats" />
<img src="https://your-domain/widgets/streak.svg"    width="420" alt="Contribution streak" />
<img src="https://your-domain/widgets/languages.svg" width="420" alt="Top languages" />
<img src="https://your-domain/widgets/external.svg"  width="420" alt="External contributions" />
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
