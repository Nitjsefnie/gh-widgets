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
unit.** Bare, it installs a six-file deployment set to `/usr/local/bin`
(`render.py` → `render-gh-widgets.py`, `render-impact.py`, `impact_loc.py`,
`render-responsiveness.py`, `ghwidgets_common.py`, `ghwidgets_data.py`). The
three renderers retain their existing `ghwidgets_common.py` runtime dependency
and assert its `COMMON_VERSION` at startup. `ghwidgets_data.py` is installed
alongside as the separate public API for pinned consumers; the renderers do not
import it. A partial renderer deployment missing `ghwidgets_common.py` refuses
to run, deliberately preventing rendering from a stale module. `install.sh --units`
additionally installs `units/`, reloads systemd, enables both timers and proves
them load. Everything it installs is a *copy*, so `diff` against the repo before
and after touching either side.

**Public snapshot consumers.** `ghwidgets_data.py` is the supported public
boundary for ghpulse, currently schema version `1`. Its snapshot is an
acquisition/interchange contract, not an input format for renderer-private
profile, impact, or responsiveness data. `fetch_authored_snapshot(token,
login)` pages the complete authored issue and pull-request history. It follows
every cursor to the final page; cursor failures and explicit safety limits
raise rather than silently publishing partial data.

The v1 top-level keys are exactly `schema_version`, `generated_at`, `account`,
`repositories`, `issues`, and `pull_requests`; membership relationships and
`insiders` are not serialized. `account` is exactly `{login}`. A repository is
exactly `{id, nameWithOwner, url, isPrivate, owner}`, with `owner` exactly
`{login}` and `isPrivate` always `false`. An issue is exactly
`{node_id, repository_id, repository, owner, repository_url, is_private,
number, url, created_at, updated_at, closed_at, state, state_reason}`. A pull
request has exactly those fields except `state_reason`, plus exactly
`merged_at` and `merged`. The PR source does not produce the issue
`state_reason` field, so adding it to a PR is rejected. Strings are non-empty,
numbers are positive integers, booleans are actual booleans, and timestamps
are non-empty RFC 3339 strings (or `null` only where documented as an
outcome). Every item must reference an included repository; duplicate IDs,
unknown/missing fields, private flags, credentials, invalid states, and
inconsistent closed/merged outcomes are rejected.

The complete allowed issue state matrix is:

| record | `state` | `closed_at` | `state_reason` |
|---|---|---|---|
| issue | `OPEN` | `null` | `null` or `REOPENED` |
| issue | `CLOSED` | RFC 3339 | `COMPLETED` or `NOT_PLANNED` |

The complete allowed pull-request state matrix is:

| record | `state` | `merged` | `closed_at` | `merged_at` |
|---|---|---|---|---|
| pull request | `OPEN` | `false` | `null` | `null` |
| pull request | `CLOSED` | `false` | RFC 3339 | `null` |
| pull request | `MERGED` | `true` | RFC 3339 | RFC 3339 |

No other state/state-reason/null combination is valid. Open items cannot have
`closed_at`; closed issues cannot have a null or `REOPENED` reason; PRs do not
have a `state_reason` field; a `MERGED` PR must have both outcome timestamps;
and `OPEN` or `CLOSED` PRs must be unmerged with `merged_at: null`.

The supported public functions are `normalise_issue`,
`normalise_pull_request`, `fetch_authored_snapshot`, `load_snapshot`, and
`write_snapshot`, plus `SCHEMA_VERSION`. Arbitrary construction is internal
(`_build_snapshot`) and is not a consumer API. The producer is public-only and
external-only: private repositories are filtered, the account and explicitly
public organization owners are excluded transiently, credentials are never
serialized, and environment insider/email additions are not read by the public
producer. Membership data never crosses the boundary. `load_snapshot()` and
`write_snapshot()` both enforce the full nested schema. Invalid data raises
`SnapshotValidationError`; unsupported versions raise its
`SnapshotVersionError` subclass. `write_snapshot()` validates before a locked,
atomic strict write and raises on lock or filesystem failure.

