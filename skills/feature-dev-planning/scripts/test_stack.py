"""Tests for stack.py: temp git repos, gh-absent path only."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack  # noqa: E402

PLAN = """# Plan: feat

Base branch: main

### Slice 1: one
- depends-on: []

### Slice 3: three (declared before 2: order must still be numeric)
- depends-on: [1]

### Slice 2: two
- depends-on: [1]
"""


def g(*args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *args],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


class StackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plan = root / "03-plan.md"  # outside the repo: keeps the tree clean
        self.plan.write_text(PLAN)
        repo = root / "repo"
        repo.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        g("init", "-q", "-b", "main")
        g("commit", "-q", "--allow-empty", "-m", "base")
        self.sha = {}
        for n in (1, 2):  # slice 3 has no branch yet
            g("switch", "-q", "-c", f"slice/feat-{n}")
            g("commit", "-q", "--allow-empty", "-m", f"slice {n}")
            self.sha[n] = g("rev-parse", "HEAD")
        g("update-ref", "refs/remotes/origin/slice/feat-1", self.sha[1])  # 1 is pushed
        g("switch", "-q", "main")
        self.state = repo / ".git" / "stack-state.json"
        patcher = mock.patch("stack.shutil.which", return_value=None)  # gh absent
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_stack(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = stack.main([*argv[:1], str(self.plan), *argv[1:]])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_status_lists_branches_in_stack_order(self):  # B12
        code, out, _ = self.run_stack("status")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["stack feat on main: 1 -> 2 -> 3",
                                     "gh absent — order from plan only"])
        s1 = self.sha[1][:7]
        self.assertEqual(lines[2], f"1  slice/feat-1  local {s1}  remote {s1}")
        self.assertEqual(lines[3], f"2  slice/feat-2  local {self.sha[2][:7]}  not pushed")
        self.assertEqual(lines[4], "3  slice/feat-3  local missing  not pushed")

    def test_forward_dependency_is_refused(self):
        self.plan.write_text(PLAN.replace("depends-on: []", "depends-on: [2]"))
        code, _, err = self.run_stack("status")
        self.assertEqual(code, 1)
        self.assertIn("edges must point to earlier", err)

    def test_mutating_command_writes_state_with_captured_shas(self):
        code, _, err = self.run_stack("restack", "--from", "2")
        self.assertEqual(code, 1)  # replay itself is not implemented yet
        self.assertIn("not implemented yet", err)
        state = json.loads(self.state.read_text())
        self.assertEqual(state.pop("created_at")[:2], "20")
        self.assertEqual(state, {
            "plan_path": str(self.plan),
            "base": "main",
            "order": [1, 2, 3],
            "branches": {
                "1": {"orig_sha": self.sha[1], "remote_sha": self.sha[1]},
                "2": {"orig_sha": self.sha[2], "remote_sha": None},
                "3": {"orig_sha": None, "remote_sha": None},
            },
            "position": {"next_branch": "slice/feat-2", "applied": []},
        })
        # A second run must not overwrite the recorded original SHAs.
        g("commit", "-q", "--allow-empty", "-m", "moved")
        g("branch", "-f", "slice/feat-2", "HEAD")
        self.run_stack("push")
        again = json.loads(self.state.read_text())
        self.assertEqual(again["branches"]["2"]["orig_sha"], self.sha[2])

    def test_safety_refusals_exit_nonzero_with_remediation(self):  # B13/B17 refusals
        git_dir = Path(".git")
        cases = {  # name: (make unsafe, undo, remediation text)
            "dirty tree": (lambda: Path("junk").write_text("x"),
                           lambda: Path("junk").unlink(), "git stash -u"),
            "index.lock": (lambda: (git_dir / "index.lock").touch(),
                           lambda: (git_dir / "index.lock").unlink(), "remove the lock: rm "),
            "unfinished rebase": (lambda: (git_dir / "rebase-merge").mkdir(),
                                  lambda: (git_dir / "rebase-merge").rmdir(),
                                  "git rebase --abort"),
            "foreign HEAD": (lambda: g("switch", "-q", "-c", "other"),
                             lambda: g("switch", "-q", "main"), "git switch main"),
        }
        for name, (make, undo, remediation) in cases.items():
            make()
            for cmd in stack.MUTATING:
                with self.subTest(case=name, cmd=cmd):
                    code, _, err = self.run_stack(cmd)
                    self.assertEqual(code, 1)
                    self.assertIn(remediation, err)
                    self.assertFalse(self.state.exists())
            undo()

    def test_dry_run_prints_plan_and_mutates_nothing(self):
        before = g("for-each-ref"), g("rev-parse", "HEAD")
        expected = {
            "restack": "rebase slice/feat-2 onto slice/feat-1",
            "continue": "continue the restack at slice/feat-1",
            "abort": f"restore slice/feat-2 to {self.sha[2]}",
            "push": f"push slice/feat-1 --force-with-lease=refs/heads/slice/feat-1:{self.sha[1]}",
        }
        for cmd, action in expected.items():
            with self.subTest(cmd=cmd):
                code, out, _ = self.run_stack(cmd, "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn(action, out)
                self.assertIn("cost: ", out)
                self.assertFalse(self.state.exists())
                self.assertEqual((g("for-each-ref"), g("rev-parse", "HEAD")), before)
                self.assertEqual(g("status", "--porcelain"), "")
        _, out, _ = self.run_stack("restack", "--dry-run", "--from", "3")
        self.assertNotIn("rebase slice/feat-2", out)
        self.assertIn("rebase slice/feat-3 onto slice/feat-2", out)
