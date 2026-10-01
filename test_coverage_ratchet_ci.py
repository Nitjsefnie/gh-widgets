

"""Invariants over coverage-ratchet.yml — the release-gate surface.

    python3 -m unittest discover -v


WHAT THIS MODULE OWNS. Everything about `.github/workflows/coverage-ratchet.yml`
and its coupling to `.github/workflows/release.yml`: what the workflow is allowed
to run with read-only access, how its candidate floor is announced, what its
check run is called, and what measured population in tests.yml has to be.
It also owns the small structural reader those cases are written against.

These checks are regression tripwires over reviewed source, not a sandbox. The
permission reader decodes simple YAML keys, with optional single or double
quotes, and fails closed outside that subset. The shell reader removes a
backslash-newline splice without inserting whitespace, decodes words with POSIX
`shlex.split`, then splits each decoded token on command-terminating operators
`;|&()`. A word after one of those operators (or at line start) is a command
word. Operators inside quoted words are split too; this fail-noisy behavior is
accepted. `<` and `>` are deliberately not split: their following words are
redirection targets, not commands (`echo hi>gh api` runs `echo`, not `gh`).
Bash ANSI-C quoting (`$'gh'`), backticks, `$(...)`, and other substitution
spellings are OUT OF SCOPE.


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
import shlex
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


def _yaml_scalar(value):
    """Decode a simple YAML scalar; reject malformed quote spellings."""
    value = _without_comment(value).strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise AssertionError(f"unrecognised quoted YAML scalar: {value!r}")
        value = value[1:-1]
    elif "'" in value or '"' in value:
        raise AssertionError(f"unrecognised YAML scalar: {value!r}")
    return value


def _yaml_key(value):
    """Decode a simple YAML key, allowing optional single or double quotes."""
    key = _yaml_scalar(value)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        raise AssertionError(f"unrecognised YAML declaration key: {value!r}")
    return key


def _block(records, indent, key):
    """The records nested under `key` at `indent`; [] for a scalar or absent."""
    for index, (at, content) in enumerate(records):
        if at != indent or content.startswith("- "):
            continue
        name, value = _partition(content)
        if _yaml_key(name) != key:
            continue
        if _without_comment(value):
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


def _without_job_permissions(text):
    """Remove supported job permission overrides for isolated fixtures."""
    out = []
    in_jobs = False
    dropping_block = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        indent = _indent_of(line)
        if not stripped or stripped.startswith("#"):
            if not (dropping_block and indent > 4):
                out.append(line)
            continue
        if dropping_block and indent > 4:
            continue
        dropping_block = False
        if indent == 0:
            name, value = _partition(stripped)
            in_jobs = (_yaml_key(name) == "jobs"
                       and not _without_comment(value))
        if in_jobs and indent == 4:
            name, value = _partition(stripped)
            if _yaml_key(name) == "permissions":
                dropping_block = not _without_comment(value)
                continue
        out.append(line)
    return "".join(out)


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
    """A step run block as raw lines for checking branch-local effects."""
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
    """A step's `run:` block with shell line splices removed.

    Read from the step's own body and only from the lines nested under its
    `run:` key, so an `if:` or an `env:` above it is not mistaken for shell.

    The YAML lines are joined with a separating space. Removing the splice
    backslash and following whitespace leaves the source space before the
    backslash, so `pip \\` + `install` becomes `pip install`.
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
    joined = re.sub(r"\\\s*", "", " ".join(lines))
    return re.sub(r"\s+", " ", joined).strip()


