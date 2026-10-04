#!/usr/bin/env python3
"""Require the head being checked out to carry every commit main holds that
touches a file the REQUIRED status checks read as a parameter.

A green pull-request head only vouches for the tree its checks read. If
main advances a gate-defining file before merge, that verdict does not cover
the updated gate. Require the head to carry those commits before merging.

The set of such files is DERIVED from the workflows under .github/workflows,
not kept in a list here. A remembered list is only as current as the last time
somebody remembered to add to it, which is this defect wearing different
clothes: a gate whose own inputs are enumerated by hand goes stale exactly the
way this check exists to prevent.

The only hand-held entry is REQUIRED_JOBS, because the ruleset that makes a
status context required lives in repository settings, which are not in the
repository. Every name there must still be FOUND in a workflow below or the
run refuses: a job renamed out of every workflow would otherwise shrink the
set in silence, and a narrower set reports green over a wider gate.

The workflow defining a required job is itself a gate input. Here that is
`tests.yml`, whose `aggregate` job runs `scripts/ci/aggregate_gate.py`.
The derivation also follows static imports of tracked Python modules, so
`changes_detect.py`, which defines applicability, is included without a
second hand-held path list.

WHAT IT CANNOT SEE. The derivation reads text and static Python imports:

  - A tool reading a configuration file no `run:` names.
  - The whole-tree spelling. `git grep ... -- .` in the `gates` job reads
    EVERY tracked file. `.` resolves to nothing here: enforcing freshness
    for the whole tree would require a rebase on every main change, beyond
    the gate-defining files this check is meant to protect.
  - Dynamic imports, imports resolved through runtime sys.path changes,
    files a script opens without a static import, and scripts discovered
    at runtime. Following `aggregate`'s static imports
    includes the selector, but does not enumerate every gate's test inputs.

Runs on the standard library alone. The workflows are read with a parser for
the block layout this repository uses rather than a YAML dependency, because
the gate job installs no dependencies. A parser that refuses a shape it does
not model is better than a silent misreading.

    scripts/ci/gate_base_freshness.py [--root DIR] [--print-paths]

--print-paths writes the derived set, one path per line, and is how the suite
pins the derivation.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

# The required status contexts. Hand-held, and only here: a ruleset is
# repository configuration, not a file, so nothing under .github/workflows can
# be asked which jobs it makes required. Each name is still looked up in the
# workflows below, and a name with no job behind it is a refusal rather than a
# silently smaller set.
REQUIRED_JOBS = ("aggregate",)

# The branch a required check's head is compared against. Not configurable:
# a second branch here would be a second base, and this question has one.
BASE_BRANCH = "main"

# The byte size at which a pathspec argument list is refused rather than
# attempted. The kernel's own limit is `getconf ARG_MAX`, which is not a fixed
# number and varies by platform, so this is a deliberately low ceiling:
# every repository that reaches it is far past this one's size, and a refusal
# names the cause where an E2BIG from exec would be a traceback.
PATHSPECS_MAX_BYTES = 65536

WORKFLOW_DIR = ".github/workflows"


class GateError(Exception):
    """This run could not establish the answer, and says so instead."""


class WorkflowError(GateError):
    """A workflow whose shape this parser does not model."""


def git(root, *arguments, what):
    """Run one git command in `root` and return its stdout.

    Every call goes through here because a guard that reads its own error as a
    clean tree is the false green this exists to prevent: a non-zero status is
    a refusal naming what was being attempted, never an empty answer.
    """
    command = ("git", "-C", str(root)) + arguments
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        detail = done.stderr.strip() or "no output"
        raise GateError(
            f"cannot {what}: `{' '.join(command)}` exited {done.returncode}: {detail}")
    return done.stdout


# --- reading the workflows -------------------------------------------------
#
# The subset of YAML these workflows use: explicit block mappings, explicit
# sequence entries, and block scalars for `run:`. Every step below refuses a
# shape it does not model rather than guessing at one, because a guessed shape
# yields a smaller path set, and a smaller path set is a green over a gate that
# was never checked.

MAPPING = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$")
SEQUENCE = re.compile(r"^- (?P<rest>.+)$")
BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def skippable(line):
    """A blank line or a whole-line comment, which carries no node.

    Only ever asked of lines OUTSIDE a block scalar: inside one a `#` line is
    the step author's own comment, carried to bash verbatim, and dropping it
    would change what the step runs.
    """
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def block_end(lines, start, indent):
    """The index just past the node whose key sits at `start` with `indent`."""
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if not skippable(line) and indent_of(line) <= indent:
            break
        index += 1
    return index


def block_scalar(lines, start, key_indent, limit):
    """The text of the `|` scalar introduced at `start`, and the index after it.

    The block's indentation is the first non-blank body line's, which is what
    YAML takes as its indicator when the header carries no explicit one. Every
    line is kept, comments included: they are bytes bash receives.
    """
    body = []
    index = start + 1
    while index < limit:
        line = lines[index]
        if line.strip() and indent_of(line) <= key_indent:
            break
        body.append(line)
        index += 1
    leads = [indent_of(line) for line in body if line.strip()]
    lead = min(leads) if leads else key_indent + 2
    return "\n".join(line[lead:] if len(line) > lead else "" for line in body), index


def top_level_keys(lines, key):
    """[(line index, match)] for column-0 block entries whose key is `key`.

    One `MAPPING.match` per line, narrowed once: calling the match twice —
    once as a guard, once for its groups — reads to the type checker as two
    unrelated optionals.
    """
    found = []
    for index, line in enumerate(lines):
        if skippable(line) or indent_of(line) != 0:
            continue
        match = MAPPING.match(line)
        if match is not None and match["key"] == key:
            found.append((index, match))
    return found


def workflow_steps(text, workflow):
    """{job name: [step, ...]} for one workflow, each step its `run` and `uses`."""
    lines = text.splitlines()
    # Every top-level `jobs:` is collected rather than the first one taken: a
    # document with two of them says which jobs this workflow defines only if
    # you know which half won, and a workflow whose jobs are a flow mapping
    # (`jobs: {build: ...}`) says nothing this reader can read at all. Both are
    # refusals, because the alternative is a smaller set with nothing said.
    job_matches = top_level_keys(lines, "jobs")
    tops = [index for index, match in job_matches]
    if len(tops) != 1:
        raise WorkflowError(
            f"{workflow} has {len(tops)} top-level `jobs:` mappings; this "
            f"reader models exactly one, so which jobs it defines cannot be "
            f"established")
    if job_matches[0][1]["value"] is not None:
        raise WorkflowError(
            f"{workflow} writes `jobs:` as a flow mapping; this reader models "
            f"only the block form")

    jobs = {}
    index = tops[0] + 1
    while index < len(lines):
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) < 2:
            break
        if indent_of(line) != 2:
            raise WorkflowError(
                f"{workflow}: expected a job entry under `jobs:`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match or match["value"] is not None:
            raise WorkflowError(f"{workflow}: expected a job entry, found {line!r}")
        name = match["key"]
        if name in jobs:
            raise WorkflowError(f"{workflow}: duplicate job {name!r}")
        jobs[name] = (index + 1, block_end(lines, index, 2))
        index = jobs[name][1]

    return {name: job_steps(lines, name, span, workflow)
            for name, span in jobs.items()}


def job_steps(lines, name, span, workflow):
    start, end = span
    steps = []
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 4:
            raise WorkflowError(
                f"{workflow}: expected a step list or a job key in job "
                f"`{name}`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a job key in job `{name}`, found {line!r}")
        if match["key"] == "steps" and match["value"] is not None:
            # `steps: [{run: ...}]` is a real spelling, and reading the key's
            # block and finding no entries in it yields an empty step list — a
            # plausible answer rather than a refusal, for a job whose steps are
            # right there.
            raise WorkflowError(
                f"{workflow}: `steps:` carries a value in job `{name}`; this "
                f"reader models only the block form")
        if match["key"] != "steps":
            # Every other job key — runs-on, strategy, env, permissions — is a
            # mapping or a scalar that never names a file this check reads, and
            # its children sit at an indent a step's own keys also use. Skipping
            # the key's whole block rather than its first line is what keeps
            # `permissions:`'s children from being read as steps.
            index = block_end(lines, index, 4)
            continue
        index = step_entries(lines, name, index + 1, block_end(lines, index, 4), workflow, steps)
    return steps


def step_entries(lines, name, start, end, workflow, steps):
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        entry = SEQUENCE.match(line.strip()) if indent_of(line) == 6 else None
        if entry is None:
            raise WorkflowError(
                f"{workflow}: expected a step entry in job `{name}`, found {line!r}")
        stop = block_end(lines, index, 6)
        steps.append(step_fields(lines, name, index + 1, stop, workflow,
                                 entry["rest"]))
        index = stop
    return index


def apply_field(lines, end, key, value, at, step):
    """Fold one `key: value` of a step into `step`, and return the next line.

    `at` is the line the key was written on, which for a key sitting on a
    step's own `- ` line is that dash line rather than the one after it.
    """
    if key == "run":
        if not value or BLOCK_SCALAR.match(value):
            step["run"], after = block_scalar(lines, at, 8, end)
            return after
        step["run"] = value
    elif key == "uses":
        # The value is the reference alone; the `# v7.0.1` after it is a
        # comment, and an action reference never contains a space.
        step["uses"] = value.split()[0] if value else None
    elif not value or BLOCK_SCALAR.match(value):
        # A `with:`/`env:` mapping or a block scalar the step carries but does
        # not execute. Its text is an input to a step, not a file a step reads,
        # and reading one is how an expression's spelling would be mistaken for
        # a path. Its lines are stepped over rather than read as step keys.
        return block_end(lines, at, 8)
    return at + 1


def step_fields(lines, name, start, end, workflow, dash):
    """One step's `run:` text and its `uses:` value.

    `dash` is the text following the `- ` on the step's own line, and it is the
    FIRST key the step has. A step written on one line — `- uses:
    actions/checkout@…`, the spelling every checkout step in this repository
    uses — carries no key on the following lines at all, so a walk that starts
    after the dash line reads a step with no keys in it: a `uses:` that never
    reaches the local-action branch, and a `run:` that contributes no path.
    """
    step = {"run": None, "uses": None}
    index = start
    if dash:
        match = MAPPING.match(dash)
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a key on the step entry in job `{name}`, "
                f"found {dash!r}")
        index = apply_field(lines, end, match["key"],
                            (match["value"] or "").strip(), start - 1, step)
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 8:
            raise WorkflowError(
                f"{workflow}: expected a key in a step of job `{name}`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a key in a step of job `{name}`, found {line!r}")
        index = apply_field(lines, end, match["key"],
                            (match["value"] or "").strip(), index, step)
    return step


# --- the paths a required check reads --------------------------------------

# The platforms expand `${{ }}` before bash sees a byte, so a path a step
# reaches THROUGH an expression is not text this matcher can see. Removing the
# expressions rather than keeping them keeps a resolved value from being read as
# a path; either way the limit is the same and is named in the report.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
CANDIDATE = re.compile(r"[A-Za-z0-9._/-]+")


def candidates(text):
    return CANDIDATE.findall(EXPRESSION.sub(" ", text))


def resolve(candidate, files):
    """The tracked files a candidate names, or nothing.

    Resolution against the tree IS the filter: a token that names nothing here
    is not a path this check needs to watch, and a token that names a file or a
    directory is one. There is no shape rule on top of that, because a shape
    rule is a second remembered list — a tracked file with an unfamiliar suffix must not leave the set
    merely because its spelling was not anticipated.

    There is no glob arm, and its absence is deliberate: `candidates()` cannot
    produce a glob metacharacter, so an arm reading one would be a branch that
    looks like coverage and reaches nothing. The spelling it would have served
    is already covered — `.github/workflows/*.yml` breaks at the `*` and the
    `.github/workflows/` that precedes it is a directory, so every workflow is
    taken whole, which is what that glob meant.

    `.` resolves to nothing, and that is a named reach limit rather than an
    oversight: `.` and `--` are how these tools spell "every tracked file", so
    the gates job's merge-marker step really does read all of them. See
    the module docstring for why the whole-tree arm is the maintainer's call.
    """
    candidate = candidate[2:] if candidate.startswith("./") else candidate
    candidate = candidate.rstrip("/")
    if not candidate:
        return ()
    if candidate in files:
        return (candidate,)
    prefix = candidate + "/"
    return tuple(f for f in files if f.startswith(prefix))


def tracked_files(root):
    """The tracked paths at HEAD, enumerated in the form this gate can trust.

    `-z` with a NUL split is the only enumeration git will not rewrite: in the
    default text form a path carrying non-ASCII bytes comes back C-quoted
    under `core.quotePath` — `".github/workflows/\\303\\274ber.yml"`, opening
    with a literal `"` — so it never matches the `WORKFLOW_DIR` prefix, is
    never parsed, and drops out of the derived set in silence: one workflow a
    stale base commit can touch without this gate going red. A replacement
    decode is not an option, because it would hand every later git call
    (`cat-file`, the pathspec list) a path that resolves to nothing; a name
    whose bytes are not valid UTF-8 is therefore a refusal naming the raw
    bytes. This call sits outside the `git()` helper on purpose — bytes are
    needed — while
    a non-zero exit is refused in the helper's own style and for the helper's
    own reason: a guard that reads its error as a clean tree is the false
    green this script exists to prevent.
    """
    command = ("git", "-C", str(root), "ls-tree", "-z", "-r", "--name-only",
               "--full-tree", "HEAD")
    # Outside `git()` on purpose: bytes are needed. See the docstring for
    # the refusal style, which remains the helper's.
    done = subprocess.run(command, capture_output=True, check=False)
    if done.returncode != 0:
        detail = done.stderr.strip().decode("utf-8", errors="replace")
        raise GateError(
            f"cannot list the tracked tree: `{' '.join(command)}` exited "
            f"{done.returncode}: {detail or 'no output'}")
    files = []
    for raw in done.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            files.append(raw.decode("utf-8"))
        except UnicodeDecodeError as refusal:
            raise GateError(
                f"a tracked path is not valid UTF-8, so the paths this gate "
                f"derives could not name it and every later git call would "
                f"resolve a path to nothing: {raw!r}") from refusal
    return tuple(files)


def workflow_names(files):
    return sorted(f for f in files
                  if f.startswith(WORKFLOW_DIR + "/")
                  and (f.endswith(".yml") or f.endswith(".yaml")))


def python_import_paths(root, paths, files):
    """Static Python imports resolved from the repository root or relatively.

    Read HEAD blobs, just like the workflows. External modules contribute
    nothing; local imports contribute their module and package initializers.
    A visited set bounds cycles by the tracked tree's size. Dynamic imports
    and runtime file reads remain the named reach limit above.
    """
    pending = [path for path in paths if path.endswith(".py")]
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            tree = ast.parse(
                git(root, "cat-file", "blob", f"HEAD:{path}", what=f"read {path}"),
                filename=path)
        except SyntaxError as refusal:
            raise GateError(f"cannot read Python imports in {path}: {refusal}") from refusal
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parents = path.split("/")[:-node.level]
                    prefix = ".".join([*parents, prefix]).rstrip(".")
                modules.append(prefix)
                modules.extend(f"{prefix}.{alias.name}" for alias in node.names)
        for module in modules:
            candidates_ = [module.replace(".", "/") + ".py"]
            candidates_.extend("/".join(module.split(".")[:end]) + "/__init__.py"
                               for end in range(1, len(module.split(".")) + 1))
            for imported in candidates_:
                if imported in files and imported not in paths:
                    paths.add(imported)
                    pending.append(imported)


def gate_paths(root):
    """Gate-defining paths derived from workflows and static local imports.

    Every workflow is parsed, not only the ones a required job turns out to be
    in: a workflow this cannot read is a workflow whose steps it cannot account
    for, and skipping it would be the same quiet narrowing the refusal exists
    to stop.
    """
    files = tracked_files(root)
    if not files:
        raise GateError("HEAD tracks no files, so the tree to compare is not there")
    parsed = {}
    for workflow in workflow_names(files):
        parsed[workflow] = workflow_steps(
            git(root, "cat-file", "blob", f"HEAD:{workflow}",
                what=f"read {workflow}"), workflow)
    derived = set()
    for job in REQUIRED_JOBS:
        # Every workflow defining the name, unioned. A job name is unique within
        # a workflow, not across the repository: a second workflow may define
        # `aggregate:` — another workflow with the same required check,
        # nothing renamed — and taking the first match silently drops the other
        # one's steps, so a file one required check compiles leaves the gate with
        # no message and exit 0. A refusal on a second definition would be
        # defensible too, and it would block that ordinary change until a
        # maintainer answered for it; the union cannot narrow and cannot block.
        defining = sorted(name for name, jobs in parsed.items() if job in jobs)
        if not defining:
            raise GateError(
                f"no workflow under {WORKFLOW_DIR}/ defines the required job "
                f"`{job}`, so the files the required checks read cannot be built: "
                f"put the job back, or update REQUIRED_JOBS if it was renamed")
        for name in defining:
            # The required job's steps, needs graph and conditions live here.
            derived.add(name)
            for step in parsed[name][job]:
                uses = step["uses"] or ""
                if uses.startswith("./"):
                    # A step running a composite action out of THIS repository
                    # reads that action's files as its own parameters, and they
                    # are not text in this workflow. The directory is taken whole
                    # rather than its `action.yml` alone: what the action reaches
                    # from inside is the same question this matcher cannot
                    # answer, and a partial answer would be a narrower gate than
                    # it looks.
                    derived.update(files if uses == "./" else resolve(uses, files))
                if not step["run"]:
                    continue
                for candidate in candidates(step["run"]):
                    derived.update(resolve(candidate, files))
    if not derived:
        raise GateError(
            "the required checks name no file in this tree, so there is nothing "
            "to compare and this run can vouch for nothing")
    python_import_paths(root, derived, files)
    return sorted(derived)


# --- the comparison --------------------------------------------------------


def fetch_base(root):
    """Bring main in as it is NOW, not as the checkout left it.

    actions/checkout fetches one commit of one ref by default, so a main that
    moved after that run left nothing here to compare against — which is the
    whole question. Anonymous on purpose: every checkout in this repository
    sets persist-credentials: false and the repository is public.

    --unshallow deepens the ref actually being compared. Without it the grafted
    boundary hides the head's own ancestry, git cannot tell which of main's
    commits the head already carries, and the check reports main's history
    against it — naming commits whose content is sitting in the checkout. That
    is a red nobody can satisfy by rebasing.
    """
    arguments = ["fetch", "--no-tags", "--quiet"]
    if git(root, "rev-parse", "--is-shallow-repository",
           what="ask whether the checkout is shallow").strip() == "true":
        arguments.append("--unshallow")
    arguments += ["origin", f"+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}"]
    git(root, *arguments, what=f"fetch origin/{BASE_BRANCH}")


def stale_commits(root, head, base, paths):
    """[(sha, subject, [paths])] for the commits base holds that head lacks.

    `--` with nothing after it means EVERY path, so the empty set is refused
    here rather than handed to git: comparing unrelated main commits would
    no longer answer the gate-defining-path question.
    """
    if not paths:
        raise GateError("the derived path set is empty, so there is nothing to compare")
    sized = sum(len(path.encode("utf-8")) + 1 for path in paths)
    if sized > PATHSPECS_MAX_BYTES:
        raise GateError(
            f"the derived path set is {sized} bytes, past the {PATHSPECS_MAX_BYTES} "
            f"this run will put in an argument list")
    listing = subprocess.run(
        ("git", "-C", str(root), "log", f"{head}..{base}", "--name-only",
         "--format=%x00%H%x09%s", "--", *paths),
        capture_output=True, text=True, check=False)
    if listing.returncode != 0:
        raise GateError(
            f"cannot list what {BASE_BRANCH} holds that this head lacks: "
            f"`git log {head}..{base}` exited {listing.returncode}: "
            f"{listing.stderr.strip() or 'no output'}")
    stale = []
    for chunk in listing.stdout.split("\0"):
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        sha, _, subject = header.partition("\t")
        stale.append((sha.strip(), subject.strip(),
                      [line for line in body.split("\n") if line.strip()]))
    return stale


def check(root):
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}",
               what="resolve the checked-out head").strip()
    fetch_base(root)
    base = git(root, "rev-parse", "--verify", f"refs/remotes/origin/{BASE_BRANCH}^{{commit}}",
               what=f"resolve origin/{BASE_BRANCH}").strip()
    paths = gate_paths(root)
    stale = stale_commits(root, head, base, paths)
    if not stale:
        # "reads", not "reads" with no limit attached. The merge-marker step
        # reads EVERY tracked file through the `.` spelling, and this derivation
        # resolves that to nothing, so the count below includes the defining workflow and static imports,
        # as well as the files a required check names — not every file one touches. A green line is the one
        # sentence a maintainer reads on this check, and a sentence that
        # over-claims here is the same defect as an over-claiming report: it
        # looks like the set is complete.
        print(f"This head carries every commit on {BASE_BRANCH} that touches "
              f"the {len(paths)} gate-defining file(s) derived BY NAME.")
        print("  A step reading every tracked file — the `.` spelling, which the "
              "gates\n  job's merge-marker step uses — is a named reach "
              "limit and is not\n  counted above; see the module docstring.")
        return 0
    plural = "s" if len(stale) != 1 else ""
    print(f"{BASE_BRANCH} holds {len(stale)} commit{plural} this head does not:")
    for sha, subject, touched in stale:
        print(f"  {sha} {subject}")
        if not touched:
            # `git log --name-only` reports no file for a merge commit, and the
            # commits it brought in are in the list as themselves. Saying so is
            # the whole point: a header claiming each entry names a file, over
            # an entry that names none, is a sentence the reader cannot check.
            print("    (a merge commit, which git names no file for; the "
                  "commits it brought in are listed as themselves)")
            continue
        for path in touched:
            print(f"    {path}")
    print(f"Rebase onto {BASE_BRANCH} and push again, so this run's checks read "
          f"the files {BASE_BRANCH} reads.")
    return 1


def main(argv):
    # Three parents, not two: this script sits at scripts/ci/, so two parents
    # land on scripts/, and stale_commits() compares with pathspecs —
    # `git log <head>..<base> ... -- <paths>` — which git resolves against the
    # directory `git -C` chdirs into. A root at scripts/ matches none of the
    # full-tree paths in the derived set, empties the commit listing, and
    # prints the green line over a stale head. Both gate scripts therefore
    # default to the repository root.
    root = Path(__file__).resolve().parent.parent.parent
    print_paths = False
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == "--print-paths":
            print_paths = True
        elif argument == "--root":
            if not rest:
                print("usage: gate_base_freshness.py [--root DIR] [--print-paths]",
                      file=sys.stderr)
                return 2
            root = Path(rest.pop(0))
        else:
            print("usage: gate_base_freshness.py [--root DIR] [--print-paths]",
                  file=sys.stderr)
            return 2
    try:
        if print_paths:
            for path in gate_paths(root):
                print(path)
            return 0
        return check(root)
    except GateError as refusal:
        print(f"head freshness: {refusal}", file=sys.stderr)
        return 1
    except OSError as failure:
        # git could not be run at all — absent from the runner image, or an
        # argument list past what the kernel will exec. Named rather than
        # traced: a refusal is what this run owes the reader, and the exit
        # status is the same either way.
        print(f"head freshness: cannot run git: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
