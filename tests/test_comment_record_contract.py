"""Pair the inline record validator with the pinned action's actual decoder.

The fixture is the unmodified comment_baseline.py from the adopted action
version. Only its two decoder functions are
compiled, avoiding unrelated measurement imports and network dependencies.
"""
import ast
import hashlib
import json
import subprocess
import unittest
from pathlib import Path, PurePosixPath
from typing import Any

from tests import test_ci_workflows as workflow_tests
from tests import _ghw_temp

ROOT = Path(__file__).resolve().parents[1]


def consumer_decoder():
    raw = (ROOT / "tests/fixtures/comment-ratchet-v1-policy.txt").read_bytes()
    # Pinned full upstream blob, so these are the consumer's functions rather
    # than a second implementation of what we think the consumer accepts.
    expected = "e27326173ee64b027774bae639e6156bd0c80fd00c8c1331cacc01295ce7cec7"
    if hashlib.sha256(raw).hexdigest() != expected:
        raise AssertionError("the pinned consumer fixture changed")
    nodes: list[ast.stmt] = [
        node for node in ast.parse(raw).body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"relative_path", "decode_records"}]
    if len(nodes) != 2:
        raise AssertionError("the pinned consumer decoder functions are absent")
    namespace: dict[str, Any] = {"json": json, "PurePosixPath": PurePosixPath}
    # Execute the hash-verified upstream functions, not a hand-written oracle.
    # pylint: disable-next=exec-used
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "tests/fixtures/comment-ratchet-v1-policy.txt"), "exec"),
         namespace)
    return namespace["decode_records"]


class TestCommentRecordContract(unittest.TestCase):
    def test_git_checkout_preserves_reference_bytes_with_autocrlf(self):
        with _ghw_temp.tempdir(prefix="reference checkout ") as tmp:
            root = Path(tmp)
            relative = "tests/fixtures/comment-ratchet-v1-policy.txt"
            result = subprocess.run(
                ["git", "-c", "core.autocrlf=true", "checkout-index",
                 "--prefix=" + root.as_posix() + "/", "--", relative],
                cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            raw = (root / relative).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),
                             "e27326173ee64b027774bae639e6156bd0c80fd00c8c1331cacc01295ce7cec7")
            self.assertNotIn(b"\r\n", raw)

    def test_inline_and_pinned_consumer_agree_on_canonical_document_bytes(self):
        decoder = consumer_decoder()
        case = workflow_tests.TestRatchetPushWorkflow()
        case.setUpClass()
        with _ghw_temp.tempdir() as tmp:
            data, repo = Path(tmp) / "data", Path(tmp) / "repo"
            data.mkdir()
            (repo / ".github").mkdir(parents=True)
            (repo / ".github/comment-share.json").write_bytes(b"{}\n")
            (data / "base.txt").write_bytes(b"a" * 40 + b"\n")
            valid_paths = ("new.py", "a/b.py", ".", "C:/file.py", " a.py", "a\u0085b.py")
            invalid_paths = ("", "/file.py", "//host/file.py", "../outside.py",
                             "a/../b.py", "a//b.py", "./a.py", "a/./b.py", "a.py/",
                             "a\\b.py", "a\x00b.py", "a\nb.py", "a\rb.py", "a\tb.py",
                             "a\x1fb.py", "a\x7fb.py")
            documents = [({}, True), *[({path: [1, 2]}, True) for path in valid_paths],
                         *[({path: [1, 2]}, False) for path in invalid_paths]]
            documents.extend(({"new.py": pair}, valid) for pair, valid in (
                ([0, 1], True), ([0, 0], False), ([1, 0], False),
                ([-1, 3], False), ([True, 3], False), ([1.0, 3], False),
                ([4, 3], False), ([1, 3, 5], False)))
            for document, accepted in documents:
                with self.subTest(document=document):
                    raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
                    (data / "comment-share.json").write_bytes(raw)
                    # These extract and execute the shipped inline block.
                    # pylint: disable-next=protected-access
                    result = case._run_validator(case._validator(), data, repo,
                                                 "comment-share.json")
                    if accepted:
                        self.assertEqual(decoder(raw), document)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        with self.assertRaises(ValueError):
                            decoder(raw)
                        self.assertEqual(result.returncode, 1, result.stderr)


if __name__ == "__main__":
    unittest.main()
