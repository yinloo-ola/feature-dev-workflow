"""Tests for plan_lint.py — rows map to RFC Behaviors B1-B6 (slice-execution-v2)."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_lint  # noqa: E402


def card(n, extra, deps="[]"):
    return (f"### Slice {n}: title {n}\n{extra}- depends-on: {deps}\n"
            f"- files:\n  - src/f{n}.py\n- tests:\n  - src/test_f{n}.py\n"
            f"- card: |\n    Do thing {n}.\n\n")


HEAD = "# Plan: demo\n\nBase branch: main\n"
V2_PLAN = (HEAD + "deliver: stacked\n\n" + card(1, "- deletes-existing: no\n")
           + card(2, "", "[1]")  # omitted → default no
           + card(3, "- deletes-existing: yes — drops the old path,\n  now unused\n", "[2]"))
LEGACY_PLAN = (HEAD + "\n" + card(1, "- mode: additive\n")
               + card(2, "- mode: migrate\n", "[1]") + card(3, "- mode: contract\n", "[2]"))


def lint(text):
    """Run plan_lint.main() on a plan string → (exit code, stdout)."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "03-plan.md"
        p.write_text(text)
        out, argv = io.StringIO(), sys.argv
        sys.argv = ["plan_lint.py", str(p)]
        try:
            with contextlib.redirect_stdout(out):
                code = plan_lint.main()
        finally:
            sys.argv = argv
    return code, out.getvalue()


def chain_plan(n, deliver=""):
    """Cards 1..n, each depending on the previous one."""
    return HEAD + deliver + "\n" + card(1, "") + "".join(card(i, "", f"[{i - 1}]") for i in range(2, n + 1))

def fan_plan(n, deliver=""):
    """Foundation 1 plus cards 2..n that all depend only on [1]."""
    return HEAD + deliver + "\n" + card(1, "") + "".join(card(i, "", "[1]") for i in range(2, n + 1))

def ident_card(n, body, files, tests):
    return (f"### Slice {n}: title {n}\n- depends-on: []\n- files:\n  - {files}\n"
            f"- tests:\n  - {tests}\n- card: |\n    {body}\n\n")

