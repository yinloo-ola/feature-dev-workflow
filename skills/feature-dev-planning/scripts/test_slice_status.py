"""Tests for slice_status.py — rows map to RFC Behaviors B1, B3-B10.

gh is pinned absent by default (as test_stack.py does) so no test needs the binary;
the PR rows mock stack.pr_state with the payload gh would return.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slice_status  # noqa: E402

GIT = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")
NO_PR = "PR state unavailable — merged is judged by local branch only"
STATES = {"merged", "in review", "in flight", "blocked", "ready", "not started"}


def line(n, state, detail=""):
    """One output row: the `{n:<3} {state:<12} {detail}` format RFC §1 specifies."""
    return f"{n:<3} {state:<12} {detail}"


def plan_text(cards):
    """cards: {num: (title, deps, amended)} — only the fields slice_status reads."""
    out = ["# Plan: feat", "", "Base branch: main", ""]
    for n, (title, deps, amended) in sorted(cards.items()):
        out += [f"### Slice {n}: {title}", f"- depends-on: {deps}", "- files:",
                "  - src/a.py", "- tests:", "  - test_a.py", "- card: |", "    goal"]
        if amended:
            out.append("    amended: card rewritten after the claim")
        out.append("")
    return "\n".join(out)


def default_cards():
    return {1: ("foundation", "[]", False), 2: ("needs one", "[1]", False),
            3: ("needs two", "[2]", False), 4: ("independent", "[]", False)}


def load_check_slice():
    path = Path(__file__).resolve().parents[2] / "slice-executor/scripts/check_slice.py"
    spec = importlib.util.spec_from_file_location("check_slice", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Repo:
    """A temp git repo with its plan beside it, so the checkout stays clean."""

    def __init__(self, tmp):
        self.root = Path(tmp)
        self.dir = self.root / "repo"
        self.dir.mkdir()
        self.plan = self.root / "03-plan.md"
        self.git("init", "-q", "-b", "main")
        (self.dir / "f.txt").write_text("x\n")
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args):
        r = subprocess.run(["git", *GIT, *args], cwd=self.dir, capture_output=True,
                           text=True, check=True)
        return r.stdout.strip()

    def branch(self, name):
        """A slice branch one commit ahead of main: it exists and is not merged."""
        self.git("switch", "-q", "-c", name)
        self.git("commit", "-q", "--allow-empty", "-m", name)
        self.git("switch", "-q", "main")

    def merge(self, name):
        self.git("merge", "-q", "--no-ff", "-m", f"merge {name}", name)

    def worktree(self, name, branch):
        path = self.root / name
        self.git("worktree", "add", "-q", str(path), branch)
        return path

    def claim(self, n):
        path = self.root / "claims" / f"slice-{n}"
        path.mkdir(parents=True)
        return path


class StatusCase(unittest.TestCase):
    """Slice 1 is merged into main, slice 2 has an unmerged branch, 3 and 4 have nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = tmp  # keep the object alive for the whole test
        self.repo = Repo(tmp.name)
        self.repo.branch("slice/feat-1")
        self.repo.merge("slice/feat-1")
        self.repo.branch("slice/feat-2")
        self.write_plan(default_cards())
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo.dir)
        gh_absent = mock.patch("slice_status.stack.shutil.which", return_value=None)
        gh_absent.start()
        self.addCleanup(gh_absent.stop)

    def write_plan(self, cards):
        self.repo.plan.write_text(plan_text(cards))
        return self.repo.plan

    def run_status(self, *argv, plan=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = slice_status.main([str(plan or self.repo.plan), *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def rows(out):
        """{slice number: its output line} — the leading warning line is not a slice row."""
        return {r.split()[0]: r for r in out.splitlines() if r[:1].isdigit()}

    def gh_prs(self, prs):
        """gh present, with pr_state answering `prs`: {branch: {'number','state'}}."""
        which = mock.patch("slice_status.stack.shutil.which", return_value="/usr/bin/gh")
        which.start()
        self.addCleanup(which.stop)
        patcher = mock.patch("slice_status.stack.pr_state", side_effect=lambda b: prs.get(b))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def snapshot(self):
        """Everything a read-only run must leave untouched."""
        return (self.repo.git("for-each-ref"),
                self.repo.git("worktree", "list", "--porcelain"),
                self.repo.git("status", "--porcelain"),
                sorted(p.name for p in self.repo.root.iterdir()))


class DerivedState(StatusCase):
    # B3 (local half): gh absent -> one line saying so, merged judged by the branch.
    def test_b3_gh_absent_prints_one_line_and_merges_by_ancestor(self):
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 6)  # 4 slices + the unavailable line + the summary
        self.assertEqual(lines[0], NO_PR)
        self.assertEqual(lines[1], line(1, "merged"))  # slice/feat-1 is an ancestor of main
        self.assertEqual(lines[2], line(2, "ready"))  # branch exists, dependency merged
        self.assertEqual(lines[3], line(3, "blocked", "waits on 2"))
        self.assertEqual(lines[4], line(4, "ready"))
        self.assertEqual(lines[5], "ready: 2, 4  blocked: 3 (on 2)")

    # B4
    def test_b4_every_slice_appears_once_with_one_of_the_six_states(self):
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        rows = out.splitlines()[1:-1]
        self.assertEqual([r.split()[0] for r in rows], ["1", "2", "3", "4"])
        for row in rows:
            self.assertIn(row[4:16].strip(), STATES, row)

    def test_worktree_holding_the_branch_is_in_flight(self):
        at = self.repo.worktree("wt-2", "slice/feat-2")
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out)["2"],
                         line(2, "in flight", f"claimed at {Path(at).resolve()}"))
        self.assertIn("in flight: 2", out.splitlines()[-1])

    def test_b8_unmerged_dependency_is_blocked_and_names_the_slices(self):
        cards = {1: ("foundation", "[]", False), 2: ("needs one", "[1]", False),
                 3: ("independent", "[]", False), 4: ("needs two and three", "[2, 3]", False)}
        self.write_plan(cards)
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out)["4"], line(4, "blocked", "waits on 2, 3"))
        self.assertEqual(out.splitlines()[-1], "ready: 2, 3  blocked: 4 (on 2, 3)")

    def test_ready_none_when_nothing_is_dispatchable(self):
        self.write_plan({1: ("foundation", "[]", False)})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[-1], "ready: none")


