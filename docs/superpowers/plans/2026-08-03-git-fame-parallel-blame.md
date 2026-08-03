# git-fame parallel blame — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give git-fame a `-j/--jobs` option that runs its per-file `git blame` subprocesses concurrently, land it upstream, and consume it from a pinned fork build so `render-impact.py`'s blame pass is no longer bottlenecked by serial per-file blame subprocesses.

**Architecture:** A generator helper (`imap_bounded`) wraps `ThreadPoolExecutor` with a bounded window of outstanding futures and yields results **in input order**. `_get_auth_stats` uses it to fetch blame output concurrently while keeping all parsing and aggregation serial in the calling thread — so `stats_append` needs no lock and output is byte-identical to the serial path. `run()` resolves `jobs=0` to an auto value and divides the budget when several gitdirs are processed concurrently.

**Tech Stack:** Python 3.8+, `concurrent.futures.ThreadPoolExecutor`, `argopt` (builds the CLI from the module docstring), `tqdm`, pytest (upstream), stdlib `unittest` (gh-widgets).

## Global Constraints

- Upstream `git-fame` supports **Python >= 3.8** (`pyproject.toml: requires-python`). No 3.9+ syntax.
- Upstream pytest runs with `-W=error` — any warning fails the suite.
- Upstream pytest runs with `--cov-fail-under=85`.
- Upstream line length is **120** (`flake8 max_line_length`, `yapf column_limit`, `isort line_length`).
- `argopt` builds the parser from `_gitfame.py`'s module `__doc__`. The docstring **is** the CLI definition — an option not written there does not exist.
- Option help lines need **two spaces** between the option and its description, and `[default: 0:int]` for an int-cast default.
- **gh-widgets is stdlib-only** (`CONTRIBUTING.md`): `render-impact.py` and its tests use no third-party imports. The test runner is stdlib `unittest`, not pytest.
- Every commit carries `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Output must remain **byte-identical** between `-j1` and `-jN`. This is the invariant the whole design rests on.

---

### Task 1: Bounded ordered concurrent map

**Files:**
- Create fork + clone: `Nitjsefnie-OSC/git-fame`, branch `parallel-blame`
- Modify: `gitfame/_utils.py` (append after `merge_stats`)
- Test: `tests/test_utils.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `imap_bounded(func, items, jobs)` — a generator yielding `func(item)` for each item **in input order**, running at most `jobs` calls concurrently and holding at most `2 * jobs` results in memory. `jobs < 2` runs serially with no executor created.

- [ ] **Step 1: Fork and clone**

```bash
gh repo fork casperdcl/git-fame --org Nitjsefnie-OSC --clone=false --remote=false
cd /root && git clone https://github.com/Nitjsefnie-OSC/git-fame.git
cd /root/git-fame
git remote add upstream https://github.com/casperdcl/git-fame.git
git fetch upstream
git checkout -b parallel-blame upstream/main
pip install -e '.[dev]'
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_utils.py`:

```python
def test_imap_bounded():
    """Test bounded concurrent map: order preserved, concurrency capped"""
    from threading import Lock
    from time import sleep

    from gitfame._utils import imap_bounded

    live, peak, lock = [0], [0], Lock()

    def work(i):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        sleep(0.005)
        with lock:
            live[0] -= 1
        return i * 2

    assert list(imap_bounded(work, range(20), 4)) == [i * 2 for i in range(20)]
    assert peak[0] > 1, "should have run concurrently"
    assert peak[0] <= 4, "should not exceed the job cap"

    # serial fallback: no executor, same results
    assert list(imap_bounded(lambda i: i * 2, range(5), 1)) == [0, 2, 4, 6, 8]
    assert list(imap_bounded(lambda i: i * 2, [], 4)) == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_utils.py::test_imap_bounded -v -p no:cov -p no:xdist`
Expected: FAIL with `ImportError: cannot import name 'imap_bounded'`

- [ ] **Step 4: Write minimal implementation**

Append to `gitfame/_utils.py`:

