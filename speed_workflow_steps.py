"""Running a workflow step's `run:` body the way the job runs it.

Speed.yml's defects on this branch were all one path resolved against the
wrong root, and a text assertion cannot fail on any of them. So the controls
that catch them have to EXECUTE a step: with the step's environment merged
and its `working-directory` applied as the cwd, against a synthetic tree.

These helpers are shared by the two test modules that need that, and are not
a test module themselves — `unittest discover` collects `test_*.py` only.
"""
import os
import subprocess
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent / ".github" / "workflows"


class StepRunner:
    """Turns a named step out of a workflow into a command you can run."""

    def job_env(self):
        """`jobs.<id>.env`, the mappings every step inherits."""
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        job = lines.index("  speed:")
        start = next(i for i in range(job, len(lines))
                     if lines[i] == "    env:")
        return self._env_map(lines[start + 1:], "      ")

    def step_text(self, step_name):
        """One named step's YAML, from its name to the next step."""
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        start = text.index(f"      - name: {step_name}")
        end = text.index("\n      - ", start + 10)
        return text[start:end]

    def step_run(self, step_name):
        """One named step as the JOB runs it: (env, working-directory, body).

        A step's `env:` is merged into its environment and its
        `working-directory` is its cwd; a test that executes the body without
        both is executing a different command from the one that ships.
        """
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        start = lines.index(f"      - name: {step_name}")
        block = []
        for line in lines[start:]:
            if line.startswith("      - ") and line is not lines[start]:
                break
            block.append(line)
        env, working_dir = self._step_env(block)
        run_line = next(i for i, line in enumerate(block)
                        if line.startswith("        run: |"))
        body = []
        for line in block[run_line + 1:]:
            if line.startswith("          "):
                body.append(line[10:])
            elif not line.strip():
                body.append("")
            else:
                break
        return env, working_dir, "\n".join(body) + "\n"

    def env_for(self, declared, root, **extra):
        """A step's environment against the synthetic tree.

        REPO and REPORTS are `${{ }}` expressions the runner expands; here
        they stand for the tree's own paths, which is what makes these tests
        about the PATHS rather than about the shell.
        """
        return {**os.environ,
                **{k: v for k, v in declared.items() if "${{" not in v},
                "REPO": str(root / "head"),
                "REPORTS": str(root / "reports"),
                "RUNNER_TEMP": str(root),
                "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
                "GITHUB_OUTPUT": str(root / "output.txt"),
                "GITHUB_ENV": str(root / "env.txt"),
                "ALLOWED_WORKLOAD_REMOVALS": "",
                **extra}

    @staticmethod
    def bash(block, root, working_dir, env):
        """Run a step body under `bash -e`, with `working-directory` as cwd.

        A step with no declared working-directory runs in the workspace, which
        is what GitHub does — and what makes removing that key from a step
        fail the test that executes it.
        """
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", block],
            cwd=root / working_dir if working_dir else root, env=env,
            capture_output=True, text=True, check=False)

    def _step_env(self, block):
        """The environment a step runs with: job env, then step env."""
        env = dict(self.job_env())
        working_dir = None
        step_env = []
        inside = False
        for line in block:
            # Checked first: an `env:` block may run right up to it, and the
            # loop below breaks out of that block on the first line outside.
            if line.startswith("        working-directory:"):
                working_dir = line.split(":", 1)[1].strip()
                continue
            if line.startswith("        env:"):
                inside = True
                step_env = []
                continue
            if inside and (line.startswith("          ")
                           or line.strip().startswith("#")
                           or not line.strip()):
                step_env.append(line)
                continue
            if inside and line.strip():
                break
        env.update(self._env_map(step_env, "          "))
        return env, working_dir

    @staticmethod
    def _env_map(lines, indent):
        """`KEY: value` pairs under an `env:` block."""
        env = {}
        for line in lines:
            text = line.strip()
            if text.startswith("#"):
                continue          # comments may be indented or not
            if not text:
                continue          # a blank line separates prose, not keys
            if not line.startswith(indent) or ":" not in text:
                break
            key, _, value = text.partition(":")
            env[key.strip()] = value.strip().strip("\"'")
        return env
