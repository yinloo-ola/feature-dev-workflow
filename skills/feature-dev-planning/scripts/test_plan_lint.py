"""Tests for plan_lint.py — rows map to RFC Behaviors B1-B5 (slice-execution-v2).

B6 (the identifier grep) is retired: it only fired on cards that happened to backtick
identifiers, so in practice it never fired at all."""
import contextlib
import importlib.util
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


def chain_plan(n, deliver=""):
    """Cards 1..n, each depending on the previous one."""
    return HEAD + deliver + "\n" + card(1, "") + "".join(card(i, "", f"[{i - 1}]") for i in range(2, n + 1))

def fan_plan(n, deliver=""):
    """Foundation 1 plus cards 2..n that all depend only on [1]."""
    return HEAD + deliver + "\n" + card(1, "") + "".join(card(i, "", "[1]") for i in range(2, n + 1))

class PlanLintV2(unittest.TestCase):
    def test_b1_v2_plan_passes_with_shape_line(self):
        code, out = lint(V2_PLAN)
        self.assertEqual(code, 0, out)
        self.assertIn("3 slices; deliver: stacked; deletes-existing: 1;", out)
        self.assertNotIn("WARN", out)

    def test_b2_legacy_plan_passes_with_one_warn(self):
        code, out = lint(LEGACY_PLAN)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("legacy mode field"), 1, out)
        self.assertIn("deliver: parallel; deletes-existing: 0;", out)  # defaults

    def test_failures(self):
        rows = {
            "B3 slice 1 yes": (
                HEAD + card(1, "- deletes-existing: yes — rewrites the base\n") + card(2, "", "[1]"),
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

    def test_single_card_plan_may_delete(self):
        """A one-card plan has no foundation to protect: deletes-existing: yes passes."""
        for label, deletes in (
            ("no", "- deletes-existing: no\n"),
            ("omitted", ""),
            ("yes", "- deletes-existing: yes — Refresh now returns ErrNoSession\n"),
        ):
            with self.subTest(label):
                code, out = lint(HEAD + "\n" + card(1, deletes))
                self.assertEqual(code, 0, out)
                self.assertNotIn("foundation", out)
                self.assertIn("1 slice;", out)

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

class WarnChannel(unittest.TestCase):
    """Warnings are reported once per fact, and a shape question can be answered."""

    def test_legacy_warn_is_reported_once_for_the_plan(self):
        code, out = lint(LEGACY_PLAN)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("legacy mode field"), 1, out)
        self.assertIn("legacy mode field on 3 slices", out)

    def test_legacy_warn_singular(self):
        code, out = lint(HEAD + "\n" + card(1, "- mode: additive\n"))
        self.assertEqual(code, 0, out)
        self.assertIn("legacy mode field on 1 slice —", out)

    def test_no_legacy_warn_for_v2_plan(self):
        code, out = lint(V2_PLAN)
        self.assertEqual(code, 0, out)
        self.assertNotIn("legacy mode field", out)

    def test_shape_ack_suppresses_warn_and_echoes_reason(self):
        acked = (HEAD + "shape-ack: chains are per-file; each link merges independently\n\n"
                 + chain_plan(7, "").split("\n\n", 1)[1])
        code, out = lint(acked)
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARN: pipeline", out)
        self.assertIn("shape-ack: chains are per-file; each link merges independently", out)

    def test_shape_ack_does_not_suppress_other_warns(self):
        acked = (HEAD + "shape-ack: accepted\n\n"
                 + chain_plan(7, "").split("\n\n", 1)[1] + card(8, "- mode: additive\n"))
        code, out = lint(acked)
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARN: pipeline", out)
        self.assertIn("legacy mode field on 1 slice", out)

    def test_shape_ack_ignored_when_shape_is_fine(self):
        plan = HEAD + "shape-ack: not needed here\n\n" + fan_plan(4).split("\n\n", 1)[1]
        code, out = lint(plan)
        self.assertEqual(code, 0, out)
        self.assertNotIn("shape-ack:", out.split("OK:")[0])
        self.assertNotIn("WARN", out)

    def test_shape_ack_needs_reason_text(self):
        """A bare 'shape-ack:' with no reason is not an acknowledgment."""
        acked = (HEAD + "shape-ack:\n\n" + chain_plan(7, "").split("\n\n", 1)[1])
        code, out = lint(acked)
        self.assertEqual(code, 0, out)
        self.assertIn("WARN: pipeline", out)

    def test_shape_ack_first_line_wins(self):
        acked = (HEAD + "shape-ack: first reason\nshape-ack: second reason\n\n"
                 + chain_plan(7, "").split("\n\n", 1)[1])
        code, out = lint(acked)
        self.assertIn("shape-ack: first reason", out)
        self.assertNotIn("second reason", out)

    def test_shape_warn_suppressed_when_plan_has_errors(self):
        """Shape is a judgement about a valid plan; a broken plan reports the breakage."""
        good = chain_plan(7)
        code, out = lint(good)
        self.assertEqual(code, 0, out)
        self.assertIn("WARN: pipeline", out)
        code, out = lint(good + card(9, "", "[42]"))
        self.assertEqual(code, 1, out)
        self.assertIn("depends-on 42 does not exist", out)
        self.assertNotIn("WARN: pipeline", out)


def shared_card(n, deps):
    """Card whose files are the single shared path src/shared.py."""
    return (f"### Slice {n}: title {n}\n- depends-on: {deps}\n- files:\n  - src/shared.py\n"
            f"- tests:\n  - src/test_f{n}.py\n- card: |\n    Do thing {n}.\n\n")


class PlanLintFixes(unittest.TestCase):
    """Slice 15: strict parsing, stacked chains, pinned boundaries."""

    def fails(self, plan, msg):
        code, out = lint(plan)
        self.assertEqual(code, 1, out)
        self.assertIn(msg, out)

    def test_item2_deliver_must_be_parallel_or_stacked(self):
        for bad in ("sideways", "Stacked", "parallel stacked", ""):
            with self.subTest(bad):
                self.fails(HEAD + f"deliver: {bad}\n\n" + card(1, ""),
                           f"deliver must be parallel or stacked, got '{bad}'")
        code, out = lint(HEAD + "deliver: stacked  \n\n" + card(1, ""))  # trailing blanks ok
        self.assertEqual(code, 0, out)
        code, out = lint(HEAD + "\n" + card(1, ""))  # absent -> parallel
        self.assertEqual(code, 0, out)
        self.assertIn("deliver: parallel;", out)

    def test_item3_depends_on_comment_and_brackets(self):
        slices = plan_lint.parse_plan(HEAD + card(1, "") + card(2, "", "[]  # was 2"))
        self.assertEqual(slices[2]["deps"], [])  # comment numbers are not dependencies
        slices = plan_lint.parse_plan(HEAD + card(1, "") + card(2, "", "[1] # note 3"))
        self.assertEqual(slices[2]["deps"], [1])
        code, out = lint(HEAD + card(1, "") + card(2, "", "[]  # was 2"))
        self.assertEqual(code, 0, out)
        self.assertIn("parallelizable immediately: [2]", out)

    def test_item3_depends_on_must_be_bracketed(self):
        for bad in ("2", "1, 2", "none", "", "[1", "1]", "# was [1]"):
            with self.subTest(bad):
                self.fails(HEAD + card(1, "") + card(2, "", bad),
                           "depends-on must be a bracketed list")

    def test_item4_malformed_slice_heading(self):
        rows = ["### Slice 2 - two", "### slice 2: lower", "###  Slice 2: two spaces", "### Slice"]
        for h in rows:
            with self.subTest(h):
                plan = HEAD + card(1, "") + h + "\n- depends-on: []\n- files:\n  - x\n\n"
                self.fails(plan, f"malformed slice heading: {h}")
        code, out = lint(HEAD + card(1, "") + "### Slices overview\n\n")  # not a slice heading
        self.assertEqual(code, 0, out)

    def test_item5_stacked_chain(self):
        msg = "(stacked branches form one linear chain)"
        valid = HEAD + "deliver: stacked\n\n" + card(1, "") + card(2, "", "[1]") + card(3, "", "[2]")
        code, out = lint(valid)
        self.assertEqual(code, 0, out)
        both = valid.replace("- depends-on: [2]", "- depends-on: [1, 2]")  # max == previous
        self.assertEqual(lint(both)[0], 0)
        gap = HEAD + "deliver: stacked\n\n" + card(1, "") + card(3, "", "[1]") + card(4, "", "[3]")
        self.assertEqual(lint(gap)[0], 0)  # previous slice by order, not n-1
        fan = HEAD + "deliver: stacked\n\n" + card(1, "") + card(2, "", "[1]") + card(3, "", "[1]")
        self.fails(fan, f"stacked plan: slice 3 must depend on slice 2 {msg}")
        self.assertEqual(lint(fan.replace("deliver: stacked", "deliver: parallel"))[0], 0)
        self.assertEqual(lint(fan.replace("deliver: stacked\n", ""))[0], 0)
        self.fails(HEAD + "deliver: stacked\n\n" + card(1, "") + card(2, ""),
                   f"stacked plan: slice 2 must depend on slice 1 {msg}")
        self.fails(HEAD + "deliver: stacked\n\n" + card(1, "", "[1]"),
                   f"stacked plan: slice 1 is first and must have depends-on []")

    def test_item6_self_dependency_fails(self):
        self.fails(HEAD + card(1, "") + card(2, "", "[2]"),
                   "slice 2: depends-on 2 must reference an earlier slice")

    def test_item6_sequenced_sharing_a_file_passes_whichever_has_the_edge(self):
        ok = HEAD + card(1, "") + shared_card(2, "[1]") + shared_card(3, "[2]")
        code, out = lint(ok)
        self.assertEqual(code, 0, out)
        code, out = lint(HEAD + card(1, "") + shared_card(2, "[3]") + shared_card(3, "[]"))
        self.assertNotIn("share files", out)  # reverse edge: still sequenced (other error only)
        self.fails(HEAD + card(1, "") + shared_card(2, "[1]") + shared_card(3, "[1]"),
                   "slices 2 and 3 share files ['src/shared.py'] without a dependency edge")

    def test_item6_deeper_chain_counts_and_slice_1_never_parallelizable(self):
        plan = (HEAD + card(1, "") + card(2, "") + card(3, "", "[2]") + card(4, "", "[1, 3]")
                + card(5, "", "[2, 4]"))
        code, out = lint(plan)
        self.assertEqual(code, 0, out)
        self.assertIn("2 → 3 → 4 → 5", out)
        self.assertIn("longest chain 4", out.splitlines()[-1])
        code, out = lint(HEAD + "\n" + card(1, "") + card(2, "") + card(3, ""))
        self.assertIn("parallelizable immediately: [2, 3]", out)
        self.assertNotIn("parallelizable immediately: [1", out)
        code, out = lint(chain_plan(3))
        self.assertIn("parallelizable immediately: none (all sequenced)", out)

    def test_item1_parsers_agree_on_fixture_plans(self):
        cs = load_check_slice(self)

        def sync_plan(i, nl="\n"):
            return ("# Plan: demo\n\nBase branch: main\ndeliver: stacked\n\n"
                    "### Slice 1: one\n- deletes-existing: no\n- depends-on: []\n- files:\n"
                    f"{i}- src/a.py\n{i}- src/b.py\n- tests:\n{i}- src/test_a.py\n- card: |\n"
                    f"{i}Do one.\n\n"
                    "### Slice 2: two\n- depends-on: [1]\n- files:\n"
                    f"{i}- src/c.py\n- tests: none\n- card: |\n{i}Do two.\n\n"
                    ).replace("\n", nl)
        indented = (sync_plan("    ").replace("deliver: stacked\n", "")  # header omitted: parallel
                    .replace("    Do one.\n", "    Do one.\n      deliver: stacked\n"))  # prose line
        rows = {"4-space": (sync_plan("    "), "stacked"), "tab": (sync_plan("\t"), "stacked"),
                "crlf": (sync_plan("  ", "\r\n"), "stacked"),
                "indented deliver in card prose": (indented, "parallel")}
        for name, (text, want) in rows.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                p = Path(d) / "03-plan.md"
                p.write_bytes(text.encode())  # bytes: CRLF survives to both parsers
                _, _, theirs, t_deliver = cs.parse_plan(str(p))
                mine = plan_lint.parse_plan(text)
                self.assertEqual((plan_lint.parse_deliver(text), t_deliver), (want, want))
                self.assertEqual(mine[1]["files"], ["src/a.py", "src/b.py"])
                self.assertEqual(set(mine), set(theirs))
                for n in mine:
                    for k in ("files", "tests"):
                        self.assertEqual(mine[n][k], theirs[n][k], (n, k))
                    self.assertEqual(mine[n]["deps"] or [], theirs[n]["deps"], n)
                self.assertEqual(theirs[2]["tests"], ["none"])


class PlanLintResidual(unittest.TestCase):
    """Slice 18: round-3 residual risks (items 7-11)."""

    def fails(self, plan, msg):
        code, out = lint(plan)
        self.assertEqual(code, 1, out)
        self.assertIn(msg, out)

    def test_item7_required_fields(self):
        def one(body):  # slice 2 built from raw field lines
            return HEAD + card(1, "") + f"### Slice 2: two\n{body}\n"
        files = "- files:\n  - src/f2.py\n"
        tests = "- tests:\n  - src/test_f2.py\n"
        cardf = "- card: |\n    Do two.\n"
        rows = {
            "no Base branch header": (
                "# Plan: demo\n\n" + card(1, ""), "missing 'Base branch:' header"),
            "no files": (one("- depends-on: []\n- files:\n" + tests + cardf),
                         "slice 2: files must list at least one non-test file"),
            "no tests": (one("- depends-on: []\n" + files + "- tests:\n" + cardf),
                         "slice 2: tests must list test files or be 'none'"),
            "empty card": (one("- depends-on: []\n" + files + tests + "- card: |\n"),
                           "slice 2: card is empty"),
            "no depends-on": (one(files + tests + cardf),
                              "slice 2: missing depends-on"),
            "no slice cards": (HEAD + "\nJust prose.\n", "no slice cards found"),
        }
        for name, (plan, msg) in rows.items():
            with self.subTest(name):
                self.fails(plan, msg)

    def test_item8_duplicate_slice_number(self):
        self.fails(HEAD + card(1, "") + card(2, "") + card(2, ""), "duplicate slice number: 2")

    def test_item9_indented_deliver_in_card_prose_is_not_the_header(self):
        prose = card(1, "").replace("    Do thing 1.\n", "    Do thing 1.\n  deliver: stacked\n")
        plan = HEAD + "\n" + prose + card(2, "")  # slice 2 has no edge to 1: would fail if stacked
        self.assertEqual(plan_lint.parse_deliver(plan), "parallel")
        code, out = lint(plan)
        self.assertEqual(code, 0, out)
        self.assertIn("deliver: parallel;", out)

    def test_item10_trailing_whitespace_and_tab_indented_card(self):
        code, out = lint(HEAD + card(1, "") + card(2, "", "[]  "))
        self.assertEqual(code, 0, out)
        self.assertEqual(plan_lint.parse_plan(HEAD + card(1, "") + card(2, "", "[1]   "))[2]["deps"], [1])
        tabbed = HEAD + ("### Slice 1: one\n- depends-on: []\n- files:\n  - src/f1.py\n"
                         "- tests:\n  - src/test_f1.py\n- card: |\n\tDo one.\n\n")
        self.assertEqual(plan_lint.parse_plan(tabbed)[1]["card"], ["Do one."])
        code, out = lint(tabbed)
        self.assertEqual(code, 0, out)
        self.assertNotIn("card is empty", out)


def load_check_slice(tc):
    path = Path(__file__).resolve().parents[2] / "slice-executor/scripts/check_slice.py"
    if not path.is_file():
        tc.skipTest(f"{path} not present — cross-sync with check_slice.py not checked")
    spec = importlib.util.spec_from_file_location("check_slice", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    unittest.main()
