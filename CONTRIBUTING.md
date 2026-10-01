# Contributing to gh-widgets

Issues and pull requests are welcome — especially if a widget renders wrong
for your account. This project draws SVGs from data GitHub returns, so a
report that says "my numbers are X, the widget says Y, here is the GraphQL
response" is the most valuable thing you can send.

## LLM and agent contributions are welcome

You may use an LLM or a coding agent to write your contribution. There is
no penalty, no separate review queue, and no expectation that you rewrite
its output by hand. Much of this repo was built that way.

Two conditions, and they are about honesty rather than provenance:

1. **Disclose the model** with a trailer on each commit it authored:

   ```
   Co-Authored-By: <Model Name> <noreply@example.com>
   ```

   e.g. `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. One
   primary-author trailer per commit.

2. **Do not submit claims you have not verified.** Paste the command and
   its real output. "Tests pass" without the run is not evidence, and an
   SVG change is easy to check — render it and look at it.

If a maintainer's reply reads like it was drafted by an agent, it probably
was. That is fine in both directions.

## The two constraints that reject the most patches

- **`render.py` is standard library only**, and `test_render.py` is too. A
  patch that adds `requests`, `jinja2`, or an SVG library to that path will
  be declined — the point is that it drops onto a server and runs. If you
  need HTTP, use `urllib.request`; if you need templating, use f-strings.

  `render-impact.py` is the documented exception: its blame pass shells out
  to the **`git` CLI** and to **git-fame** (pip). That is the whole reason
  it is a separate script with separate units and its own cache, rather
  than another card in `render.py`. Keep the boundary — new dependencies
  belong on the impact side, if anywhere.
- **Identity is derived, never configured.** Whose repos count as insider,
  and whose lines count as ours, come from the token's own account via
  `fetch_identity`. `GH_EXTRA_INSIDERS` / `GH_EXTRA_EMAILS` may only *add*
  to that set. A patch that lets configuration *replace* the fetched set
  will be declined: it reintroduces the staleness this design removed, just
  in a new location.
- **Ownership matching is exact.** Commit-author email in a third-party repo
  is attacker-controllable, so it is matched by exact set membership. Do not
  reintroduce a substring, prefix, or regex test.
- **Shared code lives in `ghwidgets_common.py`, `ghwidgets_cache.py`, and
  `ghwidgets_journal.py`**. Renderers load `ghwidgets_common.py` by path and
  version-check it via `COMMON_VERSION`. If you change that module's
  interface, bump `COMMON_VERSION` and both scripts' `REQUIRED_COMMON` —
  `test_common.py` fails if they drift apart.
- **No request-time work.** The renderer runs on a timer and writes files.
  Nothing in this repo may fetch, compute, or phone home when a browser
  loads the SVG. That is the failure mode of the hosted services this
  replaces.

Related: keep the query filtered. `render.py` asks for `privacy: PUBLIC` and
skips `isPrivate` pull requests. Removing either leaks private repository
names into a public SVG.

## Getting it running

No install step and no dependencies:

```sh
GH_USER=octocat GH_TOKEN=ghp_xxx OUT_DIR=./widgets ./render.py
```

A classic PAT with the documented `public_repo` scope is enough;
`read:user` is not required. `THEME=` picks a
palette; `CACHE_FILE=` points at the cache described in the README (GitHub
rejects a full-year contribution query on large accounts, so the cache is
load-bearing, not an optimisation).

See [SECURITY.md](SECURITY.md#credentials) for the full credential and scope statement.

## Tests

```sh
python3 -m unittest discover -v
```

Every case builds its own contribution calendar by hand — the suite never
touches the network, and it must stay that way. If you add a rendering
branch, add a case that pins its output; SVG regressions are invisible
until someone looks at a broken README.

**`unittest discover` is the runner and stays the runner**, and pytest has
nothing left to do here. It was `speed.yml`'s timing harness, collecting
these same TestCases and emitting `--junitxml`, which stdlib unittest cannot;
that job now counts CPU seconds with `scripts/ci/counter.py` and writes its
own JUnit. It was also imported by `test_compare_durations.py`, which
`unittest discover` had to be able to import in order to collect the suite;
that file is now stdlib unittest like everything else. `requirements-test.txt`
still pins it so a local `pytest` run of the same file works, and nothing in
CI asks it for anything. Do not write a test against pytest fixtures or
`assert`-rewriting — it would run in CI and then not run for anyone using
the documented command.

## CI

The workflow files in `.github/workflows/` are:

- `tests.yml` — unit tests and coverage.
- `lint.yml` — Python style and lint checks.
- `types.yml` — Python type checks.
- `audit.yml` — dependency vulnerability checks.
- `codeql.yml` — security analysis.
- `actionlint.yml` — workflow syntax and security checks.
- `speed.yml` — counts **CPU seconds** for each renderer workload and for
  the unit suite, on pinned offline fixtures, and compares each against a
  committed baseline in `speed-baseline.json` that only ever ratchets down.
  CPU seconds rather than a deterministic instruction or syscall count,
  because neither survives measurement on the cell that reads the baseline:
  one is refused by the kernel's `perf_event_paranoid`, the other costs
  12.8× the work it measures. The gate is therefore a **step-change
  detector, not a regression detector** — it catches a gross step change
  and the gross wall smoke bound; it will not catch a 20 % regression
  anywhere, and it does not catch a doubling either. The baseline records an observed **range** per entry rather than
  a single number, because these counters are not deterministic and a budget
  wide enough to cover their spread would be a gate that cannot fire.
- `aggregate.yml` — reports on every pull request and push to `main`, regardless
  of paths (issue #89); branch protection requires it instead of the path-filtered gates.
- `release.yml` — waits for gates, then tags and publishes releases.
- `coverage-ratchet.yml` — measures coverage on `main` and announces when
  measured coverage exceeds the committed floor. The floor only moves
  through a pull request.
- `pr-gate.yml` — pull request policy checks.
- `claim.yml` — issue assignment commands.
- `targeted-blame-audit.yml` — contribution-counting correctness audit.
- `gitfame-resync-memory.yml` — resync memory measurements.
- `gitfame-pool-probe.yml` — worker-pool measurements.

These checks can run locally — **on ONE platform, whatever platform that
is**, and that is the whole of what they cover:

```sh
python3 -m unittest discover -v                                  # tests
python3 -m coverage run --source=. --omit='test_*.py' \
  -m unittest discover && python3 -m coverage report \
  && python3 -m coverage json -o coverage.json                  # coverage