```python
def imap_bounded(func, items, jobs):
    """
    Like `map(func, items)` but runs up to `jobs` calls concurrently,
    yielding results in input order.

    Only `2 * jobs` results are held at once, so memory stays bounded
    regardless of how many items there are (`git blame --line-porcelain`
    output is large compared to the source file, so an unbounded map would
    buffer gigabytes on a large repository).
    """
    if jobs < 2:
        for i in items:
            yield func(i)
        return

    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    from itertools import islice

    it = iter(items)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = deque(pool.submit(func, i) for i in islice(it, 2 * jobs))
        while pending:
            res = pending.popleft().result()
            for i in islice(it, 1):
                pending.append(pool.submit(func, i))
            yield res
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_utils.py::test_imap_bounded -v -p no:cov -p no:xdist`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gitfame/_utils.py tests/test_utils.py
git commit -m "add imap_bounded: ordered concurrent map with a bounded window

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire `--jobs` through the CLI and the blame loop

**Files:**
- Modify: `gitfame/_gitfame.py` (module docstring; `_get_auth_stats`; `run`)
- Test: `tests/test_gitfame.py`

**Interfaces:**
- Consumes: `imap_bounded(func, items, jobs)` from Task 1.
- Produces: `_get_auth_stats(..., jobs=1)` — new trailing keyword argument, default `1` so any direct caller keeps the current serial behaviour. CLI attribute `args.jobs` (int, default `0` meaning auto).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gitfame.py`:

```python
def test_jobs_determinism(capsys):
    """--jobs must not change output"""
    root = path.dirname(path.dirname(__file__))
    main(['-s', '--format=json', '-j', '1', root])
    serial = capsys.readouterr().out
    main(['-s', '--format=json', '-j', '4', root])
    parallel = capsys.readouterr().out
    assert serial == parallel
    assert loads(serial)['total']['loc'] > 0
```

And add two entries to the existing `test_options` parametrize list (`tests/test_gitfame.py`), inside the list literal after `['-w']`:

```python
     ['-j', '1'], ['-j', '4'],
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gitfame.py::test_jobs_determinism -v -p no:cov -p no:xdist`
Expected: FAIL — `argparse` exits with `unrecognized arguments: -j 1`

- [ ] **Step 3: Declare the option in the module docstring**

In `gitfame/_gitfame.py`, insert this line into the `Options:` block immediately after the `-s, --silent-progress` entry. Two spaces before the description; `argopt` reads this docstring to build the parser.

```
  -j, --jobs=<n>  Number of concurrent `git blame` processes per repository
                  [default: 0:int]. 0: automatic (based on CPU count);
                  1: serial (the pre-3.2 behaviour).
```

- [ ] **Step 4: Import the helper**

In `gitfame/_gitfame.py`, add `imap_bounded` to the existing `._utils` import, keeping alphabetical order:

```python
from ._utils import (TERM_WIDTH, Str, TqdmStream, check_output, fext, imap_bounded, int_cast_or_len, merge_stats,
                     print_unicode, tqdm)
```

- [ ] **Step 5: Add the `jobs` parameter to `_get_auth_stats`**

Change the signature's final parameter list so it ends with `until=None, jobs=1`:

```python
def _get_auth_stats(gitdir, branch="HEAD", since=None, include_files=None, exclude_files=None, silent_progress=False,
                    ignore_whitespace=False, M=False, C=False, warn_binary=False, bytype=False, show=None,
                    prefix_gitdir=False, churn=None, ignore_rev="", ignore_revs_file=None, until=None, jobs=1):
