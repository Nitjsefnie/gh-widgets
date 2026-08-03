# gh-widgets — deployment and unit notes

Moved out of `/root/CLAUDE.md` on 2026-07-26 so it loads only when working in
this repo. The unit/env enumeration and the render-impact schedule table that
used to live here were dropped as derivable — read them from the units
themselves:

```
systemctl cat render-gh-widgets.service render-gh-widgets.timer
systemctl cat render-gh-widgets-resync.service render-gh-widgets-resync.timer
systemctl cat render-impact.service render-impact.timer
systemctl cat render-impact-resync.service render-impact-resync.timer
```

**gh-widgets rendering — restored on this box 2026-07-22.** Repo `/root/gh-widgets`.
**Deploy with `/root/gh-widgets/install.sh` (2026-07-24) — not `cp`.** Three
files now ship together: `render.py` → `/usr/local/bin/render-gh-widgets.py`,
`render-impact.py`, and the shared `ghwidgets_common.py`. Both renderers assert
its `COMMON_VERSION` at startup, so a partial copy produces a renderer that
refuses to run (deliberately — the alternative is rendering from a stale
module). `install.sh` stages all three, moves them into place, and starts both
entry points to prove the install is coherent. They are still *copies*, so
`diff` each against the repo before and after touching either side.

**External-impact rendering — a SECOND pair of units from the same repo.**
`impact.svg` is *not* produced by `render-gh-widgets.py`; it has its own
script, its own cache, and its own timers. Repo `/root/gh-widgets/render-impact.py`,
deployed as `/usr/local/bin/render-impact.py` — also a *copy*, so
`diff /usr/local/bin/render-impact.py /root/gh-widgets/render-impact.py`
before and after touching either, same as the main renderer.

Same env as the main pair except `CACHE_FILE=/var/lib/gh-widgets/impact-cache.json`
— a **separate cache** from `cache.json`; do not point them at one file — plus
`GH_EXTRA_EMAILS=zmatek.peter@gmail.com` on **both** impact units.

> **Do not drop `GH_EXTRA_EMAILS`.** Since 2026-07-24, line ownership is an
> exact match against the account's GitHub noreply addresses (derived from
> `login` + `databaseId`), replacing a substring test. The workstation address
> never appears as a noreply, so without this var those lines stop counting —
> **silently**, as a lower number rather than an error. It bites hardest on
> `--resync`, which re-blames every repo at once. Insiders/orgs need no such
> var: they are fetched from the account.

> **git-fame is pinned to a fork, not PyPI.** `render-impact.py`'s blame pass
> runs `git fame` per repo; stock git-fame spawns one serial `git blame`
> subprocess per file (N+3 processes per repo) and spends ~41% of its wall
> time on process spawn alone. We install
> `Nitjsefnie-OSC/git-fame` (branch `parallel-blame`), which adds `--jobs`:
>
> ```
> pip install --force-reinstall "git-fame @ git+https://github.com/Nitjsefnie-OSC/git-fame@a99855d3ab8323acd2c81cf40205d48ac8236537"
> ```
>
> **How you can tell which build is installed.** git-fame derives its version
> from git tags via `setuptools_scm`, so the fork build reports
> `3.1.4.dev1+g<sha>` while PyPI stock reports `3.1.2`/`3.1.3` — a silent
> revert is visible in `pip freeze` by construction, with no wheel retagging
> needed.
>
> **Three guards, at three different times.** `test_impact.py` fails if the
> installed build lacks `--jobs` (test time); `install.sh` warns (install
> time); and `render-impact.py`'s `check_git_fame()` prints the installed
> version on **every render** (run time). That last one logs on success on
> purpose — this renderer runs unattended on a timer, so a guard that is
> silent when healthy cannot be told apart from a guard that never ran. If
> `git-fame:` is missing from a run's journal output, treat that as the alarm.
>
> Once the upstream PR merges, re-pin to the PyPI release that contains it and
> delete this note.

The impact units run at a lower priority than the main pair (`Nice=12` / `15`
vs `10`), and `render-impact.service` carries
`After=…render-gh-widgets.service` so it runs last in the widget cycle. The
long timeouts are load-bearing: a run does a re-fetch **plus a git blame pass**
over external contributions and takes ~3 min normally, far more on `--resync`.

> **Reading `systemctl list-timers` will mislead you here.** The NEXT column is
> local time *and* includes `RandomizedDelaySec`, so `Sun 04:17 UTC` shows up as
> `Sun 06:19 CEST` — that is the same schedule, not drift. Check the unit file
> (`systemctl cat <unit>.timer`) before "fixing" a discrepancy that isn't one.
> `render-impact-resync.timer` also shows a blank LAST column until its first
> Sunday fires; blank ≠ broken.

> The token file holds the **`gh` CLI's own OAuth token** (`gh auth token`,
> scopes incl. `repo`), not a narrow PAT. Safe only because the renderer filters
> at the query (`privacy: PUBLIC`, `render.py:227`) and skips `isPrivate` PRs —
> keep that filter, or private repos leak into public SVGs. Swap in a
> `read:user`+`public_repo` PAT if you want least privilege.
