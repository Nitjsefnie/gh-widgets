# Parallel `git blame` inside a single repository — design spec

**Date:** 2026-08-03 · **Repos:** `Nitjsefnie-OSC/git-fame` (fork of `casperdcl/git-fame`), `/root/gh-widgets`
**HTML:** `docs/superpowers/specs/2026-08-03-git-fame-parallel-blame-design.html`

## Context

`render-impact.py` shells out to `git fame` once per external repository to count surviving
lines. Stock git-fame spawns one serial `git blame` subprocess per file, so this spec decides to
fix the dependency rather than work around it: patch git-fame in a fork, upstream the patch,
and pin the patched build.

## Decision

Add a `-j/--jobs` option to git-fame that parallelises the per-file blame subprocess, **on by
default**, and consume it from a pinned fork build.

Chosen over a caller-side workaround because the cost lives entirely inside the dependency.
Upstream already parallelises *across* repositories with `thread_map` (`_gitfame.py:441`), so
intra-repo parallelism follows the project's own precedent. The change deliberately alters no
output: only the subprocess call is parallelised; parsing and aggregation stay serial in the
main thread, consuming results in submission order, so output is byte-identical to `-j1`.

## Background — process structure

git-fame spawns processes per repository: `ls-files`, `git grep -I`, `shortlog`, then one
`git blame --line-porcelain` per text file, strictly serially (`_gitfame.py:280`). The fixed
per-call cost of `git blame` is dominated by fork/exec and repository open, so serial blame
subprocesses are the bottleneck even when the actual blame work is small.

## Change inventory

| File | Change |
|---|---|
| `gitfame/_gitfame.py` (docstring) | Declare `-j, --jobs=<n>` with `[default: 0:int]`. `argopt` builds the parser from `__doc__`, so the docstring is the source of truth. |
| `gitfame/_gitfame.py:_get_auth_stats` | Accept `jobs`. Submit blame calls to a `ThreadPoolExecutor` with a bounded window of `2 × jobs` outstanding futures; consume in submission order, parse serially in the calling thread so `stats_append` needs no lock. Per-file exception handling keeps current semantics (log, skip). |
| `gitfame/_gitfame.py:run` | `jobs=0` → `min(32, (os.cpu_count() or 1) + 4)` (stdlib executor heuristic). With multiple gitdirs, pass `max(1, jobs // len(gitdirs))` inward. |
| `tests/test_gitfame.py` | Add `['-j', '4']` to `test_options`; add a determinism test asserting `-j1` output == `-j4` output. |
| `git-fame_completion.bash` | Add `-j` / `--jobs` to the completion word list. |
| `gitfame/git-fame.1` | Regenerate from the docstring via the Makefile target. |
| `/root/gh-widgets/test_impact.py` (new) | Regression test: assert installed git-fame accepts `-j` and that `-j4` output == `-j1` output. No timing assertion. Plus a test that the runtime check is *noisy on success*. |
| `/root/gh-widgets/render-impact.py` | `check_git_fame()`, called from `main()` after `parse_args()`: prints the installed git-fame version on every render, warns loudly on stock, never fatal. |
| `/root/gh-widgets/CLAUDE.md`, `install.sh` | Record the fork pin, the version marker that distinguishes it, why it exists, and how to re-pin once the upstream PR merges. |

### Amendment (2026-08-03) — guard at run time, not just install time

Adopted from `Consultest-CZ/kvalita`, which hit both failure modes after
patching a dependency the same way (`2bca15f` → `e29fc78` → `f30eed6`):

1. **A patched build that reports stock's version gets silently reverted** by a
   routine `pip -U`. *Not applicable here, verified*: git-fame derives its
   version from tags via `setuptools_scm`, so the fork build reports
   `3.1.4.dev1+g<sha>` against stock `3.1.2`/`3.1.3`. No wheel retagging step —
   adding one would be cargo-culting a fix for a problem this project does not
   have.
2. **A guard that is silent on success is indistinguishable from a guard that
   never ran.** *Applicable, and the original design missed it*: `install.sh`
   checks at install time, but `render-impact.service` runs unattended on a
   timer long afterwards. Hence `check_git_fame()`, which logs the version on
   every render. The absence of its line is the alarm — which only works
   because success is noisy.

Deliberately **not** changed: `blame_repo` still does not pass `-j`. The patched
build parallelises by default, so leaving the flag off means a stock install
degrades to "slow" rather than "every repo errors" — and an erroring repo caches
an `{"error", head}` entry that `update_loc` then skips until HEAD moves,
silently dropping the repo from the table.

## Concurrency semantics

| `--jobs` | Blame workers | Behaviour |
|---|---|---|
| 0 (default) | `min(32, cpu_count+4)` | Auto; matches the stdlib executor heuristic. |
| 1 | 1 | Exactly the current serial path — escape hatch and the determinism test's control. |
| n > 1 | n | Explicit cap. |
| multiple gitdirs | `max(1, jobs // len(gitdirs))` | Outer `thread_map` already parallelises across repos; inner budget divided so the product stays bounded. |

Memory forces the bounded window rather than a plain `executor.map`: `--line-porcelain`
output is large compared to the source file, so buffering every file at once would reach
gigabytes on a large repository. Peak stays **O(jobs × per-file output)**, not O(repo).

## Verification

- [ ] **Byte-identical output.** `git fame -e -w --format json -j1` vs `-j8` identical on ≥3 real repos of differing size.
- [ ] **Upstream suite green.** `pytest` in the fork passes, including `-W=error` and `--cov-fail-under=85`.
- [ ] **Regression test fails on stock.** New gh-widgets test passes on the pinned fork, fails on git-fame 3.1.2 from PyPI.
- [ ] **Runtime guard is self-proving.** A real `render-impact.py` run prints a `git-fame: <version> with --jobs` line; a run against stock prints the warning instead. Neither case is silent.
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
  deployed and evaluated; doing both would oversubscribe the host.
- **Replacing per-file blame with a single `git log` pass** — would remove the N-process
  structure entirely but changes what "surviving lines" means; too large an upstream argument
  for a performance patch.