python3 scripts/ci/coverage_ratchet.py gate --coverage-json coverage.json
git ls-files -co --exclude-standard '*.py' | xargs pylint        # lint
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle   # lint
pyright                                                          # types
pip-audit -r requirements-dev.txt -r requirements-test.txt       # audit
actionlint .github/workflows/*.yml && zizmor .github/workflows/  # actionlint
```

**THOSE COMMANDS ARE NOT WHAT DECIDES WHETHER A CHANGE IS MERGEABLE.**
`tests.yml` runs the suite across an OS × Python matrix —
windows-latest, macos-latest and ubuntu-latest on 3.10 and 3.13 — and a
green run of everything above on one machine says nothing about the other
five. A POSIX-only import, a `/proc` read, a path separator, a `case` in a
shell script: all four shipped past a fully green local checklist in this
repository's history, because the checklist does not run them.

Run one cell locally if you can — a Linux-only dependency does not stop
`python3 -m unittest discover` exercising a suite that also has to import on
Windows, and `python3 -m unittest test_the_thing` is usually enough to see
an import error the full run only reports as a mass failure. What you cannot
do locally is the reverse: nothing above exercises a POSIX-only import on a
platform that has none, and nothing above runs a shell script under bash on
Windows at all. **So if your change touches anything platform-shaped —
imports, `/proc`, `/dev`, signals, file modes, shell in a workflow — open the
pull request and let the matrix decide.** That costs a reader nothing and it
is the only instruction here that has no local substitute.

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. The coverage population is **every shipped source family**,
`scripts/` included — the CI scripts and the bench harness all run in
workflows, so leaving them out reported coverage for a smaller program than
the one that ships. The floor lives in `coverage-floor.json` at the repository
root as committed data. When measured coverage exceeds it,
`coverage-ratchet.yml` announces the climbable floor with an annotation and step
summary. Raising it is a deliberate pull request: download the
`coverage-floor-candidate` artifact, replace `coverage-floor.json`, and open a
pull request, or run `scripts/ci/coverage_ratchet.py raise` locally. The floor
is never lowered — no code path in `scripts/ci/coverage_ratchet.py` can write a
smaller number than it read. Raise coverage by writing tests; never by editing
the floor. One consequence worth knowing before you add a source module: the
gate deliberately judges coverage without comparing file counts, so `tests.yml` stays
green on your pull request, and the file-count check fires only after merge, on
`main` — where it blocks every release until someone re-derives the floor.

The rest need GitHub: `codeql` (security analysis, Python only — this repo
has no JS; weekly cron, because a query published today would otherwise
only ever run against files touched after it shipped), `speed` (counts the
work this commit does in CPU seconds — per renderer workload and for the
unit suite, on pinned offline fixtures — and fails when a committed,
down-only baseline is exceeded; with no baseline committed it reports what
it measured and exits 0, because no data is not a regression; elapsed time
survives only as a gross smoke check that reports no number), `release`
(tags `v<VERSION>` once every gate a `VERSION` push
schedules — `lint`, `pyright`, `speed`, `pip-audit`, `analyze`, `unittest`,
`aggregate` —
has both *reported* and passed; a gate that never reports stops the release
rather than shrinking the bar, and a `workflow_dispatch` may waive one by
name, loudly), and the three bespoke `gitfame-*` / `targeted-blame-audit`
measurement workflows that were already here.

The measured Ubuntu/Python 3.13 cell in `tests.yml` installs the pinned
Nitjsefnie-OSC/git-fame build and requires its pin checks; if the binary is
missing, those checks fail instead of skipping. Other matrix cells skip those
environment-conditional checks when git-fame is absent.

**Release = edit `VERSION`.** One bare semver line at the repo root, no
leading `v`. `REPO_VERSION` in `ghwidgets_common.py` reads it — and note
it is NOT `COMMON_VERSION`, which is the interface-compatibility marker
between that module and the renderers. Bumping one never means the other
moved. A deployed copy has no `VERSION` beside it (`install.sh` copies the
scripts to `/usr/local/bin/`), so `REPO_VERSION` reads `"unknown"` there,
which is correct rather than a bug.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
hold a repository token. Dependabot keeps the hashes current.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. Note the shape of this repo — the renderers and their tests
live at the ROOT, so the root block names back `*.py` directly, which is
exactly why `__pycache__/` must stay denied and never re-opened. A new
file of an unlisted type is invisible to git and will NOT appear in
`git status`; `git check-ignore -v <path>` names the rule hiding an
UNTRACKED file. For a file the repo tracks, add `--no-index`, because
`check-ignore` otherwise consults the index and a tracked path is never
subject to the rules.

## House style

- **Python** — stdlib only, type hints where they help, no framework.
- **SVG** — emitted as plain strings. There is no template engine and no
  DOM library; match the surrounding code.
- Themes live in one table. Adding a theme means adding a row, not a
  special case elsewhere.
- There is no linter or formatter config. Match the surrounding file.

## Claiming an issue

Comment `/claim` on an open, unassigned issue to be assigned to it — no write
access needed. `/unclaim` (or `/release`) drops your own assignment. The
comment must be exactly the command; anything else is declined with a reply.
Check that your login actually appears among the assignees before starting.

## Pull requests

Small and single-purpose beats large and comprehensive. Fill in the pull
request template: an automated gate checks the description against it and
closes a pull request whose sections are missing, reordered or still carry
the template's instruction comments. It reopens the pull request once the
description is fixed; push once more after that reopen (an empty commit is
enough) to get CI. The gate also requires the issue the pull request resolves
to be assigned to you, so claim it first.

In Testing, include the output of the test run and — for anything that
changes rendering — the before and after SVG, or a screenshot of both.

If you are unsure whether something is a bug or intended, open an issue and
ask. A wrong premise caught early is cheaper than a correct fix to the
wrong problem.
