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

> **git-fame is PINNED to our fork build, not to PyPI.** `render-impact.py`'s
> blame pass runs `git fame` per repo, and stock git-fame before 4.0.0 spawned
> one serial `git blame` subprocess per file. Both our fork and PyPI 4.0.0 have
> `--jobs`; we run the fork because upstream's version costs ~2.2× peak memory
> for no time saving (measured — see below).
>
> ```
> pip install --force-reinstall \
>   "git+https://github.com/Nitjsefnie-OSC/git-fame@a99855d3ab8323acd2c81cf40205d48ac8236537"
> ```
>
> `git-fame --version` must print **`3.1.4.dev7+ga99855d3a`**. A local checkout
> is at `/root/git-fame` on branch `parallel-blame`; `pip install .` from there
> is equivalent. A bare `pip install --upgrade git-fame` **silently undoes this
> pin** — 4.0.0 is the higher version number and installs cleanly.
>
> Do not pass `-j` at the `blame_repo` call site. The parallelism is automatic
> (`min(32, cpu+4)`), and passing the flag explicitly would turn an older
> install from "slow" into "every repo errors", which poisons the cache.
>
> **Three guards, at three different times.** `test_impact.py` fails if the
> installed build lacks `--jobs` (test time); `install.sh` warns (install
> time); and `render-impact.py`'s `check_git_fame()` prints the installed
> version on **every render** (run time). All three probe for `--jobs` in
> `git-fame --help` rather than matching a version string, so they need no
> change when the source moves between the fork and PyPI — and, by the same
> token, **none of them can tell the two apart.** The version in the
> `git-fame:` journal line is what distinguishes them. That last one logs on
> success on purpose — this renderer runs unattended on a timer, so a guard
> that is silent when healthy cannot be told apart from a guard that never
> ran. If `git-fame:` is missing from a run's journal output, treat that as
> the alarm.
>
> **Provenance.** `--jobs` is our patch,
> [casperdcl/git-fame#132](https://github.com/casperdcl/git-fame/pull/132),
> closing [#131](https://github.com/casperdcl/git-fame/issues/131). The
> maintainer rebased and squashed it rather than merging the branch, so the PR
> reads CLOSED while the work shipped as `41e9e48` in v4.0.0 — that release IS
> that commit, and the PyPI wheel matches the git tree. Output is byte-identical
> to the old fork build.
>
> One deliberate difference survives in upstream's version: it submits every
> file to the pool up front (`list(ex.map(...))`) where the fork used a bounded
> work window, so peak memory is held across all files rather than a window of
> them. **Measured 2026-08-10 — it costs roughly 2.2× peak memory for no time
> saving.** A full `--resync` over the same 59 repos, two runs per build on a
> GitHub runner (workflow run `31386157081`, `gitfame-resync-memory` — never on
> this box):
>
> | build | cgroup peak | sampler tree peak | `git fame` proc peak | wall |
> |---|---|---|---|---|
> | fork `a99855d3` | 624.6 / 618.4 MB | 894.6 / 782.8 MB | 396.5 / 359.3 MB | 633.3 / 629.5 s |
> | upstream 4.0.0 | 1351.6 / 1354.1 MB | 2251.9 / 2252.5 MB | 996.7 / 1000.8 MB | 627.3 / 625.0 s |
>
> Every run was validated before its numbers were believed: `rc=0`, a written
> `impact.svg` of an identical 11,333 B, 59 repos actually blamed, and each
> build confirmed by its own `git-fame:` guard line. The two arms blamed the
> same repos with the same surviving LOC, so the comparison is like-for-like.
> The wall-clock difference is under 1% and inside run-to-run noise — the
> memory is spent to buy nothing.
>
> **So the pin went back to the fork on 2026-08-10 (operator instruction),
> after a brief move to 4.0.0.** Paying 2.2× peak memory to buy nothing is the
> whole argument; the earlier decision to move had been taken on a *time* cost
> structure, before anyone had measured memory. The peak scales with the
> largest single repo blamed (`Nitjsefnie-OSC/codex`, 1.65 M LOC), not with the
> repo count, so it grows as that repo does.
>
> **There is deliberately no pin-expiry check in CI, and none should be
> added.** `pin-still-needed.yml` was deleted and stays deleted. Its question —
> has upstream shipped `--jobs` yet — is answered permanently and was never the
> reason for the pin. The live reason is a memory property that no `--help`
> probe can see, and it does not decay on a schedule, so a recurring check
> would only ever produce false "you can unpin now" pressure. If you want to
> re-test the gap, re-run the measurement workflow, on a runner.
>
> Not reported upstream: `casperdcl/git-fame` is blocked by operator
> instruction (2026-08-07, no expiry, no ask to be raised) — see
> `/root/oss-contrib/repo-references/casperdcl-git-fame.md`.
>
> [#130](https://github.com/casperdcl/git-fame/issues/130) is a pre-existing,
> unrelated bug found during the work. It is NOT caused by the parallelism:
> the nondeterministic ordering comes from iterating an unsorted `set` outside
> the blame loop, identically in stock, fork and 4.0.0.

The long timeouts are load-bearing. `--resync` ignores the cache and re-blames
everything, so it takes far longer than an incremental run.

> **Reading `systemctl list-timers` will mislead you here.** The NEXT column is
> local time *and* includes `RandomizedDelaySec`, so `Sun 04:17 UTC` shows up as
> `Sun 06:19 CEST` — that is the same schedule, not drift. Check the unit file
> (`systemctl cat <unit>.timer`) before "fixing" a discrepancy that isn't one.
> `gh-widgets-resync.timer` also shows a blank LAST column until its first
> Sunday fires; blank ≠ broken.

> **Health check in one line:** every render prints
> `git-fame: <version> with --jobs` before the blame pass. If that line is
> absent from a run's journal, the guard did not run — which is the alarm, not
> the silence.

> The token file holds the **`gh` CLI's own OAuth token** (`gh auth token`,
> scopes incl. `repo`), not a narrow PAT. Safe only because the renderer filters
> at the query (`privacy: PUBLIC`, `render.py:227`) and skips `isPrivate` PRs —
> keep that filter, or private repos leak into public SVGs. Swap in a
> `read:user`+`public_repo` PAT if you want least privilege.
