"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import re
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RATCHET_WORKFLOW = WORKFLOWS / "coverage-ratchet.yml"
CODEQL_USE = re.compile(r"uses:\s*github/codeql-action/([\w-]+)@(\S+)")
FORK_PIN = re.compile(r'FORK_PIN="git\+https://github\.com/Nitjsefnie-OSC/'
                      r'git-fame@([0-9a-f]{40})"')
DOCUMENTED_PIN = re.compile(r"git\+https://github\.com/Nitjsefnie-OSC/"
                            r"git-fame@([0-9a-f]{40})")
PINNED_USE = re.compile(r"^\s*-?\s*uses:\s*[\w.-]+/[\w.-]+@[0-9a-f]{40}"
                        r"\s+#\s+v[0-9][0-9A-Za-z.\-]*$")
USES_LINE = re.compile(r"^\s*-?\s*uses:")

# What the job holding the repository's only write token may never do. Not a
# list of the tools that were planted in it — a deny-list of three is defeated
# by a fourth interpreter or a renamed script, and the point of the case using
# this is that it is not defeated that way. Read it as "this job must not
# EXECUTE anything and must not reach into a tree", which is the property the
# workflow's own comment claims, rather than as an inventory of today.
WRITE_JOB_FORBIDDEN = (
    "python",      # any interpreter, in any spelling: python3, python3.13
    "bash", "sh ", "zsh", "node", "perl", "ruby", "env ", "eval", "exec",
    "curl", "wget", "ssh",
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


class TestCodeqlPins(unittest.TestCase):
    """Every github/codeql-action step must run the same release."""

    def test_codeql_action_steps_share_one_ref(self):
        # init writes a config that analyze refuses to load from a different
        # version, so a half-bump fails every CodeQL run. Dependabot treats
        # the two as separate dependencies; dependabot.yml groups them, and
        # this catches a hand edit that splits them again.
        uses = [(path.name, step, ref)
                for path in sorted(WORKFLOWS.glob("*.yml"))
                for step, ref in CODEQL_USE.findall(path.read_text())]
        self.assertTrue(uses, "no github/codeql-action step found")
        self.assertEqual(len({ref for _, _, ref in uses}), 1, uses)


class TestGitFameForkPin(unittest.TestCase):
    """A measurement's `fork` arm must be the build production pins."""

    def test_fork_arm_matches_the_documented_pin(self):
        # The arm is the baseline every upstream comparison is read against.
        # Left on an older fork build, a comparison measures a build nobody
        # runs, and a "switch or not" answer rests on the wrong number.
        documented = set(DOCUMENTED_PIN.findall(
            (REPO_ROOT / "CLAUDE.md").read_text()))
        self.assertEqual(len(documented), 1, documented)
        arms = {(path.name, sha)
                for path in sorted(WORKFLOWS.glob("*.yml"))
                for sha in FORK_PIN.findall(path.read_text())}
        self.assertTrue(arms, "no FORK_PIN found in any workflow")
        self.assertEqual({sha for _, sha in arms}, documented, arms)


class TestSpeedWorkflowRendererGate(unittest.TestCase):
    """Exercise the workflow's unit and renderer comparison argument shape."""

    workflow = WORKFLOWS / "speed.yml"

    @staticmethod
    def _run_block(step_name):
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        step_line = f"      - name: {step_name}"
        start = lines.index(step_line)
        run_line = next(index for index in range(start + 1, len(lines))
                        if lines[index].startswith("        run: |"))
        body = []
        for line in lines[run_line + 1:]:
            if line.startswith("      - "):
                break
            if line.startswith("          "):
                body.append(line[10:])
            elif not line.strip():
                body.append("")
            else:
                break
        return "\n".join(body) + "\n"

    @staticmethod
    def _write_report(path, classname, cases):
        """One <testsuite> carrying `cases`, a sequence of (name, duration)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        suite = ET.Element("testsuite", {
            "name": "fixture", "tests": str(len(cases))})
        for name, duration in cases:
            ET.SubElement(suite, "testcase", {
                "classname": classname,
                "name": name,
                "time": str(duration),
            })
        ET.ElementTree(suite).write(path, encoding="utf-8",
                                    xml_declaration=True)

    def _write_comparison_reports(self, root, unit_head, renderer_head,
                                  omit_renderer=None):
        """Two rounds of both report families, as speed.yml produces them.

        `omit_renderer` drops one workload from the HEAD renderer report
        only — the shape issue #36 is about, where the gate keeps measuring
        one fewer shipped renderer and still reports a pass.
        """
        reports = root / "reports"
        workloads = [("bench.render", renderer_head),
                     ("bench.render-impact", 0.20),
                     ("bench.render-responsiveness", 0.05)]
        for round_number in (1, 2):
            self._write_report(reports / f"base-{round_number}.xml", "unit",
                               [("test_unit", 0.10)])
            self._write_report(reports / f"head-{round_number}.xml", "unit",
                               [("test_unit", unit_head)])
            self._write_report(reports / f"bench-base-{round_number}.xml",
                               "e2e", workloads)
            self._write_report(
                reports / f"bench-head-{round_number}.xml", "e2e",
                [case for case in workloads
                 if case[0] != omit_renderer])

    @staticmethod
    def _install_harness(head, harness_edit=None):
        """Copy the real harness into the fake head checkout, optionally edited.

        The Compare block asks the harness for its workload list, so the fake
        checkout needs it — otherwise the block fails for the wrong reason and
        proves nothing. `harness_edit` receives the source and returns the
        modified source, which is how the controls below construct the states
        a retirement and a broken listing actually produce.
        """
        harness = head / "scripts" / "bench" / "e2e_bench.py"
        harness.parent.mkdir(parents=True, exist_ok=True)
        source = (REPO_ROOT / "scripts" / "bench" /
                  "e2e_bench.py").read_text(encoding="utf-8")
        if harness_edit is not None:
            source = harness_edit(source)
        harness.write_text(source, encoding="utf-8")
        return harness

    def _execute_compare(self, root, allowed_removals="",
                         harness_edit=None):
        head = root / "head"
        comparator = head / "scripts" / "ci" / "compare_durations.py"
        comparator.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / "scripts" / "ci" /
                        "compare_durations.py", comparator)
        self._install_harness(head, harness_edit)
        summary = root / "summary.md"
        env = {
            **os.environ,
            "BASE_TAG": "fixture-baseline",
            "GITHUB_STEP_SUMMARY": str(summary),
            "MAX_REGRESSION": "0.30",
            "ALLOWED_WORKLOAD_REMOVALS": allowed_removals,
        }
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c",
             TestSpeedWorkflowRendererGate._run_block("Compare")],
            cwd=root, env=env, capture_output=True, text=True,
            check=False)
        return completed, summary

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_compare_runs_once_for_each_report_family(self):
        with tempfile.TemporaryDirectory(prefix="ghw-speed-compare-shape-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.11, 0.21)
            completed, summary = self._execute_compare(root)

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("fixture-baseline`", summary_text)
            self.assertIn("fixture-baseline renderer workloads`",
                          summary_text)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_unit_regression_still_runs_renderer_comparison(self):
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-failure-shape-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.20, 0.20)
            completed, summary = self._execute_compare(root)

            self.assertEqual(completed.returncode, 1)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("fixture-baseline`", summary_text)
            self.assertIn("fixture-baseline renderer workloads`",
                          summary_text)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_missing_renderer_workload_fails_the_compare_step(self):
        """Issue #36: a shipped renderer that stops being measured is red."""
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-missing-renderer-") as td:
            root = Path(td)
            self._write_comparison_reports(
                root, 0.11, 0.21, omit_renderer="bench.render-impact")
            completed, summary = self._execute_compare(root)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("e2e::bench.render-impact", summary_text)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_declared_workload_removal_passes_the_compare_step(self):
        """The exemption surface, for a workload the harness still lists.

        This is the only state ALLOWED_WORKLOAD_REMOVALS can actually reach:
        the harness keeps measuring the workload, so the report contains it,
        and the gate is told not to require it. A genuine retirement — a
        renderer actually deleted — does NOT come through here; see
        test_retiring_a_workload_from_workloads_needs_no_exemption.
        """
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-allowed-removal-") as td:
            root = Path(td)
            self._write_comparison_reports(
                root, 0.11, 0.21, omit_renderer="bench.render-impact")
            completed, summary = self._execute_compare(
                root, allowed_removals="e2e::bench.render-impact")

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            self.assertIn("renderer workloads`",
                          summary.read_text(encoding="utf-8"))

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_retiring_a_workload_from_workloads_needs_no_exemption(self):
        """The real retirement path, which the variable's old comment got wrong.

        A renderer retired properly is dropped from WORKLOADS, so the harness
        measures neither side: it never appears in the baseline report either,
        the closed-set check has nothing unexplained to complain about, and
        ALLOWED_WORKLOAD_REMOVALS stays empty. That is what this asserts, so
        the exemption surface is not mistaken for the retirement procedure.
        """
        def retire_impact(source):
            return source.replace(
                '    ("render-impact", "render-impact.py", ("impact.svg",),\n'
                '     "impact-cache.json"),\n', "")

        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-retired-workload-") as td:
            root = Path(td)
            reports = root / "reports"
            remaining = [("bench.render", 0.21),
                         ("bench.render-responsiveness", 0.05)]
            for round_number in (1, 2):
                self._write_report(reports / f"base-{round_number}.xml",
                                   "unit", [("test_unit", 0.10)])
                self._write_report(reports / f"head-{round_number}.xml",
                                   "unit", [("test_unit", 0.11)])
                self._write_report(reports / f"bench-base-{round_number}.xml",
                                   "e2e", remaining)
                self._write_report(reports / f"bench-head-{round_number}.xml",
                                   "e2e", remaining)
            completed, summary = self._execute_compare(
                root, allowed_removals="", harness_edit=retire_impact)

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            self.assertIn("renderer workloads`",
                          summary.read_text(encoding="utf-8"))

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_workload_listing_that_fails_stops_the_compare_step(self):
        """A producer that dies must not silently empty the required list.

        bash -e cannot see a process substitution's exit status, so a broken
        listing used to leave no --require-test at all — which is issue 36's
        own false green, reached through a different door.
        """
        def break_flag(source):
            return source.replace('if "--list-workloads" in argv:',
                                  'if "--list-workloads-v2" in argv:')

        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-broken-listing-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.11, 0.21)
            completed, _ = self._execute_compare(root, harness_edit=break_flag)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            self.assertIn("workload listing", completed.stderr)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_empty_workload_listing_stops_the_compare_step(self):
        """A producer that prints nothing is as disabling as one that dies."""
        def print_nothing(source):
            return source.replace("        print(workload_node_id(workload[0]))",
                                  "        pass  # deliberately empty listing")

        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-empty-listing-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.11, 0.21)
            completed, _ = self._execute_compare(root, harness_edit=print_nothing)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            self.assertIn("workload listing is empty", completed.stderr)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_exempting_every_workload_stops_the_compare_step(self):
        """The other self-disable: naming all three empties the required list.

        Same false green as a broken listing, by configuration instead of by
        accident, and it used to be silent.
        """
        every = ",".join([
            "e2e::bench.render",
            "e2e::bench.render-impact",
            "e2e::bench.render-responsiveness",
        ])
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-all-allowed-") as td:
            root = Path(td)
            self._write_comparison_reports(
                root, 0.11, 0.21, omit_renderer="bench.render-impact")
            completed, _ = self._execute_compare(
                root, allowed_removals=every)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            self.assertIn("every workload is an allowed removal",
                          completed.stderr)

    def test_baseline_renderer_rounds_share_head_outputs_and_selfcheck(self):
        block = self._run_block("Run renderer workloads, interleaved")
        self.assertIn('--work-root "$RUNNER_TEMP/gh7-bench/$side"', block)
        self.assertIn('if [ "$side" = "head" ] && [ "$round" -eq '
                      '"$ROUNDS" ]; then', block)
        self.assertIn("selfcheck=(--selfcheck)", block)


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
