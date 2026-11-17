"""Tests for plan_lint.py — rows map to RFC Behaviors B1-B4 (slice-execution-v2)."""
import contextlib
import io
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


if __name__ == "__main__":
    unittest.main()
