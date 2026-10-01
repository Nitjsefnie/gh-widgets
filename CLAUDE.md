# gh-widgets — deployment and unit notes

Moved out of `/root/CLAUDE.md` so it loads only when working in this repo.

**The units live in this repo now, in `units/`.** They used to be hand-maintained
in `/etc/systemd/system` and tracked nowhere, which is the same drift trap the
renderers themselves have. There are **four** files, and they replaced **ten**:

| unit | when | what |
|---|---|---|
| `gh-widgets.service` + `.timer` | hourly | all three renderers, in sequence |
| `gh-widgets-resync.service` + `.timer` | Sun 04:17 UTC | all three renderers in sequence; `--resync` is passed to `render-gh-widgets.py` and `render-impact.py` only |

The five old `render-*` service/timer pairs are **gone**; ordering that used to
be expressed with `After=` chains between separate units is now just the order
of the `ExecStart` lines. Read `units/*.service` for the env and the reasoning —
do not re-derive it here, and do not edit `/etc/systemd/system` by hand.

**Deploy with `/root/gh-widgets/install.sh` — never `cp`, never a hand-edited
unit.** Bare, it installs a nine-file deployment set to `/usr/local/bin`
(`render.py` → `render-gh-widgets.py`, `render-impact.py`, `impact_loc.py`,
`render-responsiveness.py`, `ghwidgets_common.py`, `ghwidgets_cache.py`,
`ghwidgets_journal.py`, `impact_clone.py`, `ghwidgets_data.py`). The renderers
load `ghwidgets_common.py` and assert its `COMMON_VERSION` at startup; that
module imports the adjacent cache and journal modules, and `impact_loc.py`
imports the adjacent clone module. `ghwidgets_data.py` is installed alongside
as the separate public API for pinned consumers; the renderers do not import
it. A partial deployment missing one of these runtime modules refuses to run,
deliberately preventing rendering from stale code. `install.sh --units`
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

The allowed issue state matrix is — every `state_reason` is `null` or a
non-empty, non-blank string of at most `MAX_STATE_REASON_LENGTH` characters,
and each row adds the rules its state imposes:

| record | `state` | `closed_at` | `state_reason` |
|---|---|---|---|
| issue | `OPEN` | `null` | `null`, `REOPENED`, or any reason string other than `COMPLETED` or `NOT_PLANNED` |
| issue | `CLOSED` | RFC 3339 | any reason string that is not `null` or `REOPENED` |

**`state_reason` is deliberately not a closed enum.** `state` and `closed_at`
are exact; the reason column is bounded, not enumerated. GitHub's
`IssueStateReason` is upstream-controlled and already returns members this
contract has never named — `DUPLICATE` appears 19 times in this account's own
fetched issue history — so pinning the accepted set to the reasons known today
would make `fetch_authored_snapshot` reject its own output. v1 instead
enforces consistency only:

- An `OPEN` issue may not carry `COMPLETED` or `NOT_PLANNED` — a closure reason
  contradicts an open state.
- A `CLOSED` issue requires `closed_at`, and a reason that is neither `null`
  nor `REOPENED`.
- **Any other reason string is accepted and preserved verbatim.** Unknown and
  future reasons cross the boundary by design, bounded only by the length
  limit, so a consumer can see the exact source spelling and reconcile it
  against its own known set instead of losing the record to a parse failure.
  A consumer that cares about specific reasons must match them itself.

The complete allowed pull-request state matrix is:

| record | `state` | `merged` | `closed_at` | `merged_at` |
|---|---|---|---|---|
| pull request | `OPEN` | `false` | `null` | `null` |
| pull request | `CLOSED` | `false` | RFC 3339 | `null` |
| pull request | `MERGED` | `true` | RFC 3339 | RFC 3339 |

