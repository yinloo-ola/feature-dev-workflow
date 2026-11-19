"""Tests for stack.py: temp git repos, gh-absent path only."""
import contextlib
import io
import json
import os
import re
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
deliver: stacked

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
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO()):  # EOF, never the terminal
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

    def test_self_dependency_is_refused(self):  # slice 17 item 9
        self.plan.write_text(PLAN.replace("### Slice 2: two\n- depends-on: [1]",
                                          "### Slice 2: two\n- depends-on: [2]"))
        code, _, err = self.run_stack("status")
        self.assertEqual(code, 1)
        self.assertIn("slice 2 depends-on [2]", err)
        self.assertIn("edges must point to earlier", err)

    def test_trailing_comment_digits_are_not_dependencies(self):
        self.plan.write_text(PLAN.replace("### Slice 2: two\n- depends-on: [1]",
                                          "### Slice 2: two\n- depends-on: [1]  # was 3"))
        code, out, err = self.run_stack("status")
        self.assertEqual(code, 0, err)
        self.assertIn("stack feat on main: 1 -> 2 -> 3", out)

    def test_comment_with_a_valid_earlier_slice_is_ignored(self):
        old = ("### Slice 3: three (declared before 2: order must still be numeric)\n"
               "- depends-on: [1]")
        self.plan.write_text(PLAN.replace(old, old + "  # see 1"))
        code, out, err = self.run_stack("status")
        self.assertEqual(code, 0, err)
        self.assertIn("stack feat on main: 1 -> 2 -> 3", out)

    def test_non_bracketed_dependency_is_refused(self):
        self.plan.write_text(PLAN.replace("### Slice 2: two\n- depends-on: [1]",
                                          "### Slice 2: two\n- depends-on: 1"))
        code, _, err = self.run_stack("status")
        self.assertEqual(code, 1)
        self.assertIn("depends-on must be a bracketed list, got '1'", err)

    def test_mutating_command_writes_state_with_captured_shas(self):
        code, out, _ = self.run_stack("restack", "--from", "3")  # slice 3 has no branch
        self.assertEqual(code, 0)
        self.assertIn("restack complete", out)
        state = json.loads(self.state.read_text())
        self.assertEqual(state.pop("created_at")[:2], "20")
        self.assertEqual(state, {
            "plan_path": str(self.plan),
            "feature": "feat",
            "base": "main",
            "deliver": "stacked",
            "order": [1, 2, 3],
            "branches": {
                "1": {"orig_sha": self.sha[1], "remote_sha": self.sha[1]},
                "2": {"orig_sha": self.sha[2], "remote_sha": None},
                "3": {"orig_sha": None, "remote_sha": None},
            },
            "position": {"next_branch": None, "applied": []},
            "start_branch": "main",  # item 8: HEAD when the first mutation began
            "start_worktree": str(Path.cwd()),
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
deliver: stacked

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


    def main_moves(self):  # non-conflicting commit on main: a restack from 1 changes every branch
        g("switch", "-q", "main")
        commit("m.txt", "m\n", "main moves")

    def tips(self):
        return {n: g("rev-parse", f"slice/feat-{n}") for n in (1, 2, 3)}

    def continued(self):  # a conflict resolved by hand and finished by continue
        self.conflict()
        self.resolve("alpha-1\nbeta\nresolved\n")
        self.assertEqual(self.run_stack("continue")[0], 0)
        self.assertNotEqual(self.tips()[2], self.orig[2])

    def test_continue_records_new_sha_so_abort_restores_without_force(self):  # slice 17 item 4
        self.continued()
        branches = json.loads(self.state.read_text())["branches"]
        self.assertEqual({n: branches[str(n)]["new_sha"] for n in (1, 2, 3)}, self.tips())
        code, _, err = self.run_stack("abort")  # no --force
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.tips(), self.orig)

    def test_commit_made_after_continue_makes_abort_refuse_without_force(self):  # slice 17 item 4
        self.continued()
        restacked = self.tips()
        g("switch", "-q", "slice/feat-2")
        commit("p.txt", "p\n", "PRECIOUS")
        precious = g("rev-parse", "HEAD")
        code, _, err = self.run_stack("abort")
        self.assertEqual(code, 1)
        self.assertIn(f"slice/feat-2 has commits not in the recorded restack ({precious})", err)
        self.assertEqual(self.tips(), {**restacked, 2: precious})
        self.assertEqual(self.run_stack("abort", "--force")[0], 0)
        self.assertEqual(self.tips(), self.orig)

    def test_restack_falls_back_to_plain_merge_base_without_a_reflog_fork_point(self):  # item 5a
        self.main_moves()
        m0 = g("rev-parse", "main~1")  # the commit the stack was built on
        g("rebase", "-q", "main", "slice/feat-1")  # slice 1 moves; slice 2 still sits on its old tip
        g("switch", "-q", "main")
        g("reflog", "expire", "--expire=now", "--all")  # no fork point can be found now
        self.assertIsNone(stack.git("merge-base", "--fork-point", "slice/feat-1", "slice/feat-2"))
        code, out, err = self.run_stack("restack", "--from", "2")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("rebased slice/feat-2 onto slice/feat-1", out)
        self.assertEqual(json.loads(self.state.read_text())["branches"]["2"]["base_old"], m0)
        self.assertEqual(g("log", "--format=%s", "slice/feat-1..slice/feat-2"),
                         "slice 2: edit gamma")
        self.assertEqual(g("show", "slice/feat-2:a.txt"), "alpha-1\nbeta\ngamma-2")

    def test_state_lives_in_git_common_dir_shared_by_worktrees(self):  # slice 12 item 1
        repo, wt = Path.cwd(), self.plan.parent / "wt"
        g("worktree", "add", "-q", str(wt), "slice/feat-3")
        os.chdir(wt)
        self.assertEqual(self.run_stack("restack", "--from", "3")[0], 0)
        os.chdir(repo)
        g("-C", str(wt), "switch", "-q", "--detach")  # free slice/feat-3 for abort
        self.assertEqual(json.loads(self.state.read_text())["feature"], "feat")
        self.assertFalse((repo / ".git" / "worktrees" / "wt" / "stack-state.json").exists())
        self.assertEqual(self.run_stack("abort")[0], 0)  # the main checkout sees it
        self.assertFalse(self.state.exists())

    def test_state_of_another_feature_is_refused_and_its_branches_untouched(self):  # item 1
        for n in (1, 2, 3):
            g("branch", f"slice/other-{n}", self.orig[n])
        self.fixup()
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        g("switch", "-q", "main")
        feat_plan = self.plan
        self.plan = feat_plan.parent / "other-plan.md"
        self.plan.write_text(CHAIN.replace("feat", "other"))
        before = g("for-each-ref")
        for cmd in ("abort", "continue", "push"):
            with self.subTest(cmd=cmd):
                code, _, err = self.run_stack(cmd)
                self.assertEqual(code, 1)
                self.assertIn("state belongs to feature feat; run stack.py with that plan, "
                              f"or remove {self.state.resolve()} to discard", err)
                self.assertEqual(g("for-each-ref"), before)
                self.assertTrue(self.state.exists())

    def test_unreadable_state_is_refused_without_a_traceback(self):  # item 1
        self.state.write_text("{not json")
        for cmd in ("continue", "abort", "push"):
            with self.subTest(cmd=cmd):
                code, _, err = self.run_stack(cmd)
                self.assertEqual(code, 1)
                self.assertIn(f"state file {self.state.resolve()} is unreadable (", err)
                self.assertIn(") - remove it to reset", err)

    def test_abort_refuses_commits_made_after_the_restack_unless_forced(self):  # item 2
        self.main_moves()
        self.assertEqual(self.run_stack("restack", "--from", "1")[0], 0)
        restacked = self.tips()
        self.assertTrue(all(restacked[n] != self.orig[n] for n in (1, 2, 3)))
        g("switch", "-q", "slice/feat-2")
        commit("p.txt", "p\n", "PRECIOUS")
        precious = g("rev-parse", "HEAD")
        code, _, err = self.run_stack("abort")
        self.assertEqual(code, 1)
        self.assertIn(f"slice/feat-2 has commits not in the recorded restack ({precious}); "
                      "abort would discard them - re-run with --force to restore anyway "
                      "(the tip stays in the reflog)", err)
        self.assertEqual(self.tips(), {**restacked, 2: precious})  # nothing moved
        self.assertTrue(self.state.exists())
        code, out, _ = self.run_stack("abort", "--force")
        self.assertEqual(code, 0)
        self.assertIn(precious, out)  # the discarded tip is printed
        self.assertEqual(self.tips(), self.orig)
        self.assertEqual(g("cat-file", "-t", precious), "commit")  # still in the object store

    def test_abort_refuses_when_a_branch_is_dirty_on_the_current_worktree(self):  # item 4
        self.fixup()
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        g("switch", "-q", "slice/feat-3")  # restack returns HEAD to slice/feat-1 (item 8)
        restacked = self.tips()
        Path("b.txt").write_text("edited\n")
        code, _, err = self.run_stack("abort")
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes on checked-out slice/feat-3 - commit or stash first", err)
        self.assertEqual(Path("b.txt").read_text(), "edited\n")
        self.assertEqual(self.tips(), restacked)
        self.assertTrue(self.state.exists())

    def test_abort_on_a_clean_checked_out_branch_resets_the_tree(self):  # item 4
        self.fixup()
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        g("switch", "-q", "slice/feat-3")  # restack returns HEAD to slice/feat-1 (item 8)
        state = json.loads(self.state.read_text())
        del state["start_branch"]  # a state recorded before slice 14: HEAD is left where it is
        self.state.write_text(json.dumps(state))
        self.assertTrue(Path("c.txt").exists())  # slice 3 carries slice 1's fixup
        self.assertEqual(self.run_stack("abort")[0], 0)
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-3")
        self.assertEqual(self.tips()[3], self.orig[3])
        self.assertFalse(Path("c.txt").exists())
        self.assertEqual(g("status", "--porcelain", "--untracked-files=no"), "")

    def test_branch_checked_out_in_another_worktree_refuses_restack_and_abort(self):  # item 4
        wt = self.plan.parent / "wt"
        g("worktree", "add", "-q", str(wt), "slice/feat-2")
        before = g("for-each-ref")
        code, _, err = self.run_stack("restack", "--from", "1")
        self.assertEqual(code, 1)
        self.assertIn(f"branch slice/feat-2 is checked out at {wt} - switch that worktree "
                      "off it, then re-run", err)
        self.assertFalse(self.state.exists())
        self.assertEqual(g("for-each-ref"), before)
        g("worktree", "remove", "--force", str(wt))  # now restack, then pin 2 elsewhere
        self.fixup()
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        g("worktree", "add", "-q", str(wt), "slice/feat-2")
        restacked = self.tips()
        code, _, err = self.run_stack("abort")
        self.assertEqual(code, 1)
        self.assertIn("branch slice/feat-2 is checked out at", err)
        self.assertEqual(self.tips(), restacked)
        self.assertTrue(self.state.exists())

    def test_from_must_name_a_slice_in_the_plan(self):  # item 7
        for cmd in ("restack", "push"):
            for bad in ("0", "9"):
                with self.subTest(cmd=cmd, bad=bad):
                    code, _, err = self.run_stack(cmd, "--from", bad)
                    self.assertEqual(code, 1)
                    self.assertIn(f"--from {bad}: no such slice (plan has 1, 2, 3)", err)
                    self.assertFalse(self.state.exists())
                    self.assertEqual(self.tips(), self.orig)

    # --- slice 14: continue checks, HEAD, artifacts, non-ASCII, state creation (items 6-13) ---

    def resolve(self, text):
        Path("a.txt").write_text(text)
        g("add", "a.txt")

    def test_continue_refuses_staged_or_unstaged_conflict_markers(self):  # item 6
        self.conflict()
        marked = "alpha-1\nbeta\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n"
        for staged in (True, False):
            with self.subTest(staged=staged):
                self.resolve("alpha-1\nbeta\nresolved\n")
                Path("a.txt").write_text(marked)  # worktree differs from the index
                if staged:
                    g("add", "a.txt")
                code, _, err = self.run_stack("continue")  # Makefile is green
                self.assertEqual(code, 1)
                self.assertIn("conflict markers remain in a.txt", err)
                self.assertTrue(Path(".git/rebase-merge").exists())
                self.assertEqual(self.marker.exists(), False)  # refused before running tests

    def test_continue_accepts_trailing_whitespace_in_the_resolution(self):  # item 6
        self.conflict()
        self.resolve("alpha-1\nbeta\nresolved   \n")
        code, out, _ = self.run_stack("continue")
        self.assertEqual(code, 0)
        self.assertIn("rebased slice/feat-3", out)

    def test_marker_scan_matches_only_real_markers_on_added_lines(self):  # item 6
        diff = lambda *lines: "+++ b/f.txt\n" + "".join(f"+{x}\n" for x in lines)  # noqa: E731
        for line, hit in (("<<<<<<< HEAD", True), ("=======", False), (">>>>>>> theirs", True),
                          ("<<<<<<<", True), ("<<<<<<<<", False), ("<<<<<< six", False),
                          ("a ======= b", False), ("======== eight", False)):
            with self.subTest(line=line):
                self.assertEqual(stack.marker_files(diff(line)), {"f.txt"} if hit else set())
        self.assertEqual(stack.marker_files("+++ b/f.txt\n-=======\n ======= ctx\n"), set())

    def test_continue_accepts_a_setext_heading_but_refuses_real_markers(self):  # slice 17 item 1
        self.conflict()
        refused = {"full trio": "alpha-1\nbeta\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n",
                   "only <<<<<<<": "alpha-1\nbeta\n<<<<<<< x\nresolved\n",
                   "only >>>>>>>": "alpha-1\nbeta\nresolved\n>>>>>>> y\n"}
        for name, text in refused.items():
            with self.subTest(name):
                self.resolve(text)
                code, _, err = self.run_stack("continue")
                self.assertEqual(code, 1)
                self.assertIn("conflict markers remain in a.txt", err)
                self.assertFalse(self.marker.exists())  # refused before running tests
        self.resolve("alpha-1\nbeta\nTitle\n=======\n")  # a resolved README: setext heading
        code, out, _ = self.run_stack("continue")
        self.assertEqual(code, 0)
        self.assertIn("tests green: make test", out)

    def test_continue_refuses_when_no_test_command_is_found(self):  # item 7
        self.conflict()
        self.resolve("alpha-1\nbeta\nresolved\n")
        Path("Makefile").unlink()
        code, _, err = self.run_stack("continue")
        self.assertEqual(code, 1)
        self.assertIn("no test command found - pass --test-cmd", err)

    def test_continue_test_cmd_flag_wins_over_discovery(self):  # item 7
        self.conflict()
        self.resolve("alpha-1\nbeta\nresolved\n")
        Path("Makefile").write_text("test:\n\t@exit 3\n")  # discovery would fail
        code, out, _ = self.run_stack("continue", "--test-cmd", f"echo flag > {self.marker}")
        self.assertEqual(code, 0)
        self.assertIn(f"tests green: echo flag > {self.marker}", out)
        self.assertEqual(self.marker.read_text(), "flag\n")

    def test_head_returns_to_the_start_branch(self):  # item 8
        self.fixup()
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)  # clean run
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-1")
        self.assertEqual(self.run_stack("abort")[0], 0)
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-1")
        self.main_moves()
        self.assertEqual(self.run_stack("restack", "--from", "1")[0], 0)
        g("switch", "-q", "slice/feat-2")  # wander off, then abort from elsewhere
        self.assertEqual(self.run_stack("abort")[0], 0)
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "main")

    def test_head_returns_after_the_final_continue(self):  # item 8
        self.conflict()  # started on main; HEAD is detached mid-rebase
        self.resolve("alpha-1\nbeta\nresolved\n")
        self.assertEqual(self.run_stack("continue")[0], 0)
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "main")

    def test_restore_head_needs_the_starting_worktree_and_a_clean_tree(self):  # item 8
        state = {"start_branch": "slice/feat-2", "start_worktree": str(Path.cwd())}
        stack.restore_head({**state, "start_worktree": str(self.plan.parent)})  # another worktree
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-1")
        Path("n.txt").write_text("n\n")
        g("add", "n.txt")  # a staged new file: git would carry it across a switch
        stack.restore_head(state)  # dirty tree
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-1")
        g("rm", "-q", "--cached", "n.txt")  # untracked files never count
        stack.restore_head(state)
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-2")

    def test_final_continue_deletes_conflict_artifacts(self):  # item 9
        self.conflict()
        self.assertTrue((self.plan.parent / "conflict-report.md").exists())
        self.resolve("alpha-1\nbeta\nresolved\n")
        self.assertEqual(self.run_stack("continue")[0], 0)
        self.assertFalse((self.plan.parent / "conflict-report.md").exists())
        self.assertFalse((self.plan.parent / ".stack-conflict").exists())

    def test_conflict_on_a_non_ascii_filename_is_named_in_the_brief(self):  # item 11
        g("switch", "-q", "main")
        for n in (1, 2, 3):
            g("branch", "-q", "-D", f"slice/feat-{n}")
        commit("\u00e9t\u00e9.txt", "alpha\nbeta\ngamma\n", "base file")
        g("switch", "-q", "-c", "slice/feat-1")
        commit("\u00e9t\u00e9.txt", "alpha-1\nbeta\ngamma\n", "slice 1")
        g("switch", "-q", "-c", "slice/feat-2")
        commit("\u00e9t\u00e9.txt", "alpha-1\nbeta\ngamma-2\n", "slice 2")
        g("switch", "-q", "main")
        commit("\u00e9t\u00e9.txt", "alpha\nbeta\ngamma-main\n", "main moves gamma")
        code, _, err = self.run_stack("restack", "--from", "1")
        self.assertEqual((code, "CONFLICT" in err), (1, True))
        report = (self.plan.parent / "conflict-report.md").read_text()
        self.assertIn("### \u00e9t\u00e9.txt", report)
        self.assertIn("\u00e9t\u00e9.txt base lines", report)  # hunk classification found it
        art = self.plan.parent / ".stack-conflict" / "2"
        self.assertIn("1-ours-\u00e9t\u00e9.txt", [p.name for p in art.iterdir()])

    def test_state_creation_is_atomic_when_a_concurrent_restack_wins(self):  # item 12
        before = g("for-each-ref")
        rival = lambda *a: self.state.write_text("{}")  # noqa: E731  runs after the exists() check
        with mock.patch("stack.refuse_diverged", side_effect=rival):
            code, _, err = self.run_stack("restack", "--from", "2")
        self.assertEqual(code, 1)
        self.assertIn("a restack is already recorded in", err)
        self.assertEqual(self.state.read_text(), "{}")  # the rival's file survives
        self.assertEqual(g("for-each-ref"), before)

    def test_abort_dry_run_previews_the_lost_commits_refusal(self):  # item 13
        self.main_moves()
        self.assertEqual(self.run_stack("restack", "--from", "1")[0], 0)
        g("switch", "-q", "slice/feat-2")
        commit("p.txt", "p\n", "PRECIOUS")
        precious = g("rev-parse", "HEAD")
        before = g("for-each-ref"), g("status", "--porcelain"), self.state.read_text()
        code, out, err = self.run_stack("abort", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn(f"slice/feat-2 has commits not in the recorded restack ({precious}); "
                      "abort would discard them - re-run with --force to restore anyway", err)
        self.assertEqual((g("for-each-ref"), g("status", "--porcelain"), self.state.read_text()), before)
        self.assertEqual(self.run_stack("abort", "--dry-run", "--force")[0], 0)  # --force previews it


    # --- slice 18: modify/delete brief (item 3) and a second conflicting commit (item 5) ---

    def rebuild(self, name, text):
        """Replace the chain with a fresh one on main: `name` holds `text` in the base."""
        g("switch", "-q", "main")
        for n in (1, 2, 3):
            g("branch", "-q", "-D", f"slice/feat-{n}")
        commit(name, text, "base file")
        g("switch", "-q", "-c", "slice/feat-1")

    def test_s18_item3_modify_delete_conflict_writes_the_decision_brief(self):  # r3s_report_moddel_and
        self.rebuild("d.txt", "1\n2\n3\n")
        g("rm", "-q", "d.txt")  # the slice deletes a file ...
        g("commit", "-q", "-m", "slice 1 deletes d")
        g("switch", "-q", "-c", "slice/feat-2")
        g("commit", "-q", "--allow-empty", "-m", "slice 2")
        g("switch", "-q", "main")
        commit("d.txt", "1\n2\n3\n4\n", "main modifies d")  # ... that main modified
        code, _, err = self.run_stack("restack", "--from", "1")
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT: restack stopped at slice 1", err)
        report = (self.plan.parent / "conflict-report.md").read_text()
        self.assertIn("- d.txt: modify/delete \u2014 decision required",
                      report.split("## 5.")[1].split("## 6.")[0])
        self.assertIn("(absent: theirs)", report)

    def test_s18_item5_second_conflicting_commit_refreshes_the_brief(self):  # r3s_cont_multi_conflict
        lines = lambda **kw: "".join(f"{kw.get(f'l{i}', f'l{i}')}\n" for i in range(1, 10))  # noqa: E731
        self.rebuild("d.txt", lines())
        commit("d.txt", lines(l1="l1-s1"), "slice 1")
        g("switch", "-q", "-c", "slice/feat-2")
        commit("d.txt", lines(l1="l1-s1", l5="l5-a"), "slice 2: A edits l5")
        commit("d.txt", lines(l1="l1-s1", l5="l5-a", l8="l8-b"), "slice 2: B edits l8")
        g("switch", "-q", "main")
        commit("d.txt", lines(l5="l5-main", l8="l8-main"), "main edits both")
        self.assertEqual(self.run_stack("restack", "--from", "1")[0], 1)
        report = lambda: (self.plan.parent / "conflict-report.md").read_text()  # noqa: E731
        position = lambda: [x for x in report().splitlines() if x.startswith("- commit being applied")][0]  # noqa: E731,E501
        self.assertIn("slice 2: A edits l5", position())
        Path("d.txt").write_text(lines(l1="l1-s1", l5="l5-res", l8="l8-main"))
        g("add", "d.txt")
        code, _, err = self.run_stack("continue")
        self.assertEqual(code, 1)  # the next commit conflicts too: stopped, not carried on
        self.assertIn("CONFLICT: restack stopped at slice 2", err)
        self.assertIn("slice 2: B edits l8", position())  # refreshed, not the stale first brief
        self.assertTrue(Path(".git/rebase-merge").exists())
        state = json.loads(self.state.read_text())["position"]
        self.assertEqual(state, {"next_branch": "slice/feat-2", "applied": ["slice/feat-1"]})
        Path("d.txt").write_text(lines(l1="l1-s1", l5="l5-res", l8="l8-res"))
        g("add", "d.txt")
        self.assertEqual(self.run_stack("continue")[0], 0)  # and the second resolution finishes it
        self.assertFalse((self.plan.parent / "conflict-report.md").exists())


class OldBaseTest(unittest.TestCase):
    """Slices 2 and 3 were built on slice 1; slice 1 is then amended."""

    run_stack = StackTest.run_stack
    ancestors_ok = LifecycleTest.ancestors_ok

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plan = root / "03-plan.md"
        self.plan.write_text(CHAIN)
        repo = root / "repo"
        repo.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        g("init", "-q", "-b", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            g("config", k, v)
        commit("f.txt", "x\n", "base")
        for n in (1, 2, 3):
            g("switch", "-q", "-c", f"slice/feat-{n}")
            commit(f"s{n}.txt", f"{n}\n", f"slice {n}")
        g("switch", "-q", "slice/feat-1")
        self.old1 = g("rev-parse", "HEAD")
        self.state = Path(".git/stack-state.json")
        patcher = mock.patch("stack.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_restack_after_amending_slice_1_replays_only_slice_2_and_3(self):  # item 5 (a)
        Path("s1.txt").write_text("one-amended\n")
        g("commit", "-q", "-a", "--amend", "-m", "slice 1")
        code, out, err = self.run_stack("restack", "--from", "2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(g("show", "slice/feat-2:s1.txt"), "one-amended")
        self.ancestors_ok()
        self.assertEqual(g("log", "--format=%s", "-3", "slice/feat-3").splitlines(),
                         ["slice 3", "slice 2", "slice 1"])
        self.assertEqual(json.loads(self.state.read_text())["branches"]["2"]["base_old"],
                         self.old1)

    def test_dry_run_prints_each_base_old(self):  # item 5
        Path("s1.txt").write_text("one-amended\n")
        g("commit", "-q", "-a", "--amend", "-m", "slice 1")
        code, out, _ = self.run_stack("restack", "--from", "2", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"rebase slice/feat-2 onto slice/feat-1 (base_old {self.old1[:7]})", out)
        self.assertIn(f"rebase slice/feat-3 onto slice/feat-2 (base_old {g('rev-parse', 'slice/feat-2')[:7]})", out)

    def test_restack_after_slice_1_swaps_a_file_for_another(self):  # item 5 (b)
        g("rm", "-q", "s1.txt")
        commit("s1new.txt", "new\n", "slice 1")  # commit below is the amend
        g("reset", "-q", "--soft", "HEAD~1")
        g("commit", "-q", "--amend", "-m", "slice 1")
        code, _, err = self.run_stack("restack", "--from", "2")
        self.assertEqual((code, err), (0, ""))
        files = g("ls-tree", "-r", "--name-only", "slice/feat-3").splitlines()
        self.assertNotIn("s1.txt", files)
        self.assertIn("s1new.txt", files)

    def test_cost_report_uses_the_recorded_base_old_so_slice_2_shows_no_change(self):  # item 5b
        Path("s1.txt").write_text("one-amended\n")
        g("commit", "-q", "-a", "--amend", "-m", "slice 1")
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        code, out, _ = self.run_stack("push", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("slice/feat-2: 0 patch(es) changed", out)  # a replay of the same patch
        self.assertIn("slice/feat-3: 0 patch(es) changed", out)

    def test_cost_report_counts_a_dropped_commit_as_one_changed_patch(self):  # slice 17 item 7
        g("switch", "-q", "slice/feat-2")
        commit("s2b.txt", "2b\n", "slice 2b")  # slice 2 now carries two commits
        g("switch", "-q", "slice/feat-1")
        Path("s2.txt").write_text("2\n")  # slice 1 absorbs slice 2's first commit verbatim
        g("add", "s2.txt")
        g("commit", "-q", "--amend", "--no-edit")
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        self.assertEqual(g("log", "--format=%s", "slice/feat-1..slice/feat-2"), "slice 2b")
        code, out, _ = self.run_stack("push", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("slice/feat-2: 1 patch(es) changed", out)  # dropped, no replacement

class PushTest(unittest.TestCase):
    """push on the 3-slice chain with a bare origin; slice 1 gets a fixup, then restack."""

    run_stack = StackTest.run_stack
    fixup = LifecycleTest.fixup
    conflict = LifecycleTest.conflict
    tips = LifecycleTest.tips

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

    def gh_bin(self, body):
        """A directory holding a gh script with this shell body."""
        d = self.plan.parent / "bin"
        d.mkdir(exist_ok=True)
        (d / "gh").write_text(f"#!/bin/sh\n{body}\n")
        (d / "gh").chmod(0o755)
        return d

    def gh_patches(self, d):
        return (mock.patch("stack.shutil.which", return_value=str(d / "gh")),
                mock.patch.dict(os.environ, {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"}))

    def gh_stub(self, replies):
        """PATH with a gh script answering `gh pr view <branch>` from replies."""
        cases = "".join(f"{b}) echo '{j}';; " for b, j in replies.items())
        return self.gh_patches(self.gh_bin(
            f'case "$3" in {cases}*) echo "no pull requests found" >&2; exit 1;; esac'))

    def theirs(self, n):
        """A teammate pushes a new commit on top of slice n's original tip to origin."""
        sha = g("--git-dir", str(self.origin), "commit-tree", f"{self.orig[n]}^{{tree}}",
                "-p", self.orig[n], "-m", "teammate")
        g("--git-dir", str(self.origin), "update-ref", f"refs/heads/slice/feat-{n}", sha)
        return sha

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

    def test_abort_after_a_successful_push_leaves_published_branches(self):  # slice 12 item 3
        self.restacked()
        self.assertEqual(self.run_stack("push", "--yes")[0], 0)
        published = self.tips()
        self.assertEqual(self.run_stack("abort")[0], 0)
        self.assertEqual(self.tips(), published)  # origin has them: abort must not rewind
        self.assertNotEqual(published[2], self.orig[2])

    def test_restack_refuses_when_origin_has_commits_the_local_branch_lacks(self):  # item 6
        theirs = g("--git-dir", str(self.origin), "commit-tree", f"{self.orig[2]}^{{tree}}",
                   "-p", self.orig[2], "-m", "teammate commit")
        g("--git-dir", str(self.origin), "update-ref", "refs/heads/slice/feat-2", theirs)
        g("fetch", "-q", "origin")
        before = g("for-each-ref")
        code, _, err = self.run_stack("restack", "--from", "2")
        self.assertEqual(code, 1)
        self.assertIn("local slice/feat-2 lacks origin commits (", err)
        self.assertIn("teammate commit", err)
        self.assertIn("integrate them (git rebase origin/slice/feat-2) before restacking", err)
        self.assertFalse(self.state.exists())
        self.assertEqual(g("for-each-ref"), before)

    # --- slice 14: push safety (items 1-5, 8, 10) ---

    def test_push_on_closed_stdin_refuses_without_yes(self):  # item 1 (message is new)
        self.restacked()
        before = [self.remote(n) for n in (1, 2, 3)]
        d = self.gh_bin('echo "no pull requests found" >&2; exit 1')
        r = subprocess.run([sys.executable, stack.__file__, "push", str(self.plan)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to push without confirmation (stdin closed) - pass --yes", r.stderr)
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], before)

    def test_lease_is_the_sha_captured_at_restack_not_the_tracking_ref(self):  # item 2 (pins)
        self.restacked()
        theirs = self.theirs(2)
        g("fetch", "-q", "origin")  # the tracking ref now holds the teammate's commit
        self.assertEqual(g("rev-parse", "refs/remotes/origin/slice/feat-2"), theirs)
        code, _, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.remote(2), theirs)

    def test_rejected_lease_pushes_nothing_atomically(self):  # item 3
        self.restacked()
        theirs = self.theirs(2)
        code, out, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("push rejected - nothing was pushed (atomic). origin changed since the "
                      "restack: inspect with git fetch && git log origin/slice/feat-2, then "
                      "stack.py abort and restack", err)
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], [self.orig[1], theirs, self.orig[3]])
        self.assertNotIn("pushed slice/feat-1", out)
        self.assertNotIn("pushed", json.loads(self.state.read_text())["position"])
        self.assertEqual(self.run_stack("abort")[0], 0)  # nothing published: all restored
        self.assertEqual((self.local(2), self.local(3)), (self.orig[2], self.orig[3]))

    def test_push_is_one_atomic_command_with_one_lease_per_branch(self):  # item 3
        g("push", "-q", "origin", ":slice/feat-3")  # slice 3 is not on origin: null lease
        self.restacked()
        with mock.patch("stack.run", wraps=stack.run) as run:
            code, out, _ = self.run_stack("push", "--yes")
        self.assertEqual(code, 0)
        pushes = [c.args for c in run.call_args_list if c.args[0] == "push"]
        self.assertEqual(pushes, [(
            "push", "--atomic",
            f"--force-with-lease=refs/heads/slice/feat-1:{self.orig[1]}",
            f"--force-with-lease=refs/heads/slice/feat-2:{self.orig[2]}",
            "--force-with-lease=refs/heads/slice/feat-3:",
            "origin", "slice/feat-1", "slice/feat-2", "slice/feat-3")])
        for n in (1, 2, 3):
            self.assertIn(f"pushed slice/feat-{n} -> {self.local(n)[:7]}", out)
        pos = json.loads(self.state.read_text())["position"]
        self.assertEqual(pos["pushed"], ["slice/feat-1", "slice/feat-2", "slice/feat-3"])

    def test_null_lease_rejects_when_the_branch_appeared_on_origin(self):  # item 3
        g("push", "-q", "origin", ":slice/feat-3")
        self.restacked()
        g("--git-dir", str(self.origin), "update-ref", "refs/heads/slice/feat-3", self.orig[3])
        code, _, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("nothing was pushed (atomic)", err)
        self.assertEqual([self.remote(1), self.remote(3)], [self.orig[1], self.orig[3]])

    def test_cost_report_lists_the_exact_push_set(self):  # item 4
        g("push", "-q", "origin", ":slice/feat-3")  # slice 3 unpublished
        g("switch", "-q", "slice/feat-2")
        commit("wip.txt", "wip\n", "local-only WIP")  # not pushed, below --from 3
        g("switch", "-q", "slice/feat-1")
        old2 = self.remote(2)
        code, out, _ = self.run_stack("push", "--from", "3", "--yes")
        self.assertEqual(code, 0)
        self.assertIn(f"slice/feat-2: {old2[:7]} -> {self.local(2)[:7]} (not restacked in this run)", out)
        self.assertIn(f"slice/feat-3: new branch -> {self.local(3)[:7]}", out)
        self.assertNotIn("slice/feat-1: " + self.remote(1)[:7], out)  # tip equals remote: not in the set
        self.assertLess(out.index("slice/feat-3: new branch"), out.index("pushed slice/feat-2"))
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], [self.local(n) for n in (1, 2, 3)])

    def test_dry_run_from_does_not_simulate_the_restack(self):  # item 4
        code, out, _ = self.run_stack("push", "--from", "2", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("restack 2..3 would run first; push set is computed after it", out)
        self.assertNotIn("push slice/feat-2", out)
        self.assertNotIn("push slice/feat-3", out)
        self.assertEqual(out.count("[skip: tip equals remote]"), 1)  # slice 1 only
        self.assertIn("push slice/feat-1", out)  # below --from: untouched by the restack

    def test_push_refuses_while_a_restack_is_unfinished(self):  # item 5 (pins)
        self.restacked()
        state = json.loads(self.state.read_text())
        state["position"]["next_branch"] = "slice/feat-2"
        self.state.write_text(json.dumps(state))
        before = [self.remote(n) for n in (1, 2, 3)]
        code, _, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("restack is unfinished", err)
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], before)

    def test_head_returns_to_the_start_branch_after_push(self):  # item 8
        self.restacked()
        g("switch", "-q", "slice/feat-3")
        self.assertEqual(self.run_stack("push", "--yes")[0], 0)
        self.assertEqual(g("symbolic-ref", "--short", "HEAD"), "slice/feat-1")

    def test_gh_failure_is_loud_once_per_branch_but_no_pr_is_silent(self):  # item 10
        self.restacked()
        with contextlib.ExitStack() as st:
            for p in self.gh_patches(self.gh_bin('echo "authentication required" >&2; exit 1')):
                st.enter_context(p)
            code, _, err = self.run_stack("push", "--yes")
        self.assertEqual(code, 0)
        for n in (1, 2, 3):
            self.assertEqual(err.count("gh: authentication required - merged-PR protection "
                                       f"unavailable for slice/feat-{n}\n"), 1)
        with contextlib.ExitStack() as st:
            for p in self.gh_stub({}):  # "no pull requests found"
                st.enter_context(p)
            self.assertEqual(self.run_stack("push", "--yes")[2], "")


    # --- slice 17: pushed list, report accuracy, status note ---

    main_moves = LifecycleTest.main_moves

    def with_gh(self, replies, *argv):
        with contextlib.ExitStack() as st:
            for p in self.gh_stub(replies):
                st.enter_context(p)
            return self.run_stack(*argv)

    def test_pushed_accumulates_across_pushes_and_abort_restores_none_of_it(self):  # slice 17 item 2
        self.main_moves()
        self.assertEqual(self.run_stack("restack", "--from", "1")[0], 0)
        pushed = lambda: json.loads(self.state.read_text())["position"]["pushed"]  # noqa: E731
        pr3 = lambda state: {"slice/feat-3": f'{{"number":8,"state":"{state}"}}'}  # noqa: E731
        self.assertEqual(self.with_gh(pr3("MERGED"), "push", "--yes")[0], 0)
        self.assertEqual(pushed(), ["slice/feat-1", "slice/feat-2"])
        self.assertEqual(self.remote(3), self.orig[3])  # its PR merged: not sent
        self.assertEqual(self.with_gh(pr3("OPEN"), "push", "--yes")[0], 0)
        self.assertEqual(pushed(), ["slice/feat-1", "slice/feat-2", "slice/feat-3"])
        published = self.tips()
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], [published[n] for n in (1, 2, 3)])
        self.assertEqual(self.run_stack("abort")[0], 0)
        self.assertEqual(self.tips(), published)  # everything was pushed: nothing is restored
        self.assertNotEqual(published[1], self.orig[1])

    def test_open_pr_whose_branch_is_skipped_is_not_listed_as_moving(self):  # slice 17 item 7
        self.fixup()
        g("push", "-q", "origin", "slice/feat-1")  # slice 1: tip equals remote, so skipped
        self.assertEqual(self.run_stack("restack", "--from", "2")[0], 0)
        replies = {f"slice/feat-{n}": f'{{"number":{n + 4},"state":"OPEN"}}' for n in (1, 2)}
        code, out, _ = self.with_gh(replies, "push", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("skip: tip equals remote", out)
        self.assertIn("\n    PRs whose heads will move: #6 (slice/feat-2)\n", out)  # not #5

    def test_status_notes_a_pr_whose_base_is_not_the_stack_parent(self):  # slice 17 item 8
        pr = lambda n, base: (f'{{"number":{n},"headRefName":"x","baseRefName":"{base}",'  # noqa: E731
                              '"state":"OPEN"}')
        code, out, _ = self.with_gh({"slice/feat-1": pr(1, "main"),
                                     "slice/feat-2": pr(2, "main")}, "status")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[1].endswith("PR #1 open"))  # base main == stack parent main
        self.assertTrue(lines[2].endswith("PR #2 open (PR base main, stack parent slice/feat-1)"))
        code, out, _ = self.with_gh({"slice/feat-1": pr(1, "main"),
                                     "slice/feat-2": pr(2, "slice/feat-1")}, "status")
        self.assertEqual(code, 0)
        self.assertNotIn("PR base", out)


    # --- slice 18: push prompt (item 1), abort --dry-run after a push (item 2) ---

    def answer(self, text):
        """`stack.py push` (no --yes) in a real subprocess whose stdin is the text typed."""
        d = self.gh_bin('echo "no pull requests found" >&2; exit 1')
        return subprocess.run([sys.executable, stack.__file__, "push", str(self.plan)],
                              input=text, capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})

    def test_s18_item1_only_y_or_yes_proceeds_anything_else_declines(self):  # r3s_prompt_default_yes
        self.restacked()
        before = [self.remote(n) for n in (1, 2, 3)]
        for text in ("\n", "maybe\n", "n\n"):
            with self.subTest(text=text):
                r = self.answer(text)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("push declined", r.stderr)
                self.assertEqual([self.remote(n) for n in (1, 2, 3)], before)

    def proceeds(self, text):
        self.restacked()
        r = self.answer(text)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([self.remote(n) for n in (1, 2, 3)], [self.local(n) for n in (1, 2, 3)])

    def test_s18_item1_y_proceeds(self):
        self.proceeds("y\n")

    def test_s18_item1_YES_proceeds(self):
        self.proceeds("YES\n")

    def test_s18_item1_padded_yes_proceeds(self):
        self.proceeds(" yes \n")

    def test_s18_item2_abort_dry_run_lists_what_a_real_abort_restores(self):
        self.restacked()
        replies = {"slice/feat-2": '{"number":7,"state":"MERGED"}',
                   "slice/feat-3": '{"number":8,"state":"MERGED"}'}
        self.assertEqual(self.with_gh(replies, "push", "--yes")[0], 0)  # only slice 1 is published
        self.assertEqual(json.loads(self.state.read_text())["position"]["pushed"], ["slice/feat-1"])
        code, out, _ = self.run_stack("abort", "--dry-run")
        self.assertEqual(code, 0)
        dry = dict(re.findall(r"^  restore (\S+) to (\w+)$", out, re.M))
        code, out, _ = self.run_stack("abort")
        self.assertEqual(code, 0)
        real = {b: s for b, s in re.findall(r"^  (\S+) -> (\w+)$", out, re.M)}
        self.assertEqual(sorted(real), ["slice/feat-2", "slice/feat-3"])  # not the published slice 1
        self.assertEqual(sorted(dry), sorted(real))
        self.assertEqual({b: s[:7] for b, s in dry.items()}, real)


