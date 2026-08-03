# Parallel `git blame` inside a single repository — design spec

**Date:** 2026-08-03 · **Repos:** `Nitjsefnie-OSC/git-fame` (fork of `casperdcl/git-fame`), `/root/gh-widgets`
**HTML:** `docs/superpowers/specs/2026-08-03-git-fame-parallel-blame-design.html`

## Context

`render-impact.py` shells out to `git fame` once per external repository to count surviving
lines. Measurement showed git-fame spends **41 % of its wall time on process spawn alone**,
because it runs one serial `git blame` subprocess per file. This spec decides to fix the
dependency rather than work around it: patch git-fame in a fork, upstream the patch, and pin
the patched build.

## Decision

Add a `-j/--jobs` option to git-fame that parallelises the per-file blame subprocess, **on by
default**, and consume it from a pinned fork build.

Chosen over a caller-side workaround because the cost lives entirely inside the dependency.
Upstream already parallelises *across* repositories with `thread_map` (`_gitfame.py:441`), so
intra-repo parallelism follows the project's own precedent. The change deliberately alters no
output: only the subprocess call is parallelised; parsing and aggregation stay serial in the
main thread, consuming results in submission order, so output is byte-identical to `-j1`.

## Background — the measurement

git-fame spawns **N + 3 processes per repository** (N = text files): `ls-files`,
`git grep -I`, `shortlog`, then one `git blame --line-porcelain` per file, strictly serially
(`_gitfame.py:280`).

| Repository | Files | Processes | Wall | Pure spawn | Python side |
|---|---:|---:|---:|---:|---:|
| `bamdadd/context-leak` | 28 | 31 | 0.42 s | 91 ms (22 %) | 24 % |
| `barribob/bosses-of-mass-destruction` | 477 | 480 | 6.7 s | 2.75 s (41 %) | 7 % |

Fixed per-call cost is **~5.4 ms**, confirmed independently: blaming a **0-byte file** costs
5.44 ms against a 4.10 ms bare `git rev-parse`. Median blame is ~10 ms, so about half of every
blame call is fork/exec/repo-open.

Thread-pool headroom on the 477-file repo (prediction before running: 1.0–1.6 s at 8 workers):

| Workers | 1 | 4 | 8 | 16 |
|---|---:|---:|---:|---:|
| Blame phase | 8.1 s | 1.9 s | 1.6 s | 1.3 s |

Consumer impact: `render-impact.py` re-blames 1–17 repos per run; runs take 22 s–5 min.
Extrapolating the measured ~70 loc/file across 56 cached repos (5.95 M lines) puts a
`--resync` at **~85,000 blame processes, ~7.5 min of pure fork/exec**.

## Change inventory

| File | Change |
|---|---|
| `gitfame/_gitfame.py` (docstring) | Declare `-j, --jobs=<n>` with `[default: 0:int]`. `argopt` builds the parser from `__doc__`, so the docstring is the source of truth. |
| `gitfame/_gitfame.py:_get_auth_stats` | Accept `jobs`. Submit blame calls to a `ThreadPoolExecutor` with a bounded window of `2 × jobs` outstanding futures; consume in submission order, parse serially in the calling thread so `stats_append` needs no lock. Per-file exception handling keeps current semantics (log, skip). |
| `gitfame/_gitfame.py:run` | `jobs=0` → `min(32, (os.cpu_count() or 1) + 4)` (stdlib executor heuristic). With multiple gitdirs, pass `max(1, jobs // len(gitdirs))` inward. |
| `tests/test_gitfame.py` | Add `['-j', '4']` to `test_options`; add a determinism test asserting `-j1` output == `-j4` output. |
| `git-fame_completion.bash` | Add `-j` / `--jobs` to the completion word list. |
| `gitfame/git-fame.1` | Regenerate from the docstring via the Makefile target. |
| `/root/gh-widgets/test_impact.py` (new) | Regression test: assert installed git-fame accepts `-j` and that `-j4` output == `-j1` output. No timing assertion. |
| `/root/gh-widgets/CLAUDE.md`, `install.sh` | Record the fork pin, why it exists, and how to re-pin once the upstream PR merges. |

## Concurrency semantics

| `--jobs` | Blame workers | Behaviour |
|---|---|---|
| 0 (default) | `min(32, cpu_count+4)` | Auto; matches the stdlib executor heuristic. |
| 1 | 1 | Exactly the current serial path — escape hatch and the determinism test's control. |
| n > 1 | n | Explicit cap. |
| multiple gitdirs | `max(1, jobs // len(gitdirs))` | Outer `thread_map` already parallelises across repos; inner budget divided so the product stays bounded. |

Memory forces the bounded window rather than a plain `executor.map`: `--line-porcelain` emits
~250–400 bytes per source line, so buffering every file at once would reach gigabytes on a
1.3 M-line repo like `packit/ogr`. Peak stays **O(jobs × per-file output)**, not O(repo).

## Verification

- [ ] **Byte-identical output.** `git fame -e -w --format json -j1` vs `-j8` identical on ≥3 real repos of differing size.
- [ ] **Upstream suite green.** `pytest` in the fork passes, including `-W=error` and `--cov-fail-under=85`.
- [ ] **Speedup reproduced.** `-j8` beats `-j1` by ≥3× on the 477-file repo; recorded, not asserted.
- [ ] **Regression test fails on stock.** New gh-widgets test passes on the pinned fork, fails on git-fame 3.1.2 from PyPI.
- [ ] **End-to-end unchanged.** `render-impact.py` against a copied cache reproduces current `ourloc` counts for repos whose HEAD has not moved.
- [ ] **Deployed copies match.** `diff /usr/local/bin/render-impact.py /root/gh-widgets/render-impact.py` empty after `install.sh`.

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Memory blow-up on very large repos with an unbounded window | MED | Bounded window of `2 × jobs` is a hard requirement, verified against a large repo before the PR. |
| Upstream rejects default-on parallelism | LOW | Byte-identical output plus the existing `thread_map` precedent is the argument. If rejected, flip the default to `-j1` and pass `-j` from `render-impact.py` — a one-line change. |
| Oversubscription if the caller also parallelises its repo loop | LOW | Caller-side repo parallelism is explicitly out of scope. |
| Fork pin drifts from upstream releases | LOW | Pin recorded in `CLAUDE.md` with a re-pin instruction; return to PyPI once the PR merges. |

## Out of scope

- **Parallelising `blame_moved` in render-impact.py** — deferred until the git-fame fix is
  deployed and measured; doing both would oversubscribe a 12-core box.
- **Replacing per-file blame with a single `git log` pass** — would remove the N-process
  structure entirely but changes what "surviving lines" means; too large an upstream argument
  for a performance patch.