A consumer must pin the exact gh-widgets commit/submodule that defines this
contract and upgrade it deliberately with compatibility tests; an unpinned
moving branch is not supported. The renderer CLIs, flags, caches, and SVG
output remain unchanged and standalone. `cache.json` and `impact-cache.json`
are separate private renderer caches, not the public integration API.

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
>   "git+https://github.com/Nitjsefnie-OSC/git-fame@65925d8263576dc02510f06aadbcf0386d4edada"
> ```
>
> `git-fame --version` must print **`3.1.4.dev8+g65925d826`**. A local checkout
> is at `/root/git-fame` on branch `perf-blame`; `pip install .` from there
> is equivalent. A bare `pip install --upgrade git-fame` **silently undoes this
> pin** — 4.0.0 is the higher version number and installs cleanly.
>
> `perf-blame` is `parallel-blame` plus `--incremental` blame parsing: the
> parse only ever consumed chunk headers, while `--line-porcelain` re-emits
> every commit header per LINE and both porcelain formats emit the file's
> whole content. 53.8MB of blame output became 5.1MB on a 107k-loc repo, the
> parse 0.90s -> 0.11s, and the blame phase of a full resync -18.6%. Output is
> byte-identical, including on a 1.65M-loc repo.
>
> Note that under the default `BLAME_METHOD=targeted` **git-fame does not run
> at all** — this pin only governs `fame` and the weekly `both` audit. Keep it
> current anyway: the audit is what certifies the fast path, and auditing
> against a stale reference is worth less.
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

> **Health check in one line:** every render prints `blame-method: <method>`
> as its first line. Under `fame` or `both` it then prints
> `git-fame: <version> with --jobs` before the blame pass, and the absence of
> THAT line is the alarm. Under `targeted` git-fame never runs, so its guard
> line is absent on purpose — which is why the method line exists at all:
> silence would otherwise be ambiguous between "not used" and "guard broken".

> **`BLAME_METHOD` — how the per-repo line counts are produced.**
> `fame` (default) runs git-fame over every file; `targeted` blames only the
> files our own commits touched and takes `total` from a line count of the
> files `git grep -I .` calls text; `both` runs the two and **fails** on any
> disagreement. `targeted` is 11.4s against 489.9s over 59 repos — and is NOT
> safe to enable yet.
>
> **Why `targeted` is off.** A rename made by somebody ELSE after our commit
> moves our lines to a path our own history never mentions, so they go
> uncounted. Measured over the fleet with production's real address set:
> 54/59 agree, and the five that do not under-count `ours` by up to 99%
> (warior456/Sculk-Depths 313 -> 4, tiagolauer/OwlSQL 430 -> 258,
> AgoraDMV/DeltaTrack 2,852 -> 2,776, LunarVagabond/Pipe-Deck 262 -> 212,
> Brandcraf06/AdaPaxels 1 -> 0). `total` is exact in every case, so the error
> is purely in candidate-file selection. Fixing it means resolving our touched
> paths forward through the rename chain (`git log --diff-filter=R
> --name-status`), not blaming more files.
>
> **The 59/59 result that briefly justified enabling it was measured without
> `GH_EXTRA_EMAILS`** — the derived noreply addresses alone. Every future
> measurement of this must carry the production address set, which is why the
> workflow now passes it; auditing a different identity than the one that
> renders the card is not an audit. `gitfame-resync-memory.yml` runs `both`
> every Monday 05:23 UTC and fails on any disagreement.
>
> Two more ways to get a silent zero, both now regression-tested, both hit
> while writing this: a GitHub noreply address like
> `75166987+user@users.noreply.github.com` is a **regex** to `git log
> --author`, where `+` quantifies the preceding character and matches nothing;
> and the addresses are lower-cased for comparison while `--author` matching
> is case-sensitive. Any change to author matching needs `-F` and `-i` kept.

> **The CI audit blames a SUPERSET of what production does**, so its timings
> are not production's. The runner token cannot see the account's private org
> memberships, so `Nitjsefnie-OSC` and `Nitjsefnie-Games` repos come out
> external there and get blamed; on the box they are derived insiders and are
> never cloned. Those three repos are 33.2s of the audit's 44.1s blame phase,
> so production's is nearer 11s. This is deliberate — a superset is a stricter
> correctness check — but do not read the audit's wall time as the box's.

> **`CLONE_LOOKAHEAD` (default 3)** — how many repos are cloned ahead of the
> blame consuming them. Clone waits on the network, blame saturates the CPUs,
> so the overlap is close to free: measured 125.4s of 159.0s clone hidden,
> against 60s hidden at depth 1. It costs that many extra checkouts on disk
> and that many concurrent transfers, which is why it is bounded.
>
> **Depth 8 is faster and reads as a memory regression, but read the two
> instruments before believing that.** Depth 8 gets wall 101.6s -> 79.3s with
> cgroup peak 554.6MB -> 769.3MB, which is worse than the 624.6MB baseline —
> while the sampler's RSS sum only moves 221MB -> 327MB, far under the 894MB
> baseline on that same instrument. cgroup `memory.peak` **counts page cache**,
> and eight concurrent clones write much more file data, so most of that
> "regression" is reclaimable cache rather than process memory. Capping
> index-pack (`CLONE_PACK_THREADS`, `CLONE_WINDOW_MB`) confirmed it: 769MB ->
> 761MB, i.e. allocation was never the driver.
>
> The real cost of depth 8 is reliability: one of two runs lost a repo to
> `clone_failed` under eight concurrent transfers. The failure contract kept
> its old count and its stale oid forces a retry, so nothing was lost, but
> that is the thing to weigh — not the cgroup number.

> **`DEBUG_TIMING=1`** — per-repo clone/wait/fame seconds plus a phase summary
> that closes named phases against real elapsed time. The residual is the
> point: it was a stable ~36s of unattributed run until the fetch phases were
> wrapped, and is now ~2s. A growing residual means an unmeasured phase, not a
> fast run.

> The token file holds the **`gh` CLI's own OAuth token** (`gh auth token`,
> scopes incl. `repo`), not a narrow PAT. Safe only because the renderer filters
> at the query (`privacy: PUBLIC`, `render.py:227`) and skips `isPrivate` PRs —
> keep that filter, or private repos leak into public SVGs. Swap in a
> `read:user`+`public_repo` PAT if you want least privilege.