HYPHEN = CHAIN.replace("# Plan: feat", "# Plan: slice-execution-v2")


class HyphenatedFeatureTest(unittest.TestCase):
    """Slice 18 item 4: a feature whose own name contains hyphens (slice-execution-v2)."""

    run_stack = StackTest.run_stack

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plan = root / "03-plan.md"
        self.plan.write_text(HYPHEN)
        repo = root / "repo"
        repo.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        g("init", "-q", "-b", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            g("config", k, v)
        commit("a.txt", "alpha\nbeta\ngamma\n", "base")
        for n, (name, text) in enumerate((("a.txt", "alpha-1\nbeta\ngamma\n"),
                                          ("a.txt", "alpha-1\nbeta\ngamma-2\n"),
                                          ("b.txt", "b\n")), 1):
            g("switch", "-q", "-c", f"slice/slice-execution-v2-{n}")
            commit(name, text, f"slice {n}")
        g("switch", "-q", "main")
        commit("a.txt", "alpha\nbeta\ngamma-main\n", "main moves gamma")
        patcher = mock.patch("stack.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dry_run_conflict_continue_and_cost_report_do_not_crash(self):  # r3s_cost_done_split
        code, out, _ = self.run_stack("restack", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("rebase slice/slice-execution-v2-2 onto slice/slice-execution-v2-1", out)
        code, _, err = self.run_stack("restack", "--from", "1")
        self.assertEqual((code, "CONFLICT: restack stopped at slice 2" in err), (1, True))
        self.assertIn("branch: slice/slice-execution-v2-2",
                      (self.plan.parent / "conflict-report.md").read_text())
        Path("a.txt").write_text("alpha-1\nbeta\nresolved\n")
        g("add", "a.txt")
        code, out, _ = self.run_stack("continue", "--test-cmd", "true")  # branch number from the name
        self.assertEqual(code, 0)
        self.assertIn("rebased slice/slice-execution-v2-3", out)
        code, out, _ = self.run_stack("push", "--dry-run")  # cost report parses the applied list
        self.assertEqual(code, 0)
        self.assertIn("branches restacked: 1..3", out)


class ClassifyTest(unittest.TestCase):
    """The conflict classifier and hunk finder, on in-memory file sides."""

    def classes(self, base, ours, theirs):
        return [stack.classify(base, ours, theirs, a, b)
                for a, b in stack.conflict_hunks(base, ours, theirs)]

    def test_classification_table(self):  # slice 17 item 6
        imp = "likely mechanical (both add imports)"
        md = "modify/delete \u2014 decision required"
        cases = [  # name, base, ours, theirs, expected classes
            ("imports on both sides", ["x"], ["import os"], ["import sys"], [imp]),
            ("import on ours, code on theirs", ["x"], ["import os"], ["print(1)"],
             ["likely semantic"]),
            ("code on ours, import on theirs", ["x"], ["print(1)"], ["import os"],
             ["likely semantic"]),
            ("ours deletes, theirs modifies", ["x"], [], ["y"], [md]),
            ("ours modifies, theirs deletes", ["x"], ["y"], [], [md]),
            ("both modify differently", ["x"], ["y"], ["z"], ["likely semantic"]),
        ]
        for name, base, ours, theirs, want in cases:
            with self.subTest(name):
                self.assertEqual(self.classes(base, ours, theirs), want)

    def test_adjacent_hunks_merge_but_one_line_apart_stay_separate(self):  # slice 17 item 6
        base = ["a", "b", "c", "d"]
        edit = lambda i: [x.upper() if k == i else x for k, x in enumerate(base)]  # noqa: E731
        for name, i, j, hunks in (("same line", 1, 1, 1), ("adjacent", 0, 1, 1),
                                  ("adjacent, swapped", 1, 0, 1), ("one line apart", 0, 2, 0),
                                  ("one line apart, swapped", 2, 0, 0)):
            with self.subTest(name):
                self.assertEqual(len(stack.conflict_hunks(base, edit(i), edit(j))), hunks)


class DiscoverTest(unittest.TestCase):
    """discover_test_cmd: pure function of the files present; nothing is executed."""

    def test_discovery_table(self):  # item 7
        mk = ("Makefile", "build:\n\tx\ntest:\n\ty\n")
        pkg = ("package.json", '{"scripts":{"test":"x"}}')
        cases = [
            ("Makefile test target", [mk], None, "make test"),
            ("retest: is not test:", [("Makefile", "retest:\n\tx\n")], None, None),
            ("indented test: is not a target", [("Makefile", " test:\n")], None, None),
            ("package.json with scripts.test", [("package.json", '{"scripts":{"test":"x"}}')],
             None, "npm test"),
            ("package.json without scripts.test", [("package.json", '{"scripts":{"build":"x"}}')],
             None, None),
            ("package.json without scripts", [("package.json", "{}")], None, None),
            ("go.mod", [("go.mod", "module m\n")], None, "go vet ./... && go test ./..."),
            ("Cargo.toml", [("Cargo.toml", "")], None, "cargo test"),
            ("pyproject.toml", [("pyproject.toml", "")], None, "python3 -m pytest -q"),
            ("pytest.ini", [("pytest.ini", "")], None, "python3 -m pytest -q"),
            ("setup.cfg", [("setup.cfg", "")], None, "python3 -m pytest -q"),
            ("tox.ini", [("tox.ini", "")], None, "python3 -m pytest -q"),
            ("nothing", [], None, None),
            ("Makefile beats go.mod", [mk, ("go.mod", "")], None, "make test"),
            ("go.mod beats Cargo.toml", [("go.mod", ""), ("Cargo.toml", "")], None,
             "go vet ./... && go test ./..."),
            ("Cargo.toml beats pyproject", [("Cargo.toml", ""), ("pyproject.toml", "")], None,
             "cargo test"),
            ("Makefile beats package.json", [mk, pkg], None, "make test"),
            ("package.json beats go.mod", [pkg, ("go.mod", "")], None, "npm test"),
            ("Makefile beats package.json and go.mod", [pkg, ("go.mod", ""), mk], None,
             "make test"),
            ("--test-cmd beats all", [mk, pkg, ("go.mod", "")], "pytest tests/", "pytest tests/"),
            ("--test-cmd wins", [mk, ("go.mod", "")], "pytest tests/", "pytest tests/"),
            ("--test-cmd with no files", [], "true", "true"),
        ]
        for name, files, override, want in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                for f, text in files:
                    (Path(d) / f).write_text(text)
                self.assertEqual(stack.discover_test_cmd(Path(d), override), want)


class ParallelRestack(unittest.TestCase):
    """deliver: parallel — every slice replays onto the plan base, never a sibling."""

    PLAN = """# Plan: feat

Base branch: main
deliver: parallel

### Slice 1: one
- depends-on: []

### Slice 2: two
- depends-on: []

### Slice 3: three
- depends-on: []
"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plan = root / "03-plan.md"
        self.plan.write_text(self.PLAN)
        repo = root / "repo"
        repo.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        g("init", "-q", "-b", "main")
        Path("f.txt").write_text("x\n")
        g("add", "f.txt")
        g("commit", "-q", "-m", "base")
        for n in (1, 2, 3):
            g("switch", "-q", "-c", f"slice/feat-{n}", "main")
            g("commit", "-q", "--allow-empty", "-m", f"slice {n}")
        g("switch", "-q", "main")
        g("commit", "-q", "--allow-empty", "-m", "base moves")
        self.state = repo / ".git" / "stack-state.json"
        patcher = mock.patch("stack.shutil.which", return_value=None)  # gh absent
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_stack(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO()):
            try:
                code = stack.main([*argv[:1], str(self.plan), *argv[1:]])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_parents_each_slice_to_the_base(self):
        code, out, _ = self.run_stack("restack", "--dry-run", "--from", "2")
        self.assertEqual(code, 0)
        self.assertIn("rebase slice/feat-2 onto main", out)
        self.assertIn("rebase slice/feat-3 onto main", out)
        self.assertNotIn("onto slice/feat-1", out)
        self.assertNotIn("onto slice/feat-2", out)

    def test_restack_replays_each_slice_onto_the_updated_base(self):
        code, _, err = self.run_stack("restack", "--from", "1")
        self.assertEqual(code, 0, err)
        for n in (1, 2, 3):
            r = subprocess.run(["git", "merge-base", "--is-ancestor",
                                "main", f"slice/feat-{n}"])
            self.assertEqual(r.returncode, 0, f"slice/feat-{n} lacks the updated base")
        for a, b in ((1, 2), (1, 3), (2, 3)):
            r = subprocess.run(["git", "merge-base", "--is-ancestor",
                                f"slice/feat-{a}", f"slice/feat-{b}"])
            self.assertNotEqual(r.returncode, 0, f"slice/feat-{b} wrongly contains slice/feat-{a}")


if __name__ == "__main__":
    unittest.main()
