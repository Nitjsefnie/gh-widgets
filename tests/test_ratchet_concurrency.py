"""An automated ratchet commit never cancels a run of the commit it follows.

Every workflow a push triggers is checked, including ones added later: its
concurrency groups are evaluated for a ratchet push and for the pushes around
it. A shared group lets the ratchet run cancel an in-progress run (when
cancel-in-progress is on) or evict a pending one (the default queue keeps
only the newest pending run), so the ratchet run's group must be its own.
"""
import functools
import re
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
RATCHET_SUFFIX = (
    "${{ github.event.head_commit.committer.email == "
    f"'{BOT_EMAIL}' && format('-ratchet-{{0}}', github.run_id) || '' }}}}")
TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+)"
                   r"|(?P<op>==|!=|&&|\|\||[!(),])"
                   r"|(?P<name>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*))")


def _tokens(text):
    out, pos = [], 0
    while text[pos:].strip():
        match = TOKEN.match(text, pos)
        if not match:
            raise ValueError(f"unsupported expression syntax: {text[pos:]!r}")
        kind = match.lastgroup or ""
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


def _lookup(context, path):
    if path in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[path]
    value = context
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return left == right


def evaluate(text, context):
    """Evaluate the GitHub expression subset these workflows use."""
    tokens = _tokens(text)

    def peek():
        return tokens[0][1] if tokens else None

    def primary():
        kind, value = tokens.pop(0)
        if kind == "str":
            return value[1:-1].replace("''", "'")
        if kind == "num":
            return int(value)
        if value == "!":
            return not primary()
        if value == "(":
            result = disjunction()
            if tokens.pop(0)[1] != ")":
                raise ValueError("unbalanced parenthesis")
            return result
        if kind == "name" and peek() == "(":
            if value != "format":
                raise ValueError(f"unsupported function {value}")
            tokens.pop(0)
            args = [disjunction()]
            while (separator := tokens.pop(0)[1]) == ",":
                args.append(disjunction())
            if separator != ")":
                raise ValueError("unclosed format() call")
            return re.sub(r"\{(\d+)\}",
                          lambda m: str(args[int(m.group(1)) + 1]), args[0])
        if kind != "name":
            raise ValueError(f"unexpected token {value!r}")
        return _lookup(context, value)

    def comparison():
        left = primary()
        while peek() in {"==", "!="}:
            negate = tokens.pop(0)[1] == "!="
            left = _equal(left, primary()) != negate
        return left

    def conjunction():
        left = comparison()
        while peek() == "&&":
            tokens.pop(0)
            right = comparison()
            left = right if left else left
        return left

    def disjunction():
        left = conjunction()
        while peek() == "||":
            tokens.pop(0)
            right = conjunction()
            left = left if left else right
        return left

    result = disjunction()
    if tokens:
        raise ValueError(f"trailing tokens {tokens!r}")
    return result


def render(value, context):
    """Render a scalar that may embed ``${{ }}`` interpolations."""
    if not isinstance(value, str):
        return value
    whole = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", value, re.S)
    if whole and "${{" not in whole.group(1):
        return evaluate(whole.group(1), context)
    return re.sub(r"\$\{\{(.*?)\}\}",
                  lambda m: str(evaluate(m.group(1), context)), value,
                  flags=re.S)


def push(run_id, email, username=None, ref="refs/heads/main"):
    committer = {"email": email, "name": username or "github-actions[bot]"}
    if username:
        committer["username"] = username
    return {"github": {
        "event_name": "push", "ref": ref, "ref_name": ref.split("/")[-1],
        "ref_type": "branch", "run_id": run_id, "sha": f"sha-{run_id}",
        "actor": "Nitjsefnie", "triggering_actor": "Nitjsefnie",
        "event": {"head_commit": {"committer": committer},
                  "repository": {"default_branch": "main"}}}}


# The push actor is the account in both cases (a deploy-key push), so only
# the commit's committer tells a ratchet commit apart. The ratchet contexts
# carry no committer username: the payload does not promise one.
HUMAN = push("101", "75166987+Nitjsefnie@users.noreply.github.com",
             "Nitjsefnie")
