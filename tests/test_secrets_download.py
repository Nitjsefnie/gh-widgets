"""The gitleaks download retries a failed attempt inside the job's time limit."""
import re
import textwrap
import unittest

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests.test_ci_workflows import WORKFLOWS, _run_download_with_failed_digest


class TestGitleaksDownloadRetry(unittest.TestCase):
    @REQUIRES_POSIX_SHELL
    def test_the_download_retries_within_the_job_time_limit(self):
        job = yaml.safe_load((WORKFLOWS / "secrets.yml").read_text(
            encoding="utf-8"))["jobs"]["gitleaks"]
        script, = [step["run"] for step in job["steps"] if step.get("name")
                   == "Download gitleaks and verify its digest"]
        args = _run_download_with_failed_digest(
            textwrap.dedent(script).strip())[1].splitlines()

        def value(flag):
            self.assertEqual(args.count(flag), 1, (flag, args))
            return int(args[args.index(flag) + 1])
        retries, attempt, delay = (
            value("--retry"), value("--max-time"), value("--retry-delay"))
        self.assertGreaterEqual(retries, 1, "a failed download must be retried")
        self.assertIn("--retry-all-errors", args,
                      "a dropped connection is retried only with this flag")
        self.assertEqual(
            [arg for arg in args
             if arg == "--silent" or re.fullmatch(r"-[^-]*s[^-]*", arg)], [],
            "silent mode hides the warning curl prints for each retry")
        self.assertLess((retries + 1) * attempt + retries * delay,
                        job["timeout-minutes"] * 60)