def _flow_mapping_entries(value):
    """Parse a small YAML flow mapping, rejecting shapes this reader lacks."""
    value = value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        raise AssertionError(f"unrecognised permissions mapping: {value!r}")
    inner = value[1:-1].strip()
    if not inner:
        return {}

    entries = []
    quote = None
    start = 0
    for index, character in enumerate(inner):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == ",":
            entries.append(inner[start:index].strip())
            start = index + 1
    if quote:
        raise AssertionError(f"unrecognised permissions mapping: {value!r}")
    entries.append(inner[start:].strip())
    if not all(entries):
        raise AssertionError(f"unrecognised permissions mapping: {value!r}")

    scopes = {}
    for entry in entries:
        key, permission = _flow_partition(entry)
        key = _yaml_key(key)
        if not key or key in scopes:
            raise AssertionError(f"unrecognised permissions mapping entry: {entry!r}")
        scopes[key] = _permission_value(permission)
    return scopes


def _flow_partition(entry):
    """Split a flow mapping entry at its first colon outside quotes."""
    quote = None
    for index, character in enumerate(entry):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == ":":
            key, value = entry[:index].strip(), entry[index + 1:].strip()
            if key and value:
                return key, value
            break
    raise AssertionError(f"unrecognised permissions mapping entry: {entry!r}")


def _permission_value(value):
    """Decode a permission value and fail closed on unsupported YAML."""
    decoded = _yaml_scalar(value)
    if decoded not in {"read", "write", "none"}:
        raise AssertionError(f"unrecognised permission value: {decoded!r}")
    return decoded


def _job_permissions(records):
    """Return a job's scopes; decode simple quoted keys and fail closed."""
    declarations = []
    for at, content in records:
        if at != 4 or content.startswith("- "):
            continue
        name, value = _partition(content)
        if _yaml_key(name) == "permissions":
            declarations.append(value)
    if len(declarations) > 1:
        raise AssertionError("duplicate permissions declaration in job")
    if not declarations:
        return {}  # An absent job override inherits the read-only workflow map.

    value = _without_comment(declarations[0])
    if value in {"read-all", "write-all"}:
        return {"*": value.split("-", maxsplit=1)[0]}
    if value.startswith("{"):
        return _flow_mapping_entries(value)
    if value:
        raise AssertionError(f"unrecognised permissions shape: {value!r}")

    block = _block(records, 4, "permissions")
    if not block:
        raise AssertionError("unrecognised empty permissions declaration")
    indent = block[0][0]
    scopes = {}
    for at, content in block:
        if at != indent:
            raise AssertionError(
                f"unrecognised nested permissions shape: {content!r}")
        key, permission = _partition(content)
        key = _yaml_key(key)
        if not key or key in scopes or not permission:
            raise AssertionError(
                f"unrecognised permissions mapping entry: {content!r}")
        scopes[key] = _permission_value(permission)
    return scopes


def _assert_no_job_write_scopes(text):
    """Reject every declared job writer, including writers in newly added jobs."""
    jobs = _jobs(text)
    if not jobs:
        raise AssertionError("workflow has no readable jobs")
    writers = {
        job for job, records in jobs.items()
        if "write" in _job_permissions(records).values()}
    if writers:
        raise AssertionError(
            f"jobs hold a write scope: {', '.join(sorted(writers))}")


def _step_github_api_calls(text):
    """Return commands with adjacent decoded `gh api` shell words.

    `_command()` removes shell line splices; `shlex.split()` decodes POSIX
    words, then `;|&()` delimit command words even when adjoining them.
    Redirection `<` and `>` stay attached because their following words are
    targets, not commands. Operators inside quotes are also split, which is
    fail-noisy and accepted. This is not a sandbox: Bash ANSI-C quoting,
    backticks, `$(...)`, and other substitution spellings are OUT OF SCOPE.
    """
    calls = []
    for job, records in _jobs(text).items():
        for step in _steps(records):
            command = _command(step)
            try:
                shell_words = shlex.split(command)
            except ValueError as error:
                raise AssertionError(
                    f"uninterpretable shell command in {job}/{step['name']}: "
                    f"{error}") from error
            words = [fragment for word in shell_words
                     for fragment in re.split(r"[;|&()]", word) if fragment]
            if any(left == "gh" and right == "api"
                   for left, right in zip(words, words[1:])):
                calls.append((job, step["name"], command))
    return calls


