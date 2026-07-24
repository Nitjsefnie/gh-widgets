# Design spec — fetched identity, env-driven knobs, shared helpers

**Date:** 2026-07-24
**Supersedes/relates to:** `2026-07-21-widget-cache-design.md`

## Context

`render-impact.py` hardcodes the facts that define *whose* impact it measures:
a literal `INSIDERS` set of five org names and a literal `OUR_EMAIL`, matched
by substring (`"nitjsefnie" in em`). `render.py` derives the same insider
concept from the API instead. The two scripts also carry ~200 lines of
byte-identical duplicated code.

This spec replaces the constants with fetched identity, moves the metric knobs
to the environment, and extracts the duplication into one shared module.

## Decision

1. **Identity is derived from the token's own account**, not configured.
   Env vars may only *add* to the derived set, never replace it — an override
   would reintroduce the same staleness the constants have.
2. **The four metric knobs become env-driven** with the current values as
   defaults.
3. **The duplication moves to `ghwidgets_common.py`**, a third file loaded by
   path, with a version constant both scripts assert against.
4. **`install.sh` copies all three files**, so a partial copy stops being
   possible.

### Why not just move the constants to env vars

A new org would still count as external until someone edits a unit file. That
is the same failure in a different location, not a fix.

## Verified facts this design rests on

| Fact | Value | How verified |
|---|---|---|
| `user.databaseId` for `Nitjsefnie` | `75166987` | GraphQL query, 2026-07-24 |
| Constructed noreply address | `75166987+Nitjsefnie@users.noreply.github.com` | Matches the address recorded in `/root/CLAUDE.md` |
| Fetched org list | `BrainByteQuiz, Consultest-CZ, West-Scripts, Nitjsefnie-Games, Nitjsefnie-OSC` | GraphQL; **identical** to the current `INSIDERS` literal |
| `user.email` availability | **Not fetchable** | Production token lacks `read:user`; requesting the field fails the entire query with `INSUFFICIENT_SCOPES` |

The third row is the important one: the fetched set reproduces the hardcoded
set exactly, so this refactor changes no rendered number today.

The fourth row constrains the design — `email` must not appear in the query.

## Identity model

```
insiders = casefold({login} ∪ fetched_orgs ∪ split(GH_EXTRA_INSIDERS))

our_emails = lower(
    {f"{databaseId}+{login}@users.noreply.github.com",   # modern noreply
     f"{login}@users.noreply.github.com"}                # legacy noreply
    ∪ split(GH_EXTRA_EMAILS)
)
```

Matching is **exact set membership**, replacing the substring test. The box
address (`zmatek.peter@gmail.com`) moves from source constant to
`GH_EXTRA_EMAILS` in the unit files.

A repo is external iff it is public **and** its owner's login (casefolded) is
not in `insiders` — one predicate, shared by both scripts.

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `GH_EXTRA_INSIDERS` | *(empty)* | Comma-separated extra owner logins to treat as insiders. Additive. |
| `GH_EXTRA_EMAILS` | *(empty)* | Comma-separated extra addresses counted as ours in the blame pass. Additive. |
| `IMPACT_Z` | `2.58` | Wilson lower-bound z score. |
| `IMPACT_PR_GAMMA` | `1.0` | Volume exponent, PR table. |
| `IMPACT_ISSUE_GAMMA` | `1.75` | Volume exponent, issue table. |
| `IMPACT_LOC_GAMMA` | `0.5` | Volume exponent, live-code table. |

Unparseable numeric values **exit non-zero**. Silently falling back to the
default would render wrong numbers with no signal, which is the failure mode
this whole spec exists to remove.

## Change inventory

| File | Change |
|---|---|
| `ghwidgets_common.py` | **New.** Shared code + identity derivation + `COMMON_VERSION`. |
| `render.py` | Import shared code by path; derive insiders via the shared predicate; drop local duplicates. Stays stdlib-only. |
| `render-impact.py` | Delete `INSIDERS` / `OUR_EMAIL`; fetch identity; read knobs from env; exact email matching; import shared code. |
| `test_render.py` | Point at the new module layout. |
| `test_common.py` | **New.** Identity derivation, env parsing, exact-match ownership. |
| `install.sh` | **New.** Copies all three files to `/usr/local/bin`, refuses a partial copy. |
| `README.md`, `CONTRIBUTING.md` | Document the three-file deploy and the new env vars. |

## Module boundary

`ghwidgets_common.py` holds only what both scripts genuinely share:

- theme table and `FONT`
- `gql()` with the retry contract
- `PR_QUERY`, `fetch_pull_requests()`, `fetch_issues()`
- `load_cache()`, `save_cache()`
- `fmt_short()`, `xml_escape()`, `base_card()`, `stamp_cache_notice()`
- identity: `fetch_identity()`, `insider_set()`, `is_external()`, `our_emails()`

Card rendering, the metric math, and the blame pass stay in their own scripts.

## Version assertion

`ghwidgets_common.COMMON_VERSION = 1`. Each script declares the version it was
written against and exits non-zero on mismatch, so copying one file without the
other fails loudly at startup instead of rendering wrong numbers.

## Loading strategy

Both scripts load the module by explicit path relative to their own `__file__`
via `importlib.util.spec_from_file_location` — the pattern already used by
`test_render.py`. No `sys.path` mutation, no packaging, no build step.
Deployment renames `render.py` to `render-gh-widgets.py`, so import-by-name
would break; import-by-path does not care about the script's own filename.

## Verification

- `python3 -m unittest discover -v` passes.
- `render.py` still imports nothing outside the stdlib.
- A dry run of both scripts against the live token produces the same
  `impact.svg` tables as the 15:39 run, since the fetched insider set equals
  the old literal.
- Deliberately corrupt `COMMON_VERSION` in one file and confirm both scripts
  refuse to start.

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| An email used historically is in neither derived form nor `GH_EXTRA_EMAILS`, undercounting owned lines. | MED | Additive `GH_EXTRA_EMAILS`; weekly `--resync` re-blames everything, so a corrected list takes effect within a week. |
| Partial copy to `/usr/local/bin` leaves a stale module. | LOW | `COMMON_VERSION` assertion + `install.sh` copying all three. |
| A private org membership is invisible to the token, so its repos count as external. | LOW | Pre-existing in `render.py`; `GH_EXTRA_INSIDERS` covers it. |
| Three files instead of one weakens the copy-deploy story. | LOW | `install.sh` is now the documented path; the manual `cp` remains possible. |

## Out of scope

- **The `max_pages=50` truncation interacting with the "absent ⇒ merged"
  inference.** Real but latent, repaired weekly by `--resync`, and unrelated to
  constants. Tracked separately.
- **Retuning `Z` or the gammas.** They become configurable; their values do not
  change.
- **Vendoring `impact_stats.py` / `clone_fame.py`.** The validation scripts the
  knob comment cites live only in an ephemeral scratchpad. Worth recovering into
  the repo, but that is its own change.
