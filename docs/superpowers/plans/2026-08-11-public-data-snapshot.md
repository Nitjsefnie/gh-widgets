# gh-widgets Public Data and Snapshot API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose gh-widgets' GitHub identity, pagination, normalization, and snapshot handling as a stable importable API while preserving every standalone renderer.

**Architecture:** Add one dependency-free module beside `ghwidgets_common.py`. Existing GraphQL primitives remain in `ghwidgets_common`; the new module owns normalized records and a versioned atomic snapshot. Existing CLIs keep their default fetch/cache path and gain only additive snapshot inputs.

**Tech Stack:** Python 3.9+ stdlib, GitHub GraphQL, `unittest`, existing gh-widgets cache locking/atomic-write utilities.

## Global Constraints

- `render.py`, `render-impact.py`, and `render-responsiveness.py` remain independently runnable with their current commands and defaults.
- Existing private cache schemas are not the public snapshot contract.
- Pagination either reaches `hasNextPage == false` or raises; it never silently truncates.
- The snapshot contains only public data and never contains a token.
- New code remains Python 3.9 compatible and dependency-free.
- Every behavior change is test-first and each task ends in a local commit.

## File Structure

- Create `ghwidgets_data.py`: normalization, complete paging orchestration, snapshot validation/read/write.
- Modify `ghwidgets_common.py`: make existing issue/PR paging accept an explicit no-limit mode without changing current callers.
- Modify `render.py`, `render-impact.py`, `render-responsiveness.py`: accept optional snapshot input while preserving default behavior.
- Create `test_data.py`: public contract, pagination, normalization, atomic snapshot tests.
- Modify renderer tests only for additive snapshot paths.
- Modify `README.md` and `CLAUDE.md`: public API, standalone compatibility, snapshot schema/version.

---

### Task 1: Versioned normalized snapshot contract

**Files:**
- Create: `ghwidgets_data.py`
- Create: `test_data.py`
- Modify: `install.sh`

**Interfaces:**
- Produces: `SCHEMA_VERSION: int = 1`.
- Produces: `normalise_issue(node: dict) -> dict`.
- Produces: `normalise_pull_request(node: dict) -> dict`.
- Produces: `build_snapshot(*, account: dict, insiders: set[str], repositories: list[dict], issues: list[dict], pull_requests: list[dict], generated_at: str | None = None) -> dict`.
- Produces: `load_snapshot(path: str | Path) -> dict`.
- Produces: `write_snapshot(path: str | Path, snapshot: dict) -> None`.

- [ ] **Step 1: Write failing contract tests**

```python
class Normalization(unittest.TestCase):
    def test_issue_keeps_current_final_state(self):
        out = data.normalise_issue({
            "id": "I_1", "number": 7, "url": "https://github/x/y/7",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-03T00:00:00Z",
            "closedAt": "2026-01-03T00:00:00Z",
            "state": "CLOSED", "stateReason": "NOT_PLANNED",
            "repository": {"id": "R_1", "nameWithOwner": "x/y",
                           "url": "https://github/x/y", "isPrivate": False,
                           "owner": {"login": "x"}},
        })
        self.assertEqual(out["state_reason"], "NOT_PLANNED")
        self.assertEqual(out["repository_id"], "R_1")

    def test_snapshot_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            path.write_text('{"schema_version":999}', encoding="utf-8")
            with self.assertRaises(data.SnapshotVersionError):
                data.load_snapshot(path)
```

- [ ] **Step 2: Run tests and verify the module is missing**

Run: `python3 -m unittest -v test_data.py`  
Expected: import failure for `ghwidgets_data`.

- [ ] **Step 3: Implement normalized records and validation**

Implement exact keys:

```python
SCHEMA_VERSION = 1

def normalise_issue(node):
    repo = node["repository"]
    return {
        "node_id": node["id"], "repository_id": repo["id"],
        "repository": repo["nameWithOwner"], "owner": repo["owner"]["login"],
        "repository_url": repo["url"], "is_private": bool(repo["isPrivate"]),
        "number": int(node["number"]), "url": node["url"],
        "created_at": node["createdAt"], "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"), "state": node["state"],
        "state_reason": node.get("stateReason"),
    }
```

The PR equivalent adds `merged_at` and `merged`. Validation requires top-level `schema_version`, `generated_at`, `account`, `insiders`, `repositories`, `issues`, and `pull_requests`; all collection fields must be lists.

- [ ] **Step 4: Implement atomic snapshot writing**

Use `ghwidgets_common.save_cache` only as the atomic/locked writer, not as a cache-schema validator. `write_snapshot` validates before writing. `generated_at` defaults to current UTC ISO seconds.

- [ ] **Step 5: Install the public module with the renderers**

Add `ghwidgets_data.py` to `install.sh`'s staged file set and installation smoke checks so partial deployments fail.

- [ ] **Step 6: Run contract and existing tests**