def _assert_no_step_github_api(text):
    calls = _step_github_api_calls(text)
    if calls:
        raise AssertionError(f"workflow steps invoke gh api: {calls!r}")


class TestTheWorkflowReader(unittest.TestCase):
    """The reader itself, on the shapes it is known to reshape.

    Each case below exists because the thing it pins was a demonstrated false
    green in this file, not because the reader is interesting.
    """

    def test_a_line_continued_command_normalises_to_single_spaces(self):
        # Shell removes the splice but preserves the source space before it,
        # so `pip \` + `install` remains two words with one separator.
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
    """The read-only coverage ratchet, read as structure.

    Cases pin the main-branch trigger, permissions, candidate artifact,
    announcement, check-run name, and measured cell.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = RATCHET_WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _jobs(cls.text)

    def test_it_runs_on_a_push_to_main_and_on_nothing_else(self):
        # The finite event domain is what this file DECLARES, so the
        # enumeration comes from the trigger block rather than from a list of
        # events someone remembered to check. A pull request would measure a
        # branch outside the release gate's main-branch lifecycle.
        records = _records(self.text)
        triggers = _block(records, 0, "on")
        names = {_partition(content)[0] for at, content in triggers
                 if at == 2 and not content.startswith("- ")}
        self.assertEqual(names, {"push"})
        push = _block(triggers, 2, "push")
        branches = _flow_list(_child_value(push, "branches"))
        self.assertEqual(branches, ["main"])

    def test_the_workflow_itself_is_read_only(self):
        records = _records(self.text)
        self.assertEqual(
            _mapping(_block(records, 0, "permissions")),
            {"contents": "read"})

    def test_no_job_holds_any_write_scope(self):
        # Missing job overrides inherit the top-level read map. Every declared
        # override is decoded or rejected, including scalar and flow forms.
        _assert_no_job_write_scopes(self.text)

    def test_each_valid_job_writer_spelling_is_caught(self):
        # These are valid YAML shapes that must not disappear at the reader
        # boundary. The added job also pins iteration beyond the current job.
        base = _without_job_permissions(self.text)
        marker = "    runs-on: ubuntu-latest"
        self.assertIn(marker, base)
        plants = {
            "write-all": base.replace(
                marker, "    permissions: write-all\n" + marker, 1),
            "double-quoted declaration key": base.replace(
                marker,
                '    "permissions": {contents: write}\n' + marker,
                1),
            "single-quoted declaration key": base.replace(
                marker,
                "    'permissions': write-all\n" + marker,
                1),
            "inline flow mapping": base.replace(
                marker, "    permissions: {contents: write}\n" + marker, 1),
            "quoted inline value": base.replace(
                marker,
                '    permissions: {contents: "write"}\n' + marker,
                1),
            "quoted block value": base.replace(
                marker,
                '    permissions:\n      contents: "write"\n' + marker,
                1),
            "new writer job": base + (
                "\n  extra-writer:\n"
                "    permissions:\n"
                "      contents: write\n"
                "    steps:\n"
                "      - run: echo writer\n"),
        }
        for spelling, planted in plants.items():
            with self.subTest(spelling=spelling):
                self.assertNotEqual(planted, base)
                with self.assertRaisesRegex(AssertionError, "write scope"):
                    _assert_no_job_write_scopes(planted)

        # The valid read-all shorthand and quoted read value remain allowed.
        for declaration in (
                "    permissions: read-all\n",
                '    permissions: {contents: "read"}\n',
                '    permissions:\n      contents: "read"\n'):
            with self.subTest(read_only=declaration):
                _assert_no_job_write_scopes(
                    base.replace(marker, declaration + marker, 1))

    def test_permission_fixture_isolation_removes_each_declaration_form(self):
        # Plant into a clean workflow and require exact restoration, including
        # preserving the job and its unrelated fields.
        base = _without_job_permissions(self.text)
        marker = "    runs-on: ubuntu-latest"
        declarations = {
            "block": "    permissions:\n      contents: write\n",
            "flow": "    permissions: {contents: write}\n",
            "scalar": "    permissions: write-all\n",
            "double-quoted key": (
                '    "permissions": {contents: write}\n'),
            "single-quoted key": "    'permissions': write-all\n",
            "commented block header": (
                "    permissions: # override\n      contents: write\n"),
        }
        for spelling, declaration in declarations.items():
            with self.subTest(spelling=spelling):
                planted = base.replace(marker, declaration + marker, 1)
                isolated = _without_job_permissions(planted)
                self.assertEqual(isolated, base)
                self.assertEqual(set(_jobs(isolated)), set(_jobs(base)))
                self.assertIn("measure", _jobs(isolated))
                self.assertEqual(
                    _job_permissions(_jobs(isolated)["measure"]), {})

    def test_an_unrecognised_job_permission_shape_fails_closed(self):
        base = _without_job_permissions(self.text)
        marker = "    runs-on: ubuntu-latest"
        planted = base.replace(
            marker, "    permissions: maybe-write\n" + marker, 1)
        with self.assertRaisesRegex(AssertionError,
                                    "unrecognised permissions shape"):
            _assert_no_job_write_scopes(planted)

    def test_the_candidate_floor_is_computed_in_the_read_only_job(self):
        # The candidate is computed on a copy and uploaded for the human
        # pull-request procedure.
        command = _command(
            next(step for step in _steps(self.jobs["measure"])
                 if step["name"] == "Compute the candidate floor"))
        self.assertIn("--floor-file candidate-coverage-floor.json", command)
        uploads = [step for step in _steps(self.jobs["measure"])
                   if (step["uses"] or "").startswith(
                       "actions/upload-artifact@")]
        self.assertEqual(len(uploads), 2, uploads)
        names = [_child_value(step["with"], "name") for step in uploads]
        self.assertCountEqual(
            names, ["coverage-json", "coverage-floor-candidate"])

    def test_the_climbable_floor_is_announced_after_computation(self):
        # The notice and summary belong only to the strict-climb branch.
        steps = _steps(self.jobs["measure"])
        names = [step["name"] for step in steps]
        announce_name = "Announce a climbable floor"
        self.assertEqual(names.count(announce_name), 1)
        self.assertEqual(names.index(announce_name),
                         names.index("Compute the candidate floor") + 1)
        step = steps[names.index(announce_name)]
        command = _command(step)
        self.assertIn("jq -r '.floor' candidate-coverage-floor.json", command)
        self.assertIn("jq -r '.floor' coverage-floor.json", command)
        self.assertIn("awk", command)
        self.assertRegex(command, r"\bcandidate\s+>\s+committed\b")
        self.assertNotRegex(
            command, r"\bcandidate\s*(?:>=|<=|<)\s*committed\b")
        self.assertEqual(command.count("::notice::"), 1)
        self.assertIn("$GITHUB_STEP_SUMMARY", command)
        self.assertIn("$candidate", command)
        self.assertIn("$committed", command)
        self.assertIn("coverage-floor-candidate", command)
        self.assertIn("pull request", command)
        self.assertIn("CONTRIBUTING.md", command)

        lines = _run_lines(step)
        announce_branch = lines.index('if [ "$climbed" = 1 ]; then')
        quiet_branch = lines.index("else", announce_branch)
        summary_writes = [index for index, line in enumerate(lines)
                          if '>> "$GITHUB_STEP_SUMMARY"' in line]
        self.assertEqual(len(summary_writes), 1)
        self.assertGreater(summary_writes[0], announce_branch)
        self.assertLess(summary_writes[0], quiet_branch)
        self.assertFalse(any("GITHUB_STEP_SUMMARY" in line
                             for line in lines[quiet_branch + 1:]))

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
        # One checkout remains in the read-only job and does not persist its
        # credentials. The count is asserted so deleting the checkout cannot
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

    def test_no_step_calls_the_github_api_or_sets_gh_token(self):
        # Check the whole workflow so job-level or top-level env cannot restore
        # a token that the individual step bodies do not declare.
        self.assertEqual(_step_github_api_calls(self.text), [])
        self.assertNotIn("GH_TOKEN", self.text)

    def test_each_github_api_spacing_spelling_is_caught(self):
        # The command reader folds shell continuations and normalises spacing
        # before the invocation is matched.
        marker = "          cp coverage-floor.json"
        self.assertIn(marker, self.text)
        plants = {
            "double space": self.text.replace(
                marker, "          gh  api repos/example/example\n" + marker,
                1),
            "line continuation": self.text.replace(
                marker,
                "          gh \\\n            api repos/example/example\n" + marker,
                1),
        }
        for spelling, planted in plants.items():
            with self.subTest(spelling=spelling):
                self.assertNotEqual(planted, self.text)
                with self.assertRaisesRegex(AssertionError, "invoke gh api"):
                    _assert_no_step_github_api(planted)

    def test_each_quoted_or_spliced_api_word_is_caught(self):
        marker = "          cp coverage-floor.json"
        plants = {
            "quoted command word": self.text.replace(
                marker,
                '          "gh" api repos/example/example\n' + marker,
                1),
            "quoted operation word": self.text.replace(
                marker,
                "          gh 'api' repos/example/example\n" + marker,
                1),
            "midword continuation": self.text.replace(
                marker,
                "          gh a" + "\\\n"
                + "          pi repos/example/example\n" + marker,
                1),
        }
        for spelling, planted in plants.items():
            with self.subTest(spelling=spelling):
                self.assertNotEqual(planted, self.text)
                with self.assertRaisesRegex(AssertionError, "invoke gh api"):
                    _assert_no_step_github_api(planted)

    def test_each_shell_control_operator_boundary_is_caught(self):
        marker = "          cp coverage-floor.json"
        plants = {
            "subshell grouping": self.text.replace(
                marker,
                "          (gh api repos/example/example)\n" + marker,
                1),
            "command separator": self.text.replace(
                marker,
                "          true;gh api repos/example/example\n" + marker,
                1),
            "pipeline": self.text.replace(
                marker,
                '          printf ""|gh api repos/example/example\n' + marker,
                1),
        }
        for spelling, planted in plants.items():
            with self.subTest(spelling=spelling):
                self.assertNotEqual(planted, self.text)
                with self.assertRaisesRegex(AssertionError, "invoke gh api"):
                    _assert_no_step_github_api(planted)
        # Bash runs `echo`; `gh` is the redirection target and `api` an
        # argument. Splitting `>` would invent a command Bash never runs.
        redirected = self.text.replace(
            marker, "          echo done>gh api\n" + marker, 1)
        self.assertNotEqual(redirected, self.text)
        self.assertEqual(_step_github_api_calls(redirected), [])

    def test_quoted_read_permission_and_api_prose_stay_allowed(self):
        marker = "    runs-on: ubuntu-latest"
        command_marker = "          cp coverage-floor.json"
        base = _without_job_permissions(self.text)
        self.assertIn(marker, base)
        self.assertIn(command_marker, base)
        planted = base.replace(
            marker,
            '    "permissions": {contents: "read"}\n' + marker,
            1).replace(
                command_marker,
                '          echo "gh api"\n' + command_marker,
                1)
        _assert_no_job_write_scopes(planted)
        self.assertEqual(_step_github_api_calls(planted), [])

    def test_an_unbalanced_shell_quote_fails_closed(self):
        marker = "          cp coverage-floor.json"
        planted = self.text.replace(
            marker, '          echo "unclosed\n' + marker, 1)
        with self.assertRaisesRegex(
                AssertionError, "uninterpretable shell command"):
            _assert_no_step_github_api(planted)

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

    def test_a_newer_main_supersedes_an_in_flight_measurement(self):
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