class PrState(StatusCase):
    # B1
    def test_b1_merged_pr_satisfies_a_deleted_branch(self):
        self.write_plan({1: ("foundation", "[]", False), 2: ("needs one", "[1]", False)})
        self.repo.git("branch", "-D", "slice/feat-1")
        self.gh_prs({"slice/feat-1": {"number": 11, "state": "MERGED"}})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(rows["1"], line(1, "merged", "PR #11"))
        self.assertEqual(rows["2"], line(2, "ready"))  # not "blocked"
        self.assertNotIn("blocked", out)

    # B5
    def test_b5_open_pr_is_in_review_not_in_flight(self):
        self.repo.claim(2)  # claimed too: the PR outranks the claim
        self.gh_prs({"slice/feat-2": {"number": 7, "state": "OPEN"}})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out)["2"], line(2, "in review", "PR #7"))

    # B9
    def test_b9_no_pr_flag_never_calls_gh(self):
        pr_state = self.gh_prs({"slice/feat-2": {"number": 7, "state": "OPEN"}})
        code, out, err = self.run_status("--no-pr")
        self.assertEqual(code, 0, err)
        self.assertEqual(pr_state.call_count, 0)
        self.assertEqual(out.splitlines()[0], NO_PR)

    def test_pr_state_is_asked_once_per_slice_in_plan_order(self):
        pr_state = self.gh_prs({})
        code, _, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual([c.args[0] for c in pr_state.call_args_list],
                         [f"slice/feat-{n}" for n in (1, 2, 3, 4)])