Run: `python3 -m unittest -v test_data.py test_common.py`  
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ghwidgets_data.py test_data.py install.sh
git commit -m "feat: add public GitHub snapshot contract"
```

---

### Task 2: Complete authored-item acquisition API

**Files:**
- Modify: `ghwidgets_common.py`
- Modify: `ghwidgets_data.py`
- Modify: `test_common.py`
- Modify: `test_data.py`

**Interfaces:**
- Consumes: normalization and snapshot interfaces from Task 1.
- Produces: `PaginationLimitError(RuntimeError)`.
- Produces: `fetch_authored_snapshot(token: str, login: str, *, gql_fn=None, max_pages: int | None = None) -> dict`.
- Preserves: `fetch_pull_requests(..., max_pages=50)` and `fetch_issues(..., max_pages=50)` for current callers.

- [ ] **Step 1: Write failing no-truncation tests**

```python
def test_no_limit_pages_until_has_next_page(self):
    pages = iter([page("I1", True, "c1"), page("I2", False, None)])
    nodes = common.fetch_issues("t", "me", max_pages=None,
                                gql_fn=lambda *_a, **_k: next(pages))
    self.assertEqual([n["id"] for n in nodes], ["I1", "I2"])

def test_explicit_limit_raises_instead_of_returning_partial(self):
    with self.assertRaises(common.PaginationLimitError):
        common.fetch_issues("t", "me", max_pages=1,
                            gql_fn=lambda *_a, **_k: page("I1", True, "c1"))
```

- [ ] **Step 2: Run focused tests and verify current truncation behavior fails them**

Run: `python3 -m unittest -v test_common.FetchIssues test_common.FetchPullRequests`  
Expected: failure because the current loops stop at `max_pages` without raising and do not accept `None`.

- [ ] **Step 3: Make pagination completion explicit**

Change both pagers to loop while `hasNextPage`. When `max_pages is not None` and another page exists after that count, raise `PaginationLimitError` with the connection name and last cursor. Preserve cached merged-PR merging and deduplication.

- [ ] **Step 4: Implement the high-level fetch**

`fetch_authored_snapshot` calls `fetch_identity`, `fetch_pull_requests(..., max_pages=None)`, and `fetch_issues(..., max_pages=None)`, rejects private repositories, normalizes nodes, deduplicates repositories by node ID, and calls `build_snapshot`. It must accept `gql_fn` for fixture tests and never read environment variables.

- [ ] **Step 5: Run acquisition and regression tests**

Run: `python3 -m unittest -v test_data.py test_common.py test_render.py test_impact.py test_responsiveness.py`  
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add ghwidgets_common.py ghwidgets_data.py test_common.py test_data.py
git commit -m "feat: expose complete authored-item acquisition"
```

---

### Task 3: Additive snapshot inputs and standalone compatibility

**Files:**
- Modify: `render.py`
- Modify: `render-impact.py`
- Modify: `render-responsiveness.py`
- Modify: `test_render.py`
- Modify: `test_impact.py`
- Modify: `test_responsiveness.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `load_snapshot(path) -> dict`.
- Produces: optional `--snapshot-file PATH` on all renderer CLIs.
- Produces: `--render-only`, which requires every data section used by that renderer and performs no GraphQL call.
- Preserves: invocation without either flag is byte-for-byte equivalent in behavior to the pre-change standalone path.

- [ ] **Step 1: Write failing CLI and no-network tests**

For each renderer, parse a snapshot path and patch its GraphQL function to raise if called. A render-only fixture containing the renderer's required sections must write the expected SVG. A missing section must exit non-zero with `snapshot missing required section: <name>`.

- [ ] **Step 2: Run focused renderer tests**

Run: `python3 -m unittest -v test_render.py test_impact.py test_responsiveness.py`  
Expected: failures for unknown arguments.

- [ ] **Step 3: Add snapshot argument parsing and data selection**

Use a single rule in every renderer:

```python
snapshot = data.load_snapshot(args.snapshot_file) if args.snapshot_file else None
if args.render_only and snapshot is None:
    parser.error("--render-only requires --snapshot-file")
```

Snapshot sections replace only matching fetched sections. Without `--render-only`, absent sections continue through the renderer's existing fetch/cache path. With `--render-only`, absent required sections fail before any network call.

- [ ] **Step 4: Verify old standalone behavior**

Run: `python3 -m unittest discover -v`  
Expected: all existing and new tests pass.

- [ ] **Step 5: Update operational documentation**

Document the public module, snapshot schema/version, pinned-consumer rule, render-only behavior, and the guarantee that normal commands remain standalone. Keep the two private cache files distinct.

- [ ] **Step 6: Run quality gates**

Run: `python3 -m unittest discover -v`  
Run: `python3 -m pyright`  
Run: `python3 -m pylint ghwidgets_common.py ghwidgets_data.py render.py render-impact.py render-responsiveness.py`  
Run: `scripts/secrecy-check.sh`  
Expected: all exit 0.

- [ ] **Step 7: Commit**

```bash
git add render.py render-impact.py render-responsiveness.py test_render.py test_impact.py test_responsiveness.py README.md CLAUDE.md
git commit -m "feat: render from versioned GitHub snapshots"
```

---

### Task 4: Final upstream verification and handoff pin

**Files:**
- Modify only if a verification defect is found.

**Interfaces:**
- Produces: a verified gh-widgets commit SHA for ghpulse's submodule pin.

- [ ] **Step 1: Run the complete suite and install smoke test**

Run: `python3 -m unittest discover -v`  
Run: `tmpdir=$(mktemp -d) && ./install.sh "$tmpdir"`  
Expected: all tests pass; install stages all five Python modules and every entry point starts coherently.

- [ ] **Step 2: Verify legacy help and new flags**

Run each renderer with `--help`; existing flags remain and `--snapshot-file`/`--render-only` appear. Run the existing commands against fixture inputs to prove no snapshot is required.

- [ ] **Step 3: Record the pin**

Run: `git rev-parse HEAD`  
Expected: clean worktree and one exact SHA supplied to the ghpulse plan.