def lint_repo(plan, repo_files):
    """Lint plan_text at <tmp>/.specs/f/03-plan.md inside a git repo holding repo_files."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for rel, text in repo_files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        (root / ".specs/f").mkdir(parents=True)
        p = root / ".specs/f/03-plan.md"
        p.write_text(plan)
        out, argv = io.StringIO(), sys.argv
        sys.argv = ["plan_lint.py", str(p)]
        try:
            with contextlib.redirect_stdout(out):
                code = plan_lint.main()
        finally:
            sys.argv = argv
    return code, out.getvalue()

class PlanLintV2(unittest.TestCase):
    def test_b1_v2_plan_passes_with_shape_line(self):
        code, out = lint(V2_PLAN)
        self.assertEqual(code, 0, out)
        self.assertIn("3 slices; deliver: stacked; deletes-existing: 1;", out)
        self.assertNotIn("WARN", out)

    def test_b2_legacy_plan_passes_with_warn_per_card(self):
        code, out = lint(LEGACY_PLAN)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("legacy mode field — v2 plans use deletes-existing"), 3)
        self.assertIn("deliver: parallel; deletes-existing: 0;", out)  # defaults

    def test_failures(self):
        rows = {
            "B3 slice 1 yes": (
                HEAD + card(1, "- deletes-existing: yes — rewrites the base\n"),
                "slice 1 is the foundation — deletes-existing must be no"),
            "B4 yes without reason": (
                HEAD + card(1, "") + card(2, "- deletes-existing: yes\n"),
                "slice 2: deletes-existing must be"),
            "B4 yes with dash but no reason": (
                HEAD + card(1, "") + card(2, "- deletes-existing: yes —\n"),
                "slice 2: deletes-existing must be"),
            "value not in grammar": (
                HEAD + card(1, "") + card(2, "- deletes-existing: maybe\n"),
                "slice 2: deletes-existing must be"),
        }
        for name, (plan, msg) in rows.items():
            with self.subTest(name):
                code, out = lint(plan)
                self.assertEqual(code, 1, out)
                self.assertIn(msg, out)

    def test_parse_captures_reason_and_deliver(self):
        slices = plan_lint.parse_plan(V2_PLAN)
        self.assertEqual(slices[3]["reason"], "drops the old path,")
        self.assertEqual((slices[1]["deletes"], slices[2]["deletes"]), ("no", "no"))
        self.assertEqual(plan_lint.parse_deliver(V2_PLAN), "stacked")
        self.assertEqual(plan_lint.parse_deliver(HEAD + "deliver: sideways\n"), "parallel")


class PlanShape(unittest.TestCase):
    """B5: shape report."""
    def test_b5_chain_plan_warns_with_chain_order(self):
        code, out = lint(chain_plan(7))
        self.assertEqual(code, 0, out)  # never fails
        self.assertIn("WARN: pipeline, not parallel plan (fan-out 17%, longest chain 6)", out)
        self.assertIn("2 → 3 → 4 → 5 → 6 → 7", out)
        self.assertIn("fan-out 17%, longest chain 6", out.splitlines()[-1])  # OK line

    def test_b5_stacked_chain_prints_order_no_warn(self):
        code, out = lint(chain_plan(7, "deliver: stacked\n"))
        self.assertEqual(code, 0, out)
        self.assertIn("chain order: 2 → 3 → 4 → 5 → 6 → 7", out)
        self.assertNotIn("WARN", out)

    def test_b5_fan_out_plans_no_warn(self):
        rows = {
            "all depend on [1] -> 100%": (fan_plan(5), "fan-out 100%, longest chain 1"),
            "empty deps": (HEAD + "\n" + card(1, "") + card(2, "") + card(3, ""),
                           "fan-out 100%, longest chain 1"),
            "foundation only": (HEAD + "\n" + card(1, ""), "fan-out n/a, longest chain 0"),
        }
        for name, (plan, want) in rows.items():
            with self.subTest(name):
                code, out = lint(plan)
                self.assertEqual(code, 0, out)
                self.assertIn(want, out)
                self.assertNotIn("pipeline", out)

    def test_b5_thresholds(self):
        diamond = (HEAD + "\n" + card(1, "") + card(2, "") + card(3, "", "[2]")
                   + card(4, "", "[2]") + card(5, "", "[3, 4]"))
        rows = {  # name: (plan, warns?)
            "chain of 5 cards is within threshold": (
                chain_plan(6) + card(7, "") + card(8, "") + card(9, ""), False),
            "chain of 6 cards warns even with high fan-out": (
                chain_plan(7) + card(8, "") + card(9, "") + card(10, "") + card(11, "")
                + card(12, "") + card(13, ""), True),
            "ratio 30% is not below threshold": (
                HEAD + "\n" + card(1, "") + card(2, "") + card(3, "") + card(4, "")
                + "".join(card(i, "", f"[{d}]") for i, d in zip(range(5, 12), (2, 2, 3, 3, 4, 4, 4))),
                False),
            "diamond: ratio 25% warns": (diamond, True),
        }
        for name, (plan, warns) in rows.items():
            with self.subTest(name):
                code, out = lint(plan)
                self.assertEqual(code, 0, out)
                self.assertEqual("pipeline, not parallel plan" in out, warns, out)

class IdentifierGrep(unittest.TestCase):
    """B6: undeclared-test blast radius."""
    PLAN = (HEAD + "\n" + card(1, "") + ident_card(
        2, "Rename `frobnicate` and `a/b/frobnicate.py` and `abc`.", "src/f2.py", "src/test_f2.py"))

    def test_b6_undeclared_test_file_warns_naming_both(self):
        code, out = lint_repo(self.PLAN, {"pkg/foo_test.py": "frobnicate()\n",
                                          "src/test_f2.py": "frobnicate()\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("WARN: slice 2: identifier frobnicate appears in undeclared test file "
                      "pkg/foo_test.py — add pkg/foo_test.py to tests or note the required "
                      "update", out)
        self.assertNotIn("a/b/frobnicate.py", out)  # path-like excluded
        self.assertNotIn("identifier abc", out)  # shorter than 4

    def test_b6_no_warn_rows(self):
        rows = {
            "declared test file": {"pkg/foo_test.py": "x\n", "src/test_f2.py": "frobnicate()\n",
                                   "src/f2.py": "frobnicate"},
            "non-test file only": {"pkg/foo.py": "frobnicate()\n"},
            "node_modules test excluded": {"node_modules/x/a.test.js": "frobnicate()\n"},
            "no match": {"pkg/foo_test.go": "other()\n"},
            "substring is not a match": {"pkg/foo_test.go": "frobnicated()\n"},
        }
        for name, files in rows.items():
            with self.subTest(name):
                code, out = lint_repo(self.PLAN, files)
                self.assertEqual(code, 0, out)
                self.assertNotIn("WARN", out)

    def test_b6_test_file_patterns(self):
        for f in ("a_test.go", "a_test.py", "a.test.ts", "a.test.tsx", "a.test.js", "a.test.mjs",
                  "a.spec.ts", "a.spec.tsx", "a_test.rs"):
            with self.subTest(f):
                code, out = lint_repo(self.PLAN, {f: "frobnicate\n"})
                self.assertIn(f"undeclared test file {f}", out)

    def test_b6_not_a_git_repo_skips_with_note(self):
        code, out = lint(self.PLAN)  # plain temp dir
        self.assertEqual(code, 0, out)
        self.assertIn("identifier grep skipped (not a git repo)", out)
        self.assertNotIn("WARN", out)

    def test_b6_cap_noted_in_ok_line(self):
        idents = " ".join(f"`ident{i:04d}`" for i in range(250))
        plan = HEAD + "\n" + ident_card(1, idents, "src/f1.py", "src/test_f1.py")
        code, out = lint_repo(plan, {"pkg/a_test.py": "ident0000 ident0249\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("identifier ident0000 appears", out)
        self.assertNotIn("identifier ident0249", out)  # past the cap
        self.assertIn("identifier grep capped at 200", out.splitlines()[-1])

if __name__ == "__main__":
    unittest.main()
