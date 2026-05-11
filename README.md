# gh-widgets

Self-hosted alternative to `github-readme-stats` and friends. Renders three
static SVGs about your GitHub profile on a cron — no JavaScript, no third-party
service, no flakiness.

![stats](https://nitjsefni.eu/widgets/stats.svg)
![streak](https://nitjsefni.eu/widgets/streak.svg)
![languages](https://nitjsefni.eu/widgets/languages.svg)

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
```

Works in GitHub READMEs (it goes through GitHub's camo proxy — note camo
caches images for ~10 minutes, so README updates lag by that much).

## Caveats

- The streak counter walks newest→oldest contribution days and skips a single
  leading zero (today might not be logged yet, or your timezone is ahead of
  UTC). It does **not** try to be cleverer than that.
- Token only needs read access. If you accidentally grant `repo` write
  scope, that's on you.

## License

MIT. See `LICENSE`.