class Claims(StatusCase):
    # B6 — a stale claim is a purely local fact (claim dir, no branch, no worktree). It is
    # reported and counted whatever the slice's PR says, because the PR is the `state`
    # column's job and the leftover directory is the detail column's.
    def test_b6_local_claim_without_branch_is_stale_and_is_not_removed(self):
        claim = self.repo.claim(3)
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(rows["3"],
                         line(3, "in flight", f"stale claim — clear with rmdir {claim}"))
        self.assertTrue(claim.is_dir())  # read-only: the operator clears it, not us
        self.assertIn("stale claims: 1", out.splitlines()[-1])

    def test_b6_merged_pr_with_a_leftover_claim_is_still_stale(self):
        """The real case: .specs/slice-execution-v2/claims/slice-1 survives PR #1 merging."""
        self.repo.git("branch", "-D", "slice/feat-1")  # merged and cleaned up
        claim = self.repo.claim(1)
        self.gh_prs({"slice/feat-1": {"number": 1, "state": "MERGED"}})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(rows["1"], line(1, "merged", f"PR #1; stale claim — clear with rmdir {claim}"))
        self.assertTrue(claim.is_dir())
        summary = out.splitlines()[-1]
        self.assertIn("stale claims: 1", summary)
        self.assertNotIn("in flight", summary)  # merged outranks the claim, as ever

    def test_b6_merged_and_local_stale_claims_are_both_counted(self):
        self.repo.git("branch", "-D", "slice/feat-1")
        merged, local = self.repo.claim(1), self.repo.claim(3)
        self.gh_prs({"slice/feat-1": {"number": 1, "state": "MERGED"}})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(rows["1"], line(1, "merged", f"PR #1; stale claim — clear with rmdir {merged}"))
        self.assertEqual(rows["3"],
                         line(3, "in flight", f"stale claim — clear with rmdir {local}"))
        self.assertIn("stale claims: 2", out.splitlines()[-1])

    def test_stale_claim_survives_an_open_pr_too(self):
        # PR state never suppresses the cleanup hint: merged is not a special exception.
        self.repo.git("branch", "-D", "slice/feat-2")
        claim = self.repo.claim(2)
        self.gh_prs({"slice/feat-2": {"number": 7, "state": "OPEN"}})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out)["2"],
                         line(2, "in review", f"PR #7; stale claim — clear with rmdir {claim}"))
        self.assertIn("stale claims: 1", out.splitlines()[-1])

    # B7
    def test_b7_amended_line_flags_only_a_claim_older_than_the_plan(self):
        plan = self.write_plan({1: ("foundation", "[]", False),
                                2: ("amended card", "[1]", True)})
        claim = self.repo.claim(2)  # slice/feat-2 exists, so the claim is not stale
        mtime = plan.stat().st_mtime
        os.utime(claim, (mtime - 100, mtime - 100))  # claimed, then the card was rewritten
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out)["2"],
                         line(2, "in flight", "claimed, no worktree; amended since claim"))
        os.utime(claim, (mtime + 100, mtime + 100))  # claim newer than the card: quiet
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out)["2"], line(2, "in flight", "claimed, no worktree"))

    def test_staleness_never_changes_the_state_column(self):
        """PR state qualifies the row's `state`, never the claim's staleness.

        Slices 1, 2 and 3 each lose whatever branch they had and keep a claim dir, so all
        three are stale. Their states stay whatever the six-row table says — merged, in
        review, in flight — and the count is 3, not 0.
        """
        for n in (1, 2):  # the only slices setUp gives a branch
            self.repo.git("branch", "-D", f"slice/feat-{n}")
        for n in (1, 2, 3):
            self.repo.claim(n)
        self.gh_prs({"slice/feat-1": {"number": 11, "state": "MERGED"},
                     "slice/feat-2": {"number": 7, "state": "OPEN"}})
        code, out, err = self.run_status()
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertTrue(rows["1"].startswith(line(1, "merged")), rows["1"])
        self.assertTrue(rows["2"].startswith(line(2, "in review")), rows["2"])
        self.assertTrue(rows["3"].startswith(line(3, "in flight")), rows["3"])
        for n in (1, 2, 3):
            self.assertIn("stale claim — clear with rmdir", rows[str(n)], rows[str(n)])
        self.assertIn("stale claims: 3", out.splitlines()[-1])


class ReadOnly(StatusCase):
    # B10
    def test_b10_refs_worktrees_and_tree_are_unchanged(self):
        self.repo.claim(3)  # a non-trivial state to read
        self.repo.worktree("wt-2", "slice/feat-2")
        before = self.snapshot()
        self.assertEqual(self.run_status()[0], 0)
        self.assertEqual(self.snapshot(), before)  # gh absent
        self.gh_prs({"slice/feat-2": {"number": 7, "state": "OPEN"}})
        self.assertEqual(self.run_status()[0], 0)
        self.assertEqual(self.run_status("--no-pr")[0], 0)
        self.assertEqual(self.snapshot(), before)  # gh present


class Contract(StatusCase):
    def test_missing_plan_is_refused(self):
        code, out, err = self.run_status(plan=self.repo.root / "nope.md")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("not found", err)

    def test_deps_agree_with_check_slice_parser(self):
        # RFC §1: the per-card reader must agree with the sibling parser it does not import.
        _, _, slices, _ = load_check_slice().parse_plan(self.repo.plan)
        cards = slice_status.parse_cards(self.repo.plan)
        self.assertEqual({n: c["deps"] for n, c in cards.items()},
                         {n: c["deps"] for n, c in slices.items()})


if __name__ == "__main__":
    unittest.main()