```

- [ ] **Step 6: Replace the serial blame loop**

Replace the whole `if churn & CHURN_SLOC:` block (the `for fname in tqdm(file_list, ...)` loop) with:

```python
    if churn & CHURN_SLOC:

        def blame_file(fname):
            """Blame one file. Returns `(fname, output_or_exception)` so that
            failures stay in input order and are reported by the caller."""
            try:
                return fname, check_output(base_cmd + [branch, fname], stderr=subprocess.STDOUT)
            except Exception as err:
                return fname, err

        # only the subprocess runs concurrently; parsing and `stats_append`
        # stay in this thread, so output is identical to `--jobs=1`
        for fname, blame_out in tqdm(imap_bounded(blame_file, file_list, jobs), total=len(file_list),
                                     desc=gitdir if prefix_gitdir else "Processing", disable=silent_progress,
                                     unit="file"):
            # `fname` is relative to `gitdir`, so only prefix the reported name
            display_fname = path.join(gitdir, fname) if prefix_gitdir else fname
            if isinstance(blame_out, Exception):
                getattr(log, "warn" if warn_binary else "debug")(display_fname + ':' + str(blame_out))
                continue
            log.log(logging.NOTSET, blame_out)

            if since:
                # Strip boundary messages,
                # preventing user with nearest commit to boundary owning the LOC
                blame_out = RE_BLAME_BOUNDS.sub('', blame_out)

            if until:
                # Strip boundary messages,
                # preventing user with nearest commit to boundary owning the LOC
                blame_out = RE_BLAME_BOUNDS.sub('', blame_out)

            for loc, name, email, tstamp in RE_AUTHS_BLAME.findall(blame_out): # for each chunk
                loc = int(loc)
                auth = f'{name} <{email}>'
                stats_append(display_fname, auth, loc, tstamp)