No other pull-request state/merge/null combination is valid. Open items
cannot have `closed_at`; a closed issue requires a reason that is neither
`null` nor `REOPENED` (and accepts any other reason string); PRs do not have a
`state_reason` field; a `MERGED` PR must have both outcome timestamps; and
`OPEN` or `CLOSED` PRs must be unmerged with `merged_at: null`.
`test_data.py` pins every one of these cells against the validator, so the prose
above cannot drift away from the code without a test failing.

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
> for byte-identical output (measured — see below; the memory columns carry
> that argument, the wall columns in those tables do not).
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
> **Wall time is not a magnitude on this box or on a CI runner.** Both are
> multi-tenant, with steal time, CPU model and thermal state nobody owns, so
> a wall figure moves for reasons unrelated to the code — and paired wall A/B
> is not a magnitude EVEN WITHIN ONE JOB, which is the case people reach for
> when they doubt the general claim. Quote a load-invariant quantity
> instead: CPU time, statement and file counts, output bytes. The obvious
> first choice is an instruction count, and it is what a CI RUNNER here
> refuses: image `20260927.320.1` runs `perf_event_paranoid: 4` and `perf
> stat -e instructions` exits 255 on it (run `36811152307`; `counter.py`
> scopes the figure). This box is not that — its own `perf_event_paranoid` is
> 3 and the counter works. Syscall counting works on the runner and costs
> 12.8× the work it measures, which is why the speed gate uses CPU seconds
> (`CONTRIBUTING.md` carries the full account of that gate).
> **A wall figure that survives in this file is an indicative observation**,
> labelled as one, and must never be the load-bearing evidence for a
> decision. The memory tables below need no hedge: cgroup and RSS peaks are
> sizing, not wall.
>
> **This file does not have a distribution for the cell's wall noise, and
> that is a fact rather than a gap.** The run history groups by date, not by
> the commit each run measured, and the measured modules changed repeatedly
> across the dates it covers, so a same-content spread cannot be read off
> it at all. What IS measured, per entry per measurement window — and a window
> is a single sha, so these are same-content by construction — is 5.1%-50.0%
> of CPU seconds, and 45.8%-55.4% across the unioned envelopes. Read that as
> the size of the effect, not as its shape.
>
> `perf-blame` is `parallel-blame` plus `--incremental` blame parsing: the
> parse only ever consumed chunk headers, while `--line-porcelain` re-emits
> every commit header per LINE and both porcelain formats emit the file's
> whole content. The load-invariant half of that: 53.8MB of blame output
> became 5.1MB on a 107k-loc repo, and the output is byte-identical,
> including on a 1.65M-loc repo. The wall half — parse 0.90s -> 0.11s, blame
> phase of a full resync -18.6% — is an INDICATIVE OBSERVATION from those
> runs, not a magnitude: it is measured where the machine's load is not ours
> to fix. The bytes and the equality are the argument; the seconds only
> agree with it, and should not be carried forward without them.
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
> them. **Measured 2026-08-10 — it costs roughly 2.2× peak memory for
> byte-identical output.** A full `--resync` over the same 59 repos, two runs
> per build on a GitHub runner (workflow run `31386157081`,
> `gitfame-resync-memory` — never on this box). Memory is load-invariant
> sizing; the wall column is not, and is here as an indicative observation:
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
> The wall column is an indicative observation and nothing more: "under 1%"
> is a statement about two builds on one runner, not a magnitude, and the
> load-invariant half of this measurement says the same thing without the
> wall clause at all — the memory is spent to buy byte-identical output.
>
> **So the pin went back to the fork on 2026-08-10 (operator instruction),
> after a brief move to 4.0.0.** Paying 2.2× peak memory for byte-identical
> output is the whole argument, and it stands on the memory columns alone;
> the earlier decision to move had been taken on a *wall-time* cost
> structure, before anyone had measured memory. The peak scales with the
> largest single repo blamed (`Nitjsefnie-OSC/codex`, 1.65 M LOC), not with the
> repo count, so it grows as that repo does.
>
> **Re-measured 2026-09-30 against the CURRENT pin (`65925d8`, perf-blame):
> the fork now wins on cgroup peak memory by roughly 2×, and on wall time
> too.**
> The memory half is the load-bearing one and is what the pin rests on; the
> wall half is an indicative observation, and the 2026-08-10 wall result
> above is the reminder that a wall reading on this cell is not a magnitude
> in either direction. Workflow run `36718311078`, 58 repos, two runs per
> build, all valid with identical output:
>
> | build | cgroup peak | sampler tree peak | wall |
> |---|---|---|---|
> | fork `65925d8` | 642.4 / 636.7 MB | 350.9 / 357.4 MB | 241.9 / 247.4 s |
> | upstream 4.0.0 | 1209.9 / 1215.1 MB | 2166.9 / 1683.3 MB | 365.8 / 374.6 s |
>
> The blame-phase figures in those runs — 155 s against 282 s — are wall
> observations like every other here. The run that would settle the direction
> is not evidence and must not be read as one: `36714006796`, earlier the
> same day, had upstream about 6% FASTER, and its fork arm was still the
> older `a99855d3`, so it compared a different build against the current one.
> That is why the pin is decided on memory, where both arms are the same
> build and the comparison is like-for-like. The measurement workflows now
> install the documented pin, and `test_ci_workflows.py` fails if they drift
> from it again. The fork's `git fame` process never enters the sampler's
> top-12 list, so that
> column reads 0 for it: it is below every listed process, not unmeasured.
>
> **There is deliberately no pin-expiry check in CI, and none should be
> added.** `pin-still-needed.yml` was deleted and stays deleted. Its question —
> has upstream shipped `--jobs` yet — is answered permanently and was never the
> reason for the pin. The live reason is a memory property that no `--help`
> probe can see, and it does not decay on a schedule, so a recurring check
> would only ever produce false "you can unpin now" pressure. If you want to
> re-test the gap, re-run the measurement workflow, on a runner.
>
> The measurement is now recorded upstream, on
> <https://github.com/casperdcl/git-fame/pull/132#issuecomment-5342513092>
> (2026-08-19, operator-instructed). That was a one-off lift of a standing
> block on `casperdcl/git-fame` — no PR, no issue, nothing further there
> without another explicit instruction. See
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
> `targeted` (**the default**, `impact_loc.py`) blames only the files our own
> commits touched and takes `total` from a line count of the files
> `git grep -I .` calls text; `fame` runs git-fame over every file; `both`
> runs the two and **fails** on any disagreement. The unit sets no
> `BLAME_METHOD`, so production renders with `targeted` and git-fame does not
> run at all there. The blame phase measured 535.6s -> 61.2s over 59 repos —
> an indicative wall observation, not a magnitude; what is load-invariant
> here is that `targeted` blames a strict subset of what `fame` does, which
> is also why `both` exists to cross-check it.
>
> **Why the weekly `both` audit exists.** `targeted` selects candidate files
> from our own history, so a rename made by somebody ELSE after our commit
> used to move our lines to a path that history never mentions — measured
> under-counts of up to 99% (warior456/Sculk-Depths 313 -> 4, and four more).
> `rename_closure()` now resolves our touched paths forward through the rename
> chain (`git log --diff-filter=R --name-status`, including
> `--diff-merges=first-parent`, because a rename performed in a merge is
> otherwise invisible). `total` was exact throughout; the error was purely in
> candidate-file selection.
>
> That fix is verified, not proven: the ground truth is blame's own rename
> detection and it is not reconstructible from `git log`. What makes shipping
> it reasonable is the audit — **`targeted-blame-audit.yml`** runs `both` every
> Monday 05:23 UTC. The verdict is the renderer's own exit code: under `both`
> it exits non-zero on any disagreement, so nothing has to re-parse a log.
> **It must carry the production address set**: the earlier 59/59 that briefly
> justified enabling `targeted` was measured on the derived noreply addresses
> alone, and auditing a different identity than the one that renders the card
> is not an audit. The workflow passes `GH_EXTRA_EMAILS` for exactly that
> reason.
>
> The audit lived inside `gitfame-resync-memory.yml` until 2026-08-19, under a
> name that described only that workflow's other half. That one is now
> dispatch-only memory measurement and carries no schedule.
>
> Both workflows resolve `IMPACT_REPO_PINS` first (`scripts/resolve-repo-pins.py`):
> one commit per repo, fixed before any arm runs. Without it the two methods —
> or the two git-fame builds — clone minutes apart and a repo that moves in
> between is measured at two different sizes. Under a manifest, an unhonourable
> pin or an unpinned candidate ends the run; there is deliberately no fallback
> to the live branch tip.
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
> never cloned. Those three repos were 33.2s of the audit's 44.1s blame phase,
> so production's would be nearer 11s — indicative wall figures, like every
> wall figure here. The load-invariant statement is the one to keep: the audit
> blames strictly MORE repos than production does, on purpose, because a
> superset is a stricter correctness check. Do not read the audit's wall time
> as the box's, as production's, or as a magnitude at all: it is the wall of
> a shared CI runner measuring a different program.

