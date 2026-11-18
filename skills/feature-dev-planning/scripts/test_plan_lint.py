"""Tests for plan_lint.py — rows map to RFC Behaviors B1-B6 (slice-execution-v2)."""
import contextlib
import importlib.util
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

    def test_item1_test_re_is_check_slice_test_re(self):
        self.assertEqual(plan_lint.TEST_RE.pattern, load_check_slice(self).TEST_RE.pattern)

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
        rows = {"4-space": sync_plan("    "), "tab": sync_plan("\t"),
                "crlf": sync_plan("  ", "\r\n")}
        for name, text in rows.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                p = Path(d) / "03-plan.md"
                p.write_bytes(text.encode())  # bytes: CRLF survives to both parsers
                _, _, theirs, t_deliver = cs.parse_plan(str(p))
                mine = plan_lint.parse_plan(text)
                self.assertEqual((plan_lint.parse_deliver(text), t_deliver), ("stacked", "stacked"))
                self.assertEqual(mine[1]["files"], ["src/a.py", "src/b.py"])
                self.assertEqual(set(mine), set(theirs))
                for n in mine:
                    for k in ("files", "tests"):
                        self.assertEqual(mine[n][k], theirs[n][k], (n, k))
                    self.assertEqual(mine[n]["deps"] or [], theirs[n]["deps"], n)
                self.assertEqual(theirs[2]["tests"], ["none"])


def load_check_slice(tc):
    path = Path(__file__).resolve().parents[2] / "slice-executor/scripts/check_slice.py"
    if not path.is_file():
        tc.skipTest(f"{path} not present — cross-sync with check_slice.py not checked")
    spec = importlib.util.spec_from_file_location("check_slice", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IdentifierGrepBoundaries(unittest.TestCase):
    """Slice 15 item 6: pin the identifier grep's boundaries."""

    def warns(self, body, repo, files="src/f2.py", tests="src/test_f2.py"):
        plan = HEAD + "\n" + card(1, "") + ident_card(2, body, files, tests)
        code, out = lint_repo(plan, repo)
        self.assertEqual(code, 0, out)
        return out

    def test_repeated_identifier_warns_once(self):
        out = self.warns("`frobnicate` then `frobnicate` again", {"pkg/a_test.py": "frobnicate\n"})
        self.assertEqual(out.count("WARN: slice 2: identifier frobnicate"), 1, out)

    def test_cap_boundary_200_vs_201(self):
        def idents(n):
            return " ".join(f"`ident{i:04d}`" for i in range(n))
        repo = {"pkg/a_test.py": "ident0199 ident0200\n"}
        out = self.warns(idents(200), repo)
        self.assertIn("identifier ident0199 appears", out)
        self.assertNotIn("capped", out)
        out = self.warns(idents(201), repo)
        self.assertIn("identifier ident0199 appears", out)
        self.assertNotIn("identifier ident0200", out)
        self.assertIn("identifier grep capped at 200", out.splitlines()[-1])

    def test_slash_identifiers_skipped_and_not_counted(self):
        repo = {"pkg/a_test.py": "src/x/foo.py ident0000\n"}
        out = self.warns("`src/x/foo.py`", repo)
        self.assertNotIn("WARN", out)  # path-like text is skipped even when a test mentions it
        body = " ".join(f"`ident{i:04d}`" for i in range(200)) + " `src/x/foo.py`"
        out = self.warns(body, repo)
        self.assertNotIn("capped", out)  # a skipped identifier costs no grep

    def test_identifier_length_boundary(self):
        out = self.warns("`abc` and `abcd`", {"pkg/a_test.py": "abc abcd\n"})
        self.assertIn("identifier abcd appears", out)
        self.assertNotIn("identifier abc appears", out)

    def test_declared_files_and_tests_both_excluded(self):
        out = self.warns("`frobnicate`", {"pkg/a_test.py": "frobnicate\n", "pkg/z_test.py": "frobnicate\n"},
                         files="pkg/a_test.py")  # declared under files
        self.assertIn("undeclared test file pkg/z_test.py", out)
        self.assertNotIn("test file pkg/a_test.py", out)
        out = self.warns("`frobnicate`", {"pkg/a_test.py": "frobnicate\n", "pkg/z_test.py": "frobnicate\n"},
                         tests="pkg/a_test.py")  # declared under tests
        self.assertIn("undeclared test file pkg/z_test.py", out)
        self.assertNotIn("test file pkg/a_test.py", out)

    def test_shape_and_identifier_warns_suppressed_when_plan_has_errors(self):
        body = ident_card(8, "`frobnicate`", "src/f8.py", "src/test_f8.py")
        good = chain_plan(7) + body
        repo = {"pkg/a_test.py": "frobnicate\n"}
        code, out = lint_repo(good, repo)  # control: both warns appear without the error
        self.assertEqual(code, 0, out)
        self.assertIn("WARN: pipeline", out)
        self.assertIn("WARN: slice 8: identifier frobnicate", out)
        code, out = lint_repo(good + card(9, "", "[42]"), repo)
        self.assertEqual(code, 1, out)
        self.assertIn("depends-on 42 does not exist", out)
        self.assertNotIn("WARN", out)

    def test_test_files_named_test_star_are_scanned(self):
        out = self.warns("`frobnicate`", {"pkg/test_thing.py": "frobnicate\n"})
        self.assertIn("undeclared test file pkg/test_thing.py", out)


if __name__ == "__main__":
    unittest.main()