RATCHET = push("102", BOT_EMAIL)
NEXT_RATCHET = push("103", BOT_EMAIL)
NO_HEAD_COMMIT = {"github": dict(HUMAN["github"], run_id="104",
                                 event={"head_commit": None})}


def _rule_sections(text):
    """Parse only the top-level keys the rule reads; a full parse of every
    workflow costs the speed gate's unit-suite count millions of statements.
    A job-level concurrency key (none today) falls back to the full parse."""
    if re.search(r"^[ \t]+[\"']?concurrency[\"']?[ \t]*:", text, re.M):
        return yaml.safe_load(text)
    return yaml.safe_load("".join(
        chunk for chunk in re.split(r"(?m)^(?=[^\s#])", text)
        if re.match(r"[\"']?(on|concurrency)[\"']?:", chunk))) or {}


@functools.cache
def push_workflows():
    found = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = _rule_sections(path.read_text(encoding="utf-8"))
        trigger = workflow.get("on", workflow.get(True))
        names = [trigger] if isinstance(trigger, str) else list(trigger or ())
        if "push" in names:
            found[path.name] = workflow
    return found


def groups(workflow):
    blocks = [("workflow", workflow.get("concurrency"))] + [
        (f"job {name}", job.get("concurrency"))
        for name, job in workflow.get("jobs", {}).items()]
    for where, block in blocks:
        if block is not None:
            yield where, block["group"] if isinstance(block, dict) else block


class TestRatchetPushesHaveTheirOwnGroup(unittest.TestCase):
    def test_every_push_workflow_is_enumerated(self):
        self.assertLessEqual(
            {"tests.yml", "secrets.yml", "size-baseline.yml",
             "comment-share.yml", "coverage-ratchet.yml", "release.yml"},
            set(push_workflows()))

    def test_a_ratchet_run_shares_no_group_with_any_other_push_run(self):
        checked = 0
        for name, workflow in push_workflows().items():
            for where, group in groups(workflow):
                with self.subTest(workflow=name, block=where):
                    ratchet = render(group, RATCHET)
                    others = {render(group, ctx) for ctx in
                              (HUMAN, NEXT_RATCHET, NO_HEAD_COMMIT)}
                    self.assertTrue(ratchet)
                    self.assertNotIn(ratchet, others)
                    checked += 1
        self.assertGreaterEqual(checked, 6)

    def test_other_pushes_keep_their_workflow_group(self):
        expected = {"secrets.yml": "secrets-refs/heads/main",
                    "coverage-ratchet.yml":
                        "coverage-ratchet-refs/heads/main",
                    "release.yml": "release"}
        workflows = push_workflows()
        for name, group in expected.items():
            for context in (HUMAN, NO_HEAD_COMMIT):
                with self.subTest(workflow=name, run=context["github"]["run_id"]):
                    self.assertEqual(
                        render(workflows[name]["concurrency"]["group"],
                               context), group)

    def test_the_suffix_is_the_shared_spelling(self):
        for name in ("secrets.yml", "coverage-ratchet.yml", "release.yml"):
            with self.subTest(workflow=name):
                self.assertTrue(push_workflows()[name]["concurrency"]["group"]
                                .endswith(RATCHET_SUFFIX))

    def test_the_evaluator_follows_github_operator_semantics(self):
        ctx = {"github": {"a": "X", "n": None}}
        self.assertEqual(evaluate("github.a == 'x' && 'y' || 'z'", ctx), "y")
        self.assertEqual(evaluate("github.n && 'y' || 'z'", ctx), "z")
        self.assertIs(evaluate("github.n != 'x'", ctx), True)
        self.assertEqual(evaluate("format('{0}-{1}', 'a', 2)", ctx), "a-2")
        self.assertEqual(render("p-${{ github.a }}", ctx), "p-X")
        for bad in ("contains(github.a, 'x')", "github.a[0]", "github.a ==",
                    "format('{0}', 'a' 'b'"):
            with self.subTest(expression=bad), self.assertRaises(
                    (ValueError, IndexError)):
                evaluate(bad, ctx)


if __name__ == "__main__":
    unittest.main()
