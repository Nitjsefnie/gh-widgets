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
- **Shared code lives in `ghwidgets_common.py`**, loaded by path and
  version-checked via `COMMON_VERSION`. If you change that module's
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

A classic PAT with `read:user` + `public_repo` is enough. `THEME=` picks a
palette; `CACHE_FILE=` points at the cache described in the README (GitHub
rejects a full-year contribution query on large accounts, so the cache is
load-bearing, not an optimisation).

## Tests

```sh
python3 -m unittest discover -v
```

Every case builds its own contribution calendar by hand — the suite never
touches the network, and it must stay that way. If you add a rendering
branch, add a case that pins its output; SVG regressions are invisible
until someone looks at a broken README.

## House style

- **Python** — stdlib only, type hints where they help, no framework.
- **SVG** — emitted as plain strings. There is no template engine and no
  DOM library; match the surrounding code.
- Themes live in one table. Adding a theme means adding a row, not a
  special case elsewhere.
- There is no linter or formatter config. Match the surrounding file.

## Pull requests

Small and single-purpose beats large and comprehensive. In the description,
include what changed and why, the output of the test run, and — for
anything that changes rendering — the before and after SVG, or a screenshot
of both.

If you are unsure whether something is a bug or intended, open an issue and
ask. A wrong premise caught early is cheaper than a correct fix to the
wrong problem.
