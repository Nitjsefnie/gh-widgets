# gh-widgets — deployment and unit notes

Moved out of `/root/CLAUDE.md` so it loads only when working in this repo.

**The units live in this repo now, in `units/`.** They used to be hand-maintained
in `/etc/systemd/system` and tracked nowhere, which is the same drift trap the
renderers themselves have. There are **four** files, and they replaced **ten**:

| unit | when | what |
|---|---|---|
| `gh-widgets.service` + `.timer` | hourly | all three renderers, in sequence |
| `gh-widgets-resync.service` + `.timer` | Sun 04:17 UTC | the same, with `--resync` — the only thing that ignores the caches |

The five old `render-*` service/timer pairs are **gone**; ordering that used to
be expressed with `After=` chains between separate units is now just the order
of the `ExecStart` lines. Read `units/*.service` for the env and the reasoning —
do not re-derive it here, and do not edit `/etc/systemd/system` by hand.

**Deploy with `/root/gh-widgets/install.sh` — never `cp`, never a hand-edited
unit.** Bare, it installs the four Python files to `/usr/local/bin`
(`render.py` → `render-gh-widgets.py`, `render-impact.py`,
`render-responsiveness.py`, `ghwidgets_common.py`). Each renderer asserts the
shared module's `COMMON_VERSION` at startup, so a partial copy yields a renderer
that refuses to run — deliberately, since the alternative is rendering from a
stale module. `install.sh --units` additionally installs `units/`, reloads
systemd, enables both timers and proves they load. Everything it installs is a
*copy*, so `diff` against the repo before and after touching either side.

**Two caches, and the flags are not symmetric.** `cache.json` is the profile
cache; `impact-cache.json` is shared by `render-impact.py` and
`render-responsiveness.py` — do not point them at one file. The unit sets
`CACHE_FILE=…/cache.json` in its environment and passes `--cache-file
…/impact-cache.json` explicitly to the two that share the impact cache, because
**`render.py` has no `--cache-file` flag at all** (env only) and
**`render-responsiveness.py` has no `--resync`** (it has no cache of its own;
re-running it after the impact resync *is* its resync). Check `--help` before
adding a flag to a unit — an unknown flag exits 2 and fails the whole unit.

> **Do not drop `GH_EXTRA_EMAILS`.** Line ownership is an exact match against
> the account's GitHub noreply addresses (derived from `login` + `databaseId`),
> replacing a substring test. The workstation address never appears as a noreply,
> so without this var those lines stop counting — **silently**, as a lower number
> rather than an error. It bites hardest on `--resync`, which re-blames every
> repo at once. Insiders/orgs need no such var: they are fetched from the
> account.

> **git-fame is pinned to a fork, not PyPI.** `render-impact.py`'s blame pass
> runs `git fame` per repo; stock git-fame spawns one serial `git blame`
> subprocess per file, so the pinned `Nitjsefnie-OSC/git-fame` fork that adds
> `--jobs` is several times faster:
>
> ```
> pip install --force-reinstall "git-fame @ git+https://github.com/Nitjsefnie-OSC/git-fame@a99855d3ab8323acd2c81cf40205d48ac8236537"
> ```
>
> **How you can tell which build is installed.** git-fame derives its version
> from git tags via `setuptools_scm`, so the fork build reports
> `3.1.4.devN+g<sha>` while PyPI stock reports `3.1.2`/`3.1.3` — a silent
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
> **Upstream status.** The patch is
> [casperdcl/git-fame#132](https://github.com/casperdcl/git-fame/pull/132),
> which fixes
> [#131](https://github.com/casperdcl/git-fame/issues/131) (the serial
> one-blame-process-per-file issue). A pre-existing, unrelated bug found during
> the work is [#130](https://github.com/casperdcl/git-fame/issues/130).
>
> Once the upstream PR merges, re-pin to the PyPI release that contains it and
> delete this note.

The long timeouts are load-bearing. `--resync` ignores the cache and re-blames
everything, so it takes far longer than an incremental run.

> **Reading `systemctl list-timers` will mislead you here.** The NEXT column is
> local time *and* includes `RandomizedDelaySec`, so `Sun 04:17 UTC` shows up as
> `Sun 06:19 CEST` — that is the same schedule, not drift. Check the unit file
> (`systemctl cat <unit>.timer`) before "fixing" a discrepancy that isn't one.
> `gh-widgets-resync.timer` also shows a blank LAST column until its first
> Sunday fires; blank ≠ broken.

> **Health check in one line:** every render prints
> `git-fame: 3.1.4.devN+g<sha> with --jobs (patched build)` before the blame
> pass. If that line is absent from a run's journal, the guard did not run —
> which is the alarm, not the silence.

> The token file holds the **`gh` CLI's own OAuth token** (`gh auth token`,
> scopes incl. `repo`), not a narrow PAT. Safe only because the renderer filters
> at the query (`privacy: PUBLIC`, `render.py:227`) and skips `isPrivate` PRs —
> keep that filter, or private repos leak into public SVGs. Swap in a
> `read:user`+`public_repo` PAT if you want least privilege.
