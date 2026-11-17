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
        Path("f.txt").write_text("x\n")
        g("add", "f.txt")
        g("commit", "-q", "-m", "base")
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
        code, out, _ = self.run_stack("restack", "--from", "3")  # slice 3 has no branch
        self.assertEqual(code, 0)
        self.assertIn("restack complete", out)
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
            "position": {"next_branch": None, "applied": []},
        })
        # A second run must not overwrite the recorded original SHAs.
        g("commit", "-q", "--allow-empty", "-m", "moved")
        g("branch", "-f", "slice/feat-2", "HEAD")
        self.run_stack("restack", "--from", "3")  # state exists: reused, not re-captured
        again = json.loads(self.state.read_text())
        self.assertEqual(again["branches"]["2"]["orig_sha"], self.sha[2])

    def test_safety_refusals_exit_nonzero_with_remediation(self):  # B13/B17 refusals
        git_dir = Path(".git")
        cases = {  # name: (make unsafe, undo, remediation text)
            "dirty tree": (lambda: Path("f.txt").write_text("changed\n"),
                           lambda: g("checkout", "f.txt"), "git stash -u"),
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
            exempt = name in ("dirty tree", "unfinished rebase")
            for cmd in stack.MUTATING:
                if exempt and cmd in stack.RESUMING:
                    continue  # covered by test_resuming_commands_exempt_...
                with self.subTest(case=name, cmd=cmd):
                    code, _, err = self.run_stack(cmd)
                    self.assertEqual(code, 1)
                    self.assertIn(remediation, err)
                    self.assertFalse(self.state.exists())
            undo()

    def test_resuming_commands_exempt_from_dirty_and_rebase_refusals(self):  # amendment (a)
        Path("f.txt").write_text("changed\n")  # dirty tree
        (Path(".git") / "rebase-merge").mkdir()  # open rebase (no head-name: HEAD is main)
        for cmd in stack.RESUMING:
            with self.subTest(cmd=cmd):
                code, _, err = self.run_stack(cmd)
                self.assertEqual(code, 1)
                self.assertIn("no restack in progress", err)  # past safety, no state file
        (Path(".git") / "index.lock").touch()  # but the lock still refuses them
        for cmd in stack.RESUMING:
            with self.subTest(cmd=cmd, case="lock"):
                self.assertIn("remove the lock", self.run_stack(cmd)[2])

    def test_untracked_files_never_block(self):  # amendment (b)
        Path(".specs").mkdir()
        Path(".stack-conflict").mkdir()
        Path("conflict-report.md").write_text("x")
        code, _, _ = self.run_stack("restack", "--dry-run")
        self.assertEqual(code, 0)

    def test_dry_run_prints_plan_and_mutates_nothing(self):
        before = g("for-each-ref"), g("rev-parse", "HEAD")
        expected = {
            "restack": "rebase slice/feat-2 onto slice/feat-1",
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


CHAIN = """# Plan: feat

Base branch: main

### Slice 1: one
- depends-on: []

### Slice 2: two
- depends-on: [1]
- card: |
    Goal: edit the gamma line.
    Acceptance: Behaviors B13, B14.

### Slice 3: three
- depends-on: [2]
"""


def commit(name, text, msg):
    Path(name).write_text(text)
    g("add", name)
    g("commit", "-q", "-m", msg)


class LifecycleTest(unittest.TestCase):
    """restack / continue / abort on a 3-slice chain; slice 1 later gets a fixup."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plan = root / "03-plan.md"
        self.plan.write_text(CHAIN)
        self.marker = root / "tests-ran"
        repo = root / "repo"
        repo.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        g("init", "-q", "-b", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            g("config", k, v)  # stack.py's own git calls need an identity
        commit("a.txt", "alpha\nbeta\ngamma\n", "base")
        g("switch", "-q", "-c", "slice/feat-1")
        commit("a.txt", "alpha-1\nbeta\ngamma\n", "slice 1")
        g("switch", "-q", "-c", "slice/feat-2")
        commit("a.txt", "alpha-1\nbeta\ngamma-2\n", "slice 2: edit gamma")
        g("switch", "-q", "-c", "slice/feat-3")
        commit("b.txt", "b\n", "slice 3")
        g("switch", "-q", "slice/feat-1")
        self.orig = {n: g("rev-parse", f"slice/feat-{n}") for n in (1, 2, 3)}
        self.state = Path(".git/stack-state.json")
        # The slice's build+tests: a fake Makefile; untracked, so never blocks anything.
        Path("Makefile").write_text(f"test:\n\t@echo ran >> {self.marker}\n")
        patcher = mock.patch("stack.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    run_stack = StackTest.run_stack

    def fixup(self):  # new commit on slice 1 (checked out) after slice 2 forked from it
        commit("c.txt", "c\n", "fixup")

    def ancestors_ok(self):  # each branch sits directly on the previous one
        for n in (2, 3):
            self.assertEqual(g("rev-parse", f"slice/feat-{n - 1}"),
                             g("merge-base", f"slice/feat-{n - 1}", f"slice/feat-{n}"))

    def conflict(self):
        """main moves under the stack (edits gamma, like slice 2): slice 1 rebases
        cleanly, then slice 2 collides with the new commit underneath it."""
        g("switch", "-q", "main")
        commit("a.txt", "alpha\nbeta\ngamma-main\n", "main moves gamma")
        code, _, err = self.run_stack("restack", "--from", "1")
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT", err)

    def test_restack_clean_replays_k_to_n_in_order(self):  # B13 clean path
        self.fixup()  # slice 1 gains c.txt; no overlap with slice 2/3
        tip1 = g("rev-parse", "slice/feat-1")
        code, out, _ = self.run_stack("restack", "--from", "2")
        self.assertEqual(code, 0)
        self.assertLess(out.index("rebased slice/feat-2"), out.index("rebased slice/feat-3"))
        self.assertEqual(g("rev-parse", "slice/feat-1"), tip1)  # before --from: untouched
        self.assertNotEqual(g("rev-parse", "slice/feat-2"), self.orig[2])
        self.ancestors_ok()
        self.assertEqual(g("log", "--format=%s", "-3", "slice/feat-3").splitlines(),
                         ["slice 3", "slice 2: edit gamma", "fixup"])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["position"], {"next_branch": None,
                                             "applied": ["slice/feat-2", "slice/feat-3"]})
        self.assertEqual(g("status", "--porcelain", "--untracked-files=no"), "")
        code, _, err = self.run_stack("restack", "--from", "2")  # state kept for push/abort
        self.assertIn("already recorded", err)

    def test_conflict_stops_with_seven_section_report_and_state(self):  # B13/B14
        self.conflict()
        self.assertTrue(Path(".git/rebase-merge").exists())  # left in progress
        pos = json.loads(self.state.read_text())["position"]
        self.assertEqual(pos, {"next_branch": "slice/feat-2", "applied": ["slice/feat-1"]})
        report = (self.plan.parent / "conflict-report.md").read_text()
        for n, title in enumerate(("Position", "Incoming intent", "Underneath intent",
                                   "Conflicted files", "Hunk classification",
                                   "Blast radius", "Decision rules"), 1):
            self.assertRegex(report, rf"(?m)^## {n}\. {title}")
        self.assertIn("branch: slice/feat-2", report)
        self.assertIn("slice 2: edit gamma", report)  # commit subject
        self.assertIn("parent branch: slice/feat-1", report)
        self.assertIn("### Slice 2: two", report)  # the card, in full
        self.assertIn("B13, B14", report)
        self.assertIn("main moves gamma", report.split("## 3.")[1].split("## 4.")[0])  # underneath
        self.assertIn("0 identical, 0 changed, 1 not yet applied", report.split("## 6.")[1])
        self.assertIn("likely semantic", report)
        art = self.plan.parent / ".stack-conflict" / "2"
        self.assertEqual(sorted(p.name for p in art.iterdir()),
                         ["1-base-a.txt", "1-ours-a.txt", "1-theirs-a.txt"])
        self.assertEqual((art / "1-ours-a.txt").read_text(), "alpha-1\nbeta\ngamma-main\n")
        self.assertIn("python3", report.split("## 7.")[1])
        self.assertIn(" continue ", report.split("## 7.")[1])

    def test_continue_runs_tests_then_proceeds(self):  # B15
        self.conflict()
        Path("a.txt").write_text("alpha-1\nbeta\nresolved\n")
        g("add", "a.txt")
        code, out, _ = self.run_stack("continue")
        self.assertEqual(code, 0)
        self.assertIn("tests green: make test", out)
        self.assertEqual(self.marker.read_text(), "ran\n")  # the discovered command ran
        self.assertIn("rebased slice/feat-3", out)  # proceeded to the next branch
        self.ancestors_ok()
        self.assertEqual(g("show", "slice/feat-2:a.txt"), "alpha-1\nbeta\nresolved")
        self.assertFalse(Path(".git/rebase-merge").exists())
        pos = json.loads(self.state.read_text())["position"]
        self.assertEqual(pos["applied"], ["slice/feat-1", "slice/feat-2", "slice/feat-3"])

    def test_continue_refuses_unresolved_and_red_tests(self):  # B15
        self.conflict()
        code, _, err = self.run_stack("continue")  # still unmerged
        self.assertEqual(code, 1)
        self.assertIn("unresolved conflicts", err)
        Path("a.txt").write_text("alpha-1\nbeta\nresolved\n")
        g("add", "a.txt")
        Path("Makefile").write_text("test:\n\t@exit 3\n")
        code, _, err = self.run_stack("continue")
        self.assertEqual(code, 1)
        self.assertIn("VERIFICATION FAILED", err)
        self.assertTrue(Path(".git/rebase-merge").exists())  # nothing proceeded
        self.assertIn("Verification failed", (self.plan.parent / "conflict-report.md").read_text())
        self.assertEqual(json.loads(self.state.read_text())["position"]["applied"],
                         ["slice/feat-1"])  # slice 2 not applied

    def test_continue_and_abort_dry_run_mid_conflict_change_nothing(self):
        self.conflict()
        before = g("for-each-ref"), g("status", "--porcelain")
        for cmd, text in (("continue", "continue the restack at slice/feat-2"),
                          ("abort", f"restore slice/feat-2 to {self.orig[2]}")):
            code, out, _ = self.run_stack(cmd, "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn(text, out)
        self.assertEqual((g("for-each-ref"), g("status", "--porcelain")), before)
        self.assertTrue(Path(".git/rebase-merge").exists())

    def test_abort_restores_every_orig_sha(self):  # B16
        self.conflict()  # slice 1 is already rebased when slice 2 stops
        self.assertNotEqual(g("rev-parse", "slice/feat-1"), self.orig[1])
        code, out, _ = self.run_stack("abort")
        self.assertEqual(code, 0)
        self.assertEqual({n: g("rev-parse", f"slice/feat-{n}") for n in (1, 2, 3)}, self.orig)
        for branch in ("slice/feat-1", "slice/feat-2", "slice/feat-3"):
            self.assertIn(branch, out)  # the restore list
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(".git/rebase-merge").exists())
        self.assertFalse((self.plan.parent / "conflict-report.md").exists())
        self.assertFalse((self.plan.parent / ".stack-conflict").exists())
        self.assertEqual(g("status", "--porcelain", "--untracked-files=no"), "")

    def test_abort_after_completed_restack_unwinds_it(self):  # B16
        self.fixup()
        self.run_stack("restack", "--from", "2")
        self.assertEqual(self.run_stack("abort")[0], 0)
        self.assertEqual({n: g("rev-parse", f"slice/feat-{n}") for n in (2, 3)},
                         {2: self.orig[2], 3: self.orig[3]})


class PushTest(unittest.TestCase):
    """push on the 3-slice chain with a bare origin; slice 1 gets a fixup, then restack."""

    run_stack = StackTest.run_stack
    fixup = LifecycleTest.fixup
    conflict = LifecycleTest.conflict

    def setUp(self):
        LifecycleTest.setUp(self)
        self.origin = self.plan.parent / "origin.git"
        g("init", "-q", "--bare", str(self.origin))
        g("remote", "add", "origin", str(self.origin))
        g("push", "-q", "origin", "main", "slice/feat-1", "slice/feat-2", "slice/feat-3")

    def remote(self, n):
        return g("--git-dir", str(self.origin), "rev-parse", f"refs/heads/slice/feat-{n}")

    def local(self, n):
        return g("rev-parse", f"slice/feat-{n}")

    def restacked(self):  # slice 1 gains a commit, slices 2-3 follow it
        self.fixup()
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)

    def gh_stub(self, replies):
        """PATH with a gh script answering `gh pr view <branch>` from replies."""
        d = self.plan.parent / "bin"
        d.mkdir()
        cases = "".join(f"{b}) echo '{j}';; " for b, j in replies.items())
        (d / "gh").write_text(f'#!/bin/sh\ncase "$3" in {cases}*) exit 1;; esac\n')
        (d / "gh").chmod(0o755)
        return (mock.patch("stack.shutil.which", return_value=str(d / "gh")),
                mock.patch.dict(os.environ, {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"}))

    def test_push_after_restack_moves_origin_tips_and_records_pushed(self):  # B17
        self.restacked()
        self.assertNotEqual(self.remote(2), self.local(2))
        code, out, _ = self.run_stack("push", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], [self.local(n) for n in (1, 2, 3)])
        self.assertLess(out.index("cost report"), out.index("pushed slice/feat-1"))
        self.assertIn("branches restacked: 2..3", out)
        self.assertIn("push complete: 3 pushed, 0 skipped", out)
        pos = json.loads(self.state.read_text())["position"]
        self.assertEqual(pos["pushed"], ["slice/feat-1", "slice/feat-2", "slice/feat-3"])
        code, out, _ = self.run_stack("push", "--yes")  # idempotent re-run
        self.assertEqual((code, out.count("already pushed")), (0, 3))

    def test_cost_report_counts_changed_patches(self):  # B17
        self.conflict()
        Path("a.txt").write_text("alpha-1\nbeta\nresolved\n")
        g("add", "a.txt")
        self.assertEqual(self.run_stack("continue")[0], 0)
        code, out, _ = self.run_stack("push", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("slice/feat-2: 1 patch(es) changed", out)  # resolved by hand
        self.assertIn("slice/feat-3: 0 patch(es) changed", out)  # replayed identically
        self.assertIn("reviewers of moved heads must re-review", out)

    def test_unchanged_tip_is_skipped(self):  # B17
        self.fixup()
        g("push", "-q", "origin", "slice/feat-1")  # slice 1 is already published, unrestacked
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        before = self.remote(1)
        code, out, _ = self.run_stack("push", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("skip slice/feat-1: tip equals remote", out)
        self.assertIn("push complete: 2 pushed, 1 skipped", out)
        self.assertEqual(self.remote(1), before)
        self.assertEqual((self.remote(2), self.remote(3)), (self.local(2), self.local(3)))

    def test_merged_pr_is_skipped_via_gh_stub(self):  # B17
        self.restacked()
        before = self.remote(2)
        replies = {"slice/feat-2": '{"number":7,"state":"MERGED"}',
                   "slice/feat-3": '{"number":8,"state":"OPEN"}'}
        with contextlib.ExitStack() as st:
            for p in self.gh_stub(replies):
                st.enter_context(p)
            code, out, _ = self.run_stack("push", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("skip slice/feat-2: PR merged", out)
        self.assertIn("PRs whose heads will move: #8 (slice/feat-3)", out)
        self.assertEqual(self.remote(2), before)
        self.assertEqual((self.remote(1), self.remote(3)), (self.local(1), self.local(3)))

    def test_lease_rejection_aborts_and_abort_spares_pushed_branches(self):  # B17
        self.restacked()
        theirs = self.orig[3]  # somebody pushes slice 2 behind our back
        g("--git-dir", str(self.origin), "update-ref", "refs/heads/slice/feat-2", theirs)
        code, out, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("concurrent", err)
        self.assertIn("Nothing after slice/feat-2 was pushed", err)
        self.assertEqual((self.remote(1), self.remote(2), self.remote(3)),
                         (self.local(1), theirs, self.orig[3]))  # theirs kept, 3 untouched
        self.assertEqual(json.loads(self.state.read_text())["position"]["pushed"],
                         ["slice/feat-1"])
        pushed_tip = self.local(1)
        self.assertEqual(self.run_stack("abort")[0], 0)  # pushed branch is not moved back
        self.assertEqual(self.local(1), pushed_tip)
        self.assertEqual((self.local(2), self.local(3)), (self.orig[2], self.orig[3]))

    def test_dry_run_prints_plan_and_mutates_nothing(self):  # B17
        self.restacked()
        before = g("for-each-ref"), g("--git-dir", str(self.origin), "for-each-ref")
        code, out, _ = self.run_stack("push", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"push slice/feat-2 --force-with-lease=refs/heads/slice/feat-2:{self.orig[2]}", out)
        self.assertIn("cost report", out)
        self.assertNotIn("pushed", json.loads(self.state.read_text())["position"])
        self.assertEqual((g("for-each-ref"), g("--git-dir", str(self.origin), "for-each-ref")), before)

    def test_cost_report_must_be_accepted_unless_yes(self):  # B17
        self.restacked()
        before = self.remote(2)
        with mock.patch("builtins.input", return_value="n"):
            code, out, err = self.run_stack("push")
        self.assertEqual(code, 1)
        self.assertIn("cost report", out)
        self.assertIn("declined", err)
        self.assertEqual(self.remote(2), before)
        with mock.patch("builtins.input", return_value="y"):
            self.assertEqual(self.run_stack("push")[0], 0)
        self.assertEqual(self.remote(2), self.local(2))

    def test_push_needs_a_restack_or_from(self):  # B17
        code, _, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no completed restack", err)
        self.assertFalse(self.state.exists())
        self.fixup()
        code, out, _ = self.run_stack("push", "--from", "2", "--yes")  # one step
        self.assertEqual(code, 0)
        self.assertIn("rebased slice/feat-2", out)
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], [self.local(n) for n in (1, 2, 3)])
        self.assertIn("already recorded", self.run_stack("push", "--from", "2")[2])