```

- [ ] **Step 7: Resolve and divide the job budget in `run`**

In `run()`, immediately **before** the `statter = partial(...)` assignment, insert:

```python
    jobs = args.jobs or min(32, (os.cpu_count() or 1) + 4)
    if len(gitdirs) > 1:
        # repos are already processed concurrently below; divide the budget so
        # the product stays bounded rather than multiplying
        jobs = max(1, jobs // len(gitdirs))
```

Then add `jobs=jobs` as the final keyword of the `partial(...)` call:

```python
    statter = partial(_get_auth_stats, branch=args.branch, since=args.since, until=args.until,
                      include_files=include_files, exclude_files=exclude_files, silent_progress=args.silent_progress,
                      ignore_whitespace=args.ignore_whitespace, M=args.M, C=args.C, warn_binary=args.warn_binary,
                      bytype=args.bytype, show=args.show, prefix_gitdir=len(gitdirs) > 1, churn=churn,
                      ignore_rev=args.ignore_rev, ignore_revs_file=args.ignore_revs_file, jobs=jobs)
```

- [ ] **Step 8: Run the new tests**

Run: `pytest tests/test_gitfame.py::test_jobs_determinism tests/test_gitfame.py::test_options -v -p no:cov -p no:xdist`
Expected: PASS (all `test_options` params, including the two new ones)

- [ ] **Step 9: Run the whole suite as CI does**

Run: `pytest`
Expected: PASS, coverage >= 85 %, no warnings (the suite runs `-W=error`)

- [ ] **Step 10: Verify byte-identical output on real repositories**

```bash
for r in /root/gh-widgets /root/git-fame; do
  a=$(git -C "$r" fame -e -w --format json -j1)
  b=$(git -C "$r" fame -e -w --format json -j8)
  [ "$a" = "$b" ] && echo "IDENTICAL $r" || { echo "DIFFERS $r"; exit 1; }
done
```
Expected: `IDENTICAL` for every repo.

- [ ] **Step 11: Lint**

Run: `flake8 gitfame tests && isort --check gitfame tests`
Expected: clean

- [ ] **Step 12: Commit**

```bash
git add gitfame/_gitfame.py tests/test_gitfame.py
git commit -m "run per-file git blame concurrently (--jobs)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Shell completion and man page

**Files:**
- Modify: `git-fame_completion.bash`
- Modify: `gitfame/git-fame.1` (generated)

**Interfaces:**
- Consumes: the `--jobs` option declared in Task 2's docstring.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Read the completion script**

Run: `cat git-fame_completion.bash`
Locate the space-separated list of long options and the list of short options.

- [ ] **Step 2: Add the option to the completion word list**

Add `--jobs` to the long-option list and `-j` to the short-option list, preserving the existing ordering convention of that file.

- [ ] **Step 3: Verify completion still sources cleanly**

Run: `bash -n git-fame_completion.bash && echo OK`
Expected: `OK`

- [ ] **Step 4: Regenerate the man page**

Run: `make gitfame/git-fame.1`
Expected: the target rebuilds. If `pandoc` is unavailable, skip this step and say so explicitly in the PR body rather than committing a hand-edited man page.

- [ ] **Step 5: Confirm the new option is documented**

Run: `git fame --help | grep -A2 'jobs'`
Expected: the `-j, --jobs=<n>` entry appears with its description.

- [ ] **Step 6: Commit**

```bash
git add git-fame_completion.bash gitfame/git-fame.1
git commit -m "document --jobs in completion and man page

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Pin the patched build and guard it from gh-widgets

**Files:**
- Create: `/root/gh-widgets/test_impact.py`
- Modify: `/root/gh-widgets/render-impact.py`
- Modify: `/root/gh-widgets/install.sh`
- Modify: `/root/gh-widgets/CLAUDE.md`

**Interfaces:**
- Consumes: the installed `git fame` binary supporting `-j`.
- Produces: nothing consumed by later tasks.

**Why this task has a runtime check and not just an install-time one** — copied
from a sibling repo that learned it the hard way (`Consultest-CZ/kvalita`
`2bca15f` → `e29fc78` → `f30eed6`). That repo patched a dependency, then needed
two follow-up commits: one because the patched wheel reported the *same version*
as stock so a routine `pip -U` silently reverted it, and one because the guard
**logged nothing on success**, making a healthy boot indistinguishable from the
guard never having run.

- The version half is already free here: git-fame uses `setuptools_scm`, so the
  fork build reports `3.1.4.dev1+g<sha>` against stock's `3.1.2`/`3.1.3`. Drift
  is visible in `pip freeze` by construction — do NOT add a wheel-retagging step.
- The self-proving half is NOT free, and `install.sh` alone does not provide it:
  `render-impact.service` runs unattended on a timer, long after any install.
  The check must run **per render** and must be **noisy on success**, so that the
  absence of its line in the journal is itself the alarm.

- [ ] **Step 1: Install the patched build**

```bash
cd /root/git-fame && git rev-parse HEAD
pip install --force-reinstall "git-fame @ git+https://github.com/Nitjsefnie-OSC/git-fame@$(git rev-parse HEAD)"
git-fame --version
git-fame --help | grep -c -- '--jobs'
```
Expected: the help grep returns `1`. Use `git-fame`, not `git fame`: git rewrites `git <cmd> --help` into `man git-<cmd>`, so the `git fame` form reports "No manual entry for git-fame" and greps as 0 even when the patched build is installed.

- [ ] **Step 2: Write the failing test**

Create `/root/gh-widgets/test_impact.py`. Stdlib only, `unittest`, matching `test_common.py`:

```python
#!/usr/bin/env python3
"""Tests for render-impact.py's external dependency, git-fame.

    python3 -m unittest discover -v

render-impact.py shells out to `git fame` for its blame pass. Stock git-fame
blames one file per subprocess serially, so the blame pass is dominated by
fork/exec overhead. We run a patched build (Nitjsefnie-OSC/git-fame) that adds
`--jobs`.

These tests pin the CAPABILITY, not the timing — a timing assertion would be
flaky on a shared box, and a slow run is not a wrong run. The invariant that
matters is that parallelism does not change the numbers.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fame(cwd, *extra):
    # Deliberately the `git fame` form here, not `git-fame`: this is the exact
    # invocation render-impact.py's blame_repo uses, so the parity test must
    # exercise that path. Only `--help` is special-cased by git's dispatcher,
    # which is why the capability probe below uses the hyphenated binary.
    out = subprocess.run(["git", "fame", "-s", "-e", "-w", "--format", "json", *extra],
                         cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout


class TestGitFameParallel(unittest.TestCase):
    """The blame pass depends on a git-fame that supports --jobs."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git-fame") is None:
            raise unittest.SkipTest("git-fame is not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-fame-"))
        git("init", "-q", "-b", "main", ".", cwd=cls.tmp)
        git("config", "user.email", "t@example.com", cwd=cls.tmp)
        git("config", "user.name", "T", cwd=cls.tmp)
        for i in range(12):
            (cls.tmp / f"f{i}.txt").write_text(f"line one {i}\nline two {i}\n")
        git("add", "-A", cwd=cls.tmp)
        git("commit", "-qm", "initial", cwd=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_jobs_option_supported(self):
        """The pinned build must accept --jobs.

        Fails on stock git-fame from PyPI, which is the point: without the
        patch the blame pass is serial.
        """
        # `git-fame`, NOT `git fame` — git rewrites `git <cmd> --help` into
        # `man git-<cmd>`, which reports no manual entry and would make this
        # assertion fail against a correctly patched build.
        help_text = subprocess.run(["git-fame", "--help"],
                                   capture_output=True, text=True, check=True).stdout
        self.assertIn("--jobs", help_text,
                      "installed git-fame lacks --jobs; expected the pinned "
                      "Nitjsefnie-OSC build (see CLAUDE.md)")

    def test_parallel_matches_serial(self):
        """Parallelism must not change the counts render-impact reads."""
        serial = json.loads(fame(self.tmp, "-j", "1"))
        parallel = json.loads(fame(self.tmp, "-j", "4"))
        self.assertEqual(serial, parallel)
        self.assertEqual(serial["total"]["loc"], 24)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Prove the test fails on stock git-fame**

```bash
pip install --force-reinstall 'git-fame==3.1.2'
cd /root/gh-widgets && python3 -m unittest test_impact -v
```
Expected: FAIL on `test_jobs_option_supported`. **This is the doctrine's "regression test that fails on the stock version" — record the output.**

- [ ] **Step 4: Reinstall the pinned build and confirm it passes**

```bash
cd /root/git-fame
pip install --force-reinstall "git-fame @ git+https://github.com/Nitjsefnie-OSC/git-fame@$(git rev-parse HEAD)"
cd /root/gh-widgets && python3 -m unittest test_impact -v
```
Expected: PASS

- [ ] **Step 5: Add the self-proving runtime check to render-impact.py**

Add this function to `/root/gh-widgets/render-impact.py`, next to the other module-level helpers, and call it as the first statement inside `main()` **after** `parse_args()` (so `--help` still exits without paying for it — `install.sh` uses `--help` to verify the renderer starts):

```python
def check_git_fame():
    """Verify the installed git-fame is the patched build, and SAY SO.

    This renderer runs unattended on a timer, so a check that is silent on
    success is indistinguishable from a check that never ran — the absence of
    the line has to be the alarm, which only works when success is noisy.
    Stock git-fame is degraded (serial blame), not wrong, so this warns and
    continues rather than aborting.
    """
    # NOTE: `git-fame`, not `git fame`. Git's dispatcher rewrites
    # `git <cmd> --help` into `man git-<cmd>`, so `git fame --help` prints
    # "No manual entry for git-fame" and greps as if --jobs were absent —
    # even when the patched build IS installed. Invoke the binary directly.
    try:
        r = subprocess.run(["git-fame", "--help"], capture_output=True,
                           text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"git-fame: CHECK FAILED ({e}) — blame pass may not work", flush=True)
        return False
    if "--jobs" not in r.stdout:
        print("git-fame: WARNING — no --jobs, so this is STOCK git-fame: the "
              "blame pass is serial. See CLAUDE.md for the pin.", flush=True)
        return False
    v = subprocess.run(["git-fame", "--version"], capture_output=True,
                       text=True, timeout=60, check=False).stdout.strip()
    print(f"git-fame: {v} with --jobs (patched build)", flush=True)
    return True
```

Do NOT make it fatal and do NOT pass `-j` at the `blame_repo` call site. The patched build parallelises by default, so `blame_repo`'s command stays exactly as it is; passing `-j` explicitly would turn a stock install from "slow" into "every repo errors", which is the cache-poisoning failure this whole guard exists to avoid.

- [ ] **Step 6: Cover the runtime check in test_impact.py**

Append to `/root/gh-widgets/test_impact.py`. Load the renderer the way `test_common.py` loads its module — `render-impact.py` is not an importable name:

```python
class TestGitFameRuntimeCheck(unittest.TestCase):
    """The runtime check must be self-proving: loud on success, not silent."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        import io
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location(
            "render_impact", Path(__file__).with_name("render-impact.py"))
        if spec is None or spec.loader is None:
            raise unittest.SkipTest("cannot load render-impact.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.io, cls.redirect = io, redirect_stdout

    def test_check_is_noisy_on_success(self):
        """Success must print — a silent success cannot be distinguished from
        the check never having run, which is the whole point of it."""
        buf = self.io.StringIO()
        with self.redirect(buf):
            ok = self.mod.check_git_fame()
        out = buf.getvalue()
        self.assertTrue(ok, f"check reported stock git-fame: {out!r}")
        self.assertIn("git-fame:", out)
        self.assertIn("--jobs", out)
        self.assertTrue(out.strip(), "check was silent on success")
```

- [ ] **Step 7: Add a capability warning to install.sh**

In `/root/gh-widgets/install.sh`, after the existing `verified: all renderers start...` echo, append:

```sh
# render-impact.py's blame pass needs the parallel-blame git-fame build; stock
# git-fame blames one file per subprocess serially. Degraded, not broken — so
# warn rather than fail.
if command -v git-fame >/dev/null 2>&1; then
    # git-fame, NOT `git fame`: `git <cmd> --help` is rewritten by git into
    # `man git-<cmd>`, which reports no manual entry and hides the option.
    if ! git-fame --help 2>/dev/null | grep -q -- '--jobs'; then
        echo "install.sh: WARNING - installed git-fame has no --jobs; render-impact" >&2
        echo "                     blame pass is serial. See CLAUDE.md for the pin." >&2
    fi
else
    echo "install.sh: WARNING - git-fame is not installed; render-impact cannot blame" >&2
fi
```

- [ ] **Step 8: Verify install.sh still runs clean**

```bash
sh -n /root/gh-widgets/install.sh && /root/gh-widgets/install.sh /tmp/ghw-install-check
diff /tmp/ghw-install-check/render-impact.py /root/gh-widgets/render-impact.py && echo IDENTICAL
```
Expected: no syntax error, install succeeds, `IDENTICAL`.

- [ ] **Step 9: Record the pin in CLAUDE.md**

In `/root/gh-widgets/CLAUDE.md`, directly after the paragraph about `GH_EXTRA_EMAILS`, add:

```markdown
> **git-fame is pinned to a fork, not PyPI.** `render-impact.py`'s blame pass
> runs `git fame` per repo; stock git-fame spawns one serial `git blame`
> subprocess per file. We install
> `Nitjsefnie-OSC/git-fame` (branch `parallel-blame`), which adds `--jobs`:
>
> ```
> pip install --force-reinstall "git-fame @ git+https://github.com/Nitjsefnie-OSC/git-fame@<sha>"
> ```
>
> **How you can tell which build is installed.** git-fame derives its version
> from git tags via `setuptools_scm`, so the fork build reports
> `3.1.4.dev1+g<sha>` while PyPI stock reports `3.1.2`/`3.1.3` — a silent
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
> Once the upstream PR merges, re-pin to the PyPI release that contains it and
> delete this note.
```

- [ ] **Step 10: Run the full gh-widgets suite**

Run: `cd /root/gh-widgets && python3 -m unittest discover -v`
Expected: PASS, including the new `test_impact` cases.

- [ ] **Step 11: Commit and push**

```bash
cd /root/gh-widgets
git add test_impact.py render-impact.py install.sh CLAUDE.md
git commit -m "pin git-fame to the parallel-blame fork, guard it with a test

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push
```

---

### Task 5: Upstream the patch

**Files:**
- No source changes. Produces one GitHub issue and one pull request on `casperdcl/git-fame`.

**Interfaces:**
- Consumes: the `parallel-blame` branch from Tasks 1–3.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Load the contribution contracts**

Invoke the `contribution-contracts` skill and read both contracts in full **before writing either body**. `casperdcl/git-fame` has no `CONTRIBUTING.md` and no `.github/ISSUE_TEMPLATE/` (verified: the repo root has only `.github/CODEOWNERS`, `FUNDING.yml`, `workflows/`), so the skill's contracts apply rather than a repo template.

- [ ] **Step 2: Re-check for duplicates immediately before filing**

```bash
gh issue list --repo casperdcl/git-fame --state all --search "jobs OR parallel OR concurrent" --limit 20
gh pr list --repo casperdcl/git-fame --state all --limit 20
```
Expected: still no prior art.

- [ ] **Step 3: File the issue**

Observed behaviour only, per the issue-filing contract: git-fame runs one `git blame`
subprocess per file serially, and the fixed per-call cost of fork/exec/repo-open dominates
runtime even for tiny files. Do not propose the fix in the issue body — the PR does that.

```bash
gh issue create --repo casperdcl/git-fame --title "..." --body "..."
```

- [ ] **Step 4: Push the branch to the fork**

```bash
cd /root/git-fame && git push -u origin parallel-blame
```

- [ ] **Step 5: Open the pull request**

Body follows `pr-report-contract.md` exactly, links the issue from Step 3, and states the
performance improvement in plain terms. Lead with the invariant: output is byte-identical
between `-j1` and `-jN`, proven by `test_jobs_determinism` and by the real-repo check in
Task 2 Step 10.

```bash
gh pr create --repo casperdcl/git-fame --head Nitjsefnie-OSC:parallel-blame --title "..." --body "..."
```

- [ ] **Step 6: Record the URLs**

Add the issue and PR URLs to the `CLAUDE.md` note from Task 4 Step 7 so the pin has a live pointer to its upstream status, then commit and push.

---

## Self-Review

**Amendment (2026-08-03, after Task 1):** Task 4 gained Steps 5 and 6 — a
`check_git_fame()` runtime guard in `render-impact.py` and a test that it is
noisy on success — and Steps 5–9 renumbered to 7–11. Adopted from
`Consultest-CZ/kvalita` (`2bca15f` → `e29fc78` → `f30eed6`), where an
install-time-only guard and a silent-on-success guard each needed their own
follow-up commit. The version-collision half of that pattern is *not* copied:
git-fame's `setuptools_scm` version already distinguishes the fork build.

**Spec coverage:** every row of the spec's change inventory maps to a task — `_gitfame.py` docstring/`_get_auth_stats`/`run` → Task 2; `tests/test_gitfame.py` → Task 2; `git-fame_completion.bash` + `git-fame.1` → Task 3; `test_impact.py` + `CLAUDE.md`/`install.sh` → Task 4; issue + PR → Task 5. The spec's `imap_bounded` requirement (bounded window) is Task 1. The spec verification items appear as steps: byte-identical output (T2 S10), upstream suite green (T2 S9), fails-on-stock (T4 S3), deployed copies match (T4 S8). The "end-to-end unchanged" verification is covered by T4 S8 plus the byte-identical check; a full `render-impact.py` run is not scripted here because it mutates the production cache — run it manually against a copied `CACHE_FILE` if desired.

**Placeholder scan:** the only deliberately unwritten content is the issue and PR **body text** in Task 5, which cannot be pre-written — the contracts that govern their structure are loaded in Task 5 Step 1, and the required content is enumerated in Steps 3 and 5.

**Type consistency:** `imap_bounded(func, items, jobs)` is defined in Task 1 and called with exactly that arity in Task 2 Step 6. `_get_auth_stats(..., jobs=1)` is defined in Task 2 Step 5 and passed `jobs=jobs` in Step 7. `args.jobs` is created by the docstring in Step 3 and read in Step 7.