> **`CLONE_LOOKAHEAD` (default 3)** — how many repos are cloned ahead of the
> blame consuming them. Clone waits on the network, blame saturates the CPUs,
> so the overlap is close to free: those runs measured 125.4s of 159.0s
> clone hidden against 60s hidden at depth 1, an indicative wall observation
> on a runner this variable. What the setting actually trades is load-invariant
> — extra checkouts on disk and that many concurrent transfers, which is why
> it is bounded — and the wall figure only agrees with that.
>
> **Depth 8 read as a memory regression, but read the two instruments before
> believing that.** Its cgroup peak went 554.6MB -> 769.3MB against the
> 624.6MB baseline, while the sampler's RSS sum only moves 221MB -> 327MB —
> far under the 894MB baseline on that same instrument. (Depth 8's wall,
> 101.6s -> 79.3s, is an indicative observation and is not part of this
> argument either way.)
> cgroup `memory.peak` **counts page cache**,
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
> point: before the fetch phases were wrapped a stable ~36s of each run was
> unattributed, and is now ~2s — indicative wall observations, and the
> diagnostic they evidence is that nothing is left unaccounted for, not how
> long any part takes. A growing residual means an unmeasured phase, not a
> fast run.

> The token file holds the **`gh` CLI's own OAuth token** (`gh auth token`,
> scopes incl. `repo`), not a narrow PAT. Safe only because the renderer filters
> at the query (`privacy: PUBLIC`, `render.py:227`) and skips `isPrivate` PRs —
> keep that filter, or private repos leak into public SVGs. Swap in a
> `read:user`+`public_repo` PAT if you want least privilege.
