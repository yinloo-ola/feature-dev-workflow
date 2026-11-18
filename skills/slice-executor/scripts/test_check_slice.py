"""Tests for check_slice.py gates (B7-B11). Each case runs the real script in a temp git repo."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_slice.py"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def plan_text(deliver="parallel", cards=None):
    """cards: {num: dict(mode, deletes, deps, files, tests)}; v2 fields omitted when None."""
    out = [f"# Plan: feat\n\nBase branch: main\ndeliver: {deliver}\n"]
    for num, c in sorted(cards.items()):
        out.append(f"### Slice {num}: t{num}")
        if c.get("mode"):
            out.append(f"- mode: {c['mode']}")
        if c.get("deletes"):
            out.append(f"- deletes-existing: {c['deletes']}")
        out.append(f"- depends-on: {c.get('deps', [])}")
        b = c.get("bullet", "  - ")  # bullet prefix under files/tests
        out.append("- files:" + "".join(f"\n{b}{f}" for f in c.get("files", ["src/a.py"])))
        out.append("- tests:" + "".join(f"\n{b}{f}" for f in c.get("tests", ["test_a.py"])))
        out.append("- card: |\n    goal\n" + c.get("prose", ""))
    return "\n".join(out)


class Repo:
    def __init__(self, tmp):
        self.dir = Path(tmp) / "repo"
        self.dir.mkdir()
        self.plan = Path(tmp) / "plan.md"  # outside the repo: keeps the tree clean
        self.git("init", "-q", "-b", "main")
        self.write("src/a.py", "a\nb\nc\n")
        self.commit("init")

    def git(self, *a):
        r = subprocess.run(["git", *a], cwd=self.dir, env=GIT_ENV, capture_output=True,
                           text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def write_bytes(self, rel, data):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def run(self, mode, n, plan):
        self.plan.write_text(plan, encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPT), mode, str(self.plan), str(n)],
                           cwd=self.dir, capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return r.returncode, r.stdout + r.stderr

    def branch(self, name):
        self.git("checkout", "-q", "-b", name)


class GateCase(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.r = Repo(self._t.name)
        self.r.branch("slice/feat-1")

    def verify(self, **card):
        card.setdefault("files", ["src/a.py"])
        return self.r.run("verify", 1, plan_text(cards={1: card}))


class VerifyGates(GateCase):
    # B7
    def test_b7_undeclared_production_file_fails(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("src/other.py", "x\n")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("scope:", out)
        self.assertIn("src/other.py", out)

    def test_b7_undeclared_test_file_warns_exit0(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")
        self.r.write("extra_test.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN: scope: extra_test.py: undeclared test file edited — "
                      "orchestrator to resolve (add to card or revert)", out)

    # B8
    def test_b8_undeclared_deletions_fail(self):
        self.r.write("src/a.py", "a\n")  # deletes 2 non-test lines
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        for deletes in (None, "no"):
            rc, out = self.verify(deletes=deletes)
            self.assertEqual(rc, 1, out)
            self.assertIn("2 non-test lines deleted — declare deletes-existing: yes", out)

    def test_b8_declared_deletions_pass(self):
        self.r.write("src/a.py", "a\n")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(deletes="yes — drops b,c: superseded")
        self.assertEqual(rc, 0, out)

    def test_b8_yes_without_reason_fails(self):
        self.r.write("src/a.py", "a\n")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, _ = self.verify(deletes="yes")
        self.assertEqual(rc, 1)

    def test_b8_test_file_deletions_dont_count(self):
        self.r.write("test_a.py", "1\n2\n3\n")
        self.r.commit("t")  # lands on slice branch; re-cut base below
        self.r.git("checkout", "-q", "main")
        self.r.git("merge", "-q", "slice/feat-1")
        self.r.git("checkout", "-q", "-B", "slice/feat-1")
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "1\n")  # 2 test-line deletions
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)

    # B9
    def test_b9_declared_test_unchanged_fails(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("declared test files not changed", out)

    def test_b9_tests_none_honored(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.commit("x")
        rc, out = self.verify(tests=["none"])
        self.assertEqual(rc, 0, out)

    # advisory mode + B10
    def test_legacy_mode_only_plan_is_advisory_and_gates_run(self):
        self.r.write("src/a.py", "a\n")  # deletions: legacy 'contract' must NOT permit them
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(mode="contract")
        self.assertEqual(rc, 1)
        self.assertIn("legacy mode: contract, advisory", out)
        self.assertIn("deletions:", out)

    def test_legacy_mode_additive_passes_additions(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(mode="additive")
        self.assertEqual(rc, 0, out)
        self.assertIn("legacy mode: additive, advisory", out)

    def test_b10_report_breakdown_and_no_size_cap(self):
        self.r.write("src/a.py", "a\nb\nc\n" + "x\n" * 500)  # far over the old 350 cap
        self.r.write("test_a.py", "t\n" * 4)
        self.r.write("locale/x.po", "m\n" * 3)
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn("size report — code: 500, tests: 4, data/generated: 3 (no cap)", out)

    def test_b10_po_and_pot_classify_generated(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")
        self.r.write("l/a.po", "1\n2\n")
        self.r.write("l/a.pot", "1\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn("code: 1, tests: 1, data/generated: 3", out)

    def test_b10_code_generated_header_classifies_generated(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")
        self.r.write("src/gen.go", "// Code generated by tool. DO NOT EDIT.\npackage x\nvar a = 1\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/gen.go"])
        self.assertEqual(rc, 0, out)
        self.assertIn("code: 1, tests: 1, data/generated: 3", out)

    def test_b10_header_beyond_two_lines_is_code(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")
        self.r.write("src/n.go", "x\ny\n// Code generated by tool. DO NOT EDIT.\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/n.go"])
        self.assertIn("code: 4, tests: 1, data/generated: 0", out)

    def test_report_printed_on_failure_too(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.commit("x")  # declared test unchanged -> FAIL
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("size report — code: 1, tests: 0, data/generated: 0 (no cap)", out)


BIN = bytes(range(256))  # NUL bytes + invalid UTF-8: git numstat reports "-\t-"


class GateFixes(GateCase):
    """Slice 13: crash, false-pass and false-fail fixes. Items 1-5 each FAIL on the pre-fix
    code; item 6 rows are boundary rows that pin the gates against mutation."""

    def base_change(self):  # the declared src + test change every case needs
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")

    # 1. binary / non-UTF-8
    def test_i1_binary_declared_ok(self):
        self.base_change()
        self.r.write_bytes("blob.bin", BIN)
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "blob.bin"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)

    def test_i1_binary_undeclared_fails_without_traceback(self):
        self.base_change()
        self.r.write_bytes("blob.bin", BIN)
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("scope:", out)
        self.assertIn("blob.bin", out)

    def test_i1_binary_modified_has_no_deletions(self):
        self.r.git("checkout", "-q", "main")
        self.r.write_bytes("blob.bin", BIN)
        self.r.commit("blob")
        self.r.git("checkout", "-q", "-B", "slice/feat-1")
        self.base_change()
        self.r.write_bytes("blob.bin", BIN[::-1])
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "blob.bin"])  # no deletes-existing
        self.assertEqual(rc, 0, out)
        self.assertIn("code: 1, tests: 1, data/generated: 0", out)

    def test_i1_non_utf8_text_file_does_not_crash(self):
        self.r.write_bytes("src/a.py", b"a\nb\nc\n\xe9\n")  # latin-1 text, no NUL
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)

    # 2. paths: NUL-separated, unquoted, rename = delete + add
    def test_i2_non_ascii_and_space_paths_declared_ok(self):
        self.base_change()
        self.r.write("src/dïr/é.py", "x\n")
        self.r.write("src/with space.py", "x\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/dïr/é.py", "src/with space.py"])
        self.assertEqual(rc, 0, out)

    def test_i2_undeclared_non_ascii_path_named_unquoted(self):
        self.base_change()
        self.r.write("src/ü x.py", "x\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("src/ü x.py", out)

    def test_i2_rename_both_paths_declared_ok(self):
        self.r.git("mv", "src/a.py", "src/b.py")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/b.py"], deletes="yes — rename a to b")
        self.assertEqual(rc, 0, out)

    def test_i2_rename_one_path_undeclared_fails_with_note(self):
        self.r.git("mv", "src/a.py", "src/b.py")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/b.py"], deletes="yes — rename a to b")
        self.assertEqual(rc, 1, out)
        self.assertIn("src/a.py", out)
        self.assertIn("renames need both paths declared", out)

    # 3. exclusions match directory components, not substrings
    def test_i3_substring_dirs_are_not_excluded(self):
        for path in ("src/rebuild/prod.py", "redist/x.py"):
            with self.subTest(path=path):
                self.r.git("checkout", "-q", "-B", "slice/feat-1", "main")
                self.base_change()
                self.r.write(path, "x\n")
                self.r.commit("x")
                rc, out = self.verify()
                self.assertEqual(rc, 1, out)
                self.assertIn(path, out)

    def test_i3_directory_components_are_excluded_at_any_depth(self):
        for path in ("a/vendor/b.go", "build/x.js", "dist/y.js"):
            with self.subTest(path=path):
                self.r.git("checkout", "-q", "-B", "slice/feat-1", "main")
                self.base_change()
                self.r.write(path, "x\n")
                self.r.commit("x")
                rc, out = self.verify()
                self.assertEqual(rc, 0, out)
                self.assertIn("code: 1, tests: 1, data/generated: 1", out)

    # 4. widened TEST_RE
    def test_i4_test_files_warn_not_fail_and_count_as_tests(self):
        for path in ("test_bar.py", "foo.test.mjs", "foo.test.jsx", "tests/helper.py",
                     "src/__tests__/x.ts", "conftest.py", "FooTest.java", "x_spec.rb"):
            with self.subTest(path=path):
                self.r.git("checkout", "-q", "-B", "slice/feat-1", "main")
                self.base_change()
                self.r.write(path, "1\n2\n")
                self.r.commit("x")
                rc, out = self.verify()
                self.assertEqual(rc, 0, out)
                self.assertIn(f"WARN: scope: {path}: undeclared test file edited", out)
                self.assertIn("code: 1, tests: 3, data/generated: 0", out)

    def test_i4_contest_is_not_a_test(self):
        self.base_change()
        self.r.write("src/contest.py", "x\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("scope:", out)
        self.assertIn("src/contest.py", out)

    # 5. parser aligned with plan_lint
    def test_i5_indented_and_tab_bullets_are_declared(self):
        self.base_change()
        self.r.commit("x")
        for bullet in ("    - ", "\t- "):
            with self.subTest(bullet=bullet):
                rc, out = self.verify(bullet=bullet)
                self.assertEqual(rc, 0, out)

    def test_i5_prose_field_lookalike_is_not_a_field(self):
        self.base_change()
        self.r.write("src/evil.py", "x\n")
        self.r.commit("x")
        rc, out = self.verify(prose="    - files: src/evil.py\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("src/evil.py", out)

    def test_i5_bad_deliver_value_exits_1(self):
        self.base_change()
        self.r.commit("x")
        rc, out = self.r.run("verify", 1, plan_text("statcked", {1: {"files": ["src/a.py"]}}))
        self.assertEqual(rc, 1, out)
        self.assertIn("deliver must be parallel or stacked, got 'statcked'", out)

    # 6. boundary / mutation rows (pass pre-fix by design; they pin the behavior)
    def test_i6_single_deleted_line_fails_without_declaration(self):
        self.r.write("src/a.py", "a\nb\n")  # exactly one deleted non-test line
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("1 non-test lines deleted — declare deletes-existing: yes", out)

    def test_i6_size_report_counts_adds_plus_dels(self):
        self.r.write("src/a.py", "a\nn1\nn2\nn3\n")  # 3 adds, 2 dels
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(deletes="yes — replaces b,c")
        self.assertEqual(rc, 0, out)
        self.assertIn("code: 5, tests: 1, data/generated: 0", out)

    def test_i6_dirty_tracked_file_fails(self):
        self.base_change()
        self.r.commit("x")
        self.r.write("src/a.py", "dirty\n")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("working tree is not clean", out)

    def test_i6_branch_identical_to_base_fails(self):
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("no changes between main and HEAD", out)

    def test_i6_generated_header_line_2_is_generated_line_3_is_code(self):
        self.base_change()
        self.r.write("src/l2.go", "x\n// Code generated by tool. DO NOT EDIT.\n")
        self.r.write("src/l3.go", "x\ny\n// Code generated by tool. DO NOT EDIT.\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/l2.go", "src/l3.go"])
        self.assertEqual(rc, 0, out)
        self.assertIn("code: 4, tests: 1, data/generated: 2", out)

    def test_i6_declared_test_file_never_warns(self):
        self.base_change()
        self.r.write("test_b.py", "t\n")
        self.r.commit("x")
        rc, out = self.verify(tests=["test_a.py", "test_b.py"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("WARN", out)


class GateGaps(GateCase):
    """Slice 16: mutation-audit survivors. Each row names the mutation it pins in its comment."""

    def fresh(self):  # re-cut the slice branch from main (subTest rows)
        self.r.git("checkout", "-q", "-B", "slice/feat-1", "main")

    def on_main(self, rel, text):  # land a file on main, then re-cut the slice branch
        self.r.git("checkout", "-q", "main")
        (self.r.write_bytes if isinstance(text, bytes) else self.r.write)(rel, text)
        self.r.commit("base file")
        self.fresh()

    def base_change(self):
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")

    # a. b_delre: DELETES_RE must demand a non-empty reason
    def test_a_empty_reason_does_not_declare_deletions(self):
        self.r.write("src/a.py", "a\n")
        self.r.write("test_a.py", "t\n")
        self.r.commit("x")
        for deletes in ("yes \u2014", "yes -"):
            with self.subTest(deletes=deletes):
                rc, out = self.verify(deletes=deletes)
                self.assertEqual(rc, 1, out)
                self.assertIn("2 non-test lines deleted", out)

    # b. b_gen_regex (hand-written "Code generated" is code), b_delgen (real header is generated)
    def test_b_code_generated_without_do_not_edit_is_code(self):
        self.on_main("src/hand.go", "// Code generated by hand\npackage x\nvar a = 1\nvar b = 2\n")
        self.base_change()
        self.r.write("src/hand.go", "// Code generated by hand\npackage x\n")  # 2 deletions
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/hand.go"])
        self.assertEqual(rc, 1, out)
        self.assertIn("2 non-test lines deleted", out)
        self.assertIn("code: 3, tests: 1, data/generated: 0", out)

    def test_b_real_generated_header_deletions_do_not_fail(self):
        self.on_main("src/gen.go", "// Code generated by tool. DO NOT EDIT.\npackage x\nvar a = 1\nvar b = 2\n")
        self.base_change()
        self.r.write("src/gen.go", "// Code generated by tool. DO NOT EDIT.\npackage x\n")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/gen.go"])  # no deletes-existing
        self.assertEqual(rc, 0, out)
        self.assertNotIn("deletions:", out)
        self.assertIn("code: 1, tests: 1, data/generated: 2", out)

    # c. b_excl_filename_dir (a FILE named like an excluded dir is not excluded),
    #    b_excl_name (lockfile names match at any depth)
    def test_c_files_named_like_excluded_dirs_are_in_scope(self):
        for name in ("build", "dist", "vendor"):
            with self.subTest(name=name):
                self.fresh()
                self.base_change()
                self.r.write(name, "x\n")
                self.r.commit("x")
                rc, out = self.verify()
                self.assertEqual(rc, 1, out)
                self.assertIn("scope:", out)
                self.assertIn(f"'{name}'", out)

    def test_c_nested_lockfile_is_exempt(self):
        self.base_change()
        self.r.write("web/package-lock.json", "{}\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn("code: 1, tests: 1, data/generated: 1", out)

    # d. c20: directory components match exactly, not by prefix
    def test_d_directory_prefixes_are_not_excluded(self):
        for path in ("distro/x.py", "builder/y.py"):
            with self.subTest(path=path):
                self.fresh()
                self.base_change()
                self.r.write(path, "x\n")
                self.r.commit("x")
                rc, out = self.verify()
                self.assertEqual(rc, 1, out)
                self.assertIn(path, out)

    # e. b_tabsplit: a TAB inside a path survives the numstat record split
    def test_e_tab_in_path_undeclared_fails_declared_ok(self):
        path = "src/ta\tb.py"
        self.base_change()
        self.r.write(path, "x\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("scope:", out)
        self.assertIn("src/ta\\tb.py", out)  # repr() escapes the TAB
        rc, out = self.verify(files=["src/a.py", path])
        self.assertEqual(rc, 0, out)

    # f. b_tre_anchor, b_tre_rs, b_tre_spec, b_tre_kt, b_tre_Tests
    def test_f_attest_is_not_a_test(self):
        self.base_change()
        self.r.write("src/attest_x.py", "x\n")
        self.r.commit("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1, out)
        self.assertIn("scope:", out)
        self.assertIn("src/attest_x.py", out)

    def test_f_test_naming_conventions_warn_not_fail(self):
        for path in ("foo_test.rs", "foo.spec.ts", "FooTest.kt", "FooTests.java", "conftest.py"):
            with self.subTest(path=path):
                self.fresh()
                self.base_change()
                self.r.write(path, "1\n2\n")
                self.r.commit("x")
                rc, out = self.verify()
                self.assertEqual(rc, 0, out)
                self.assertIn(f"WARN: scope: {path}: undeclared test file edited", out)
                self.assertIn("code: 1, tests: 3, data/generated: 0", out)

    # g. b_istest_declared: a declared tests: file outside every pattern is still a test
    def test_g_declared_nonpattern_test_file(self):
        self.on_main("docs/cases.txt", "1\n2\n3\n")
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("docs/cases.txt", "1\n")  # 2 deletions
        self.r.commit("x")
        rc, out = self.verify(tests=["docs/cases.txt"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("deletions:", out)
        self.assertIn("code: 1, tests: 2, data/generated: 0", out)

    # h. field boundaries: b_field_noend, b_field_strip, b_bullet_strip
    def test_h_card_prose_bullets_are_not_declarations(self):
        self.base_change()
        self.r.write("src/evil.py", "x\n")
        self.r.commit("x")
        rc, out = self.verify(prose="    - src/evil.py\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("src/evil.py", out)

    def test_h_bullets_after_other_field_are_not_declarations(self):
        self.base_change()
        self.r.write("src/evil.py", "x\n")
        self.r.commit("x")
        plan = plan_text(cards={1: {"files": ["src/a.py"]}})
        for field in ("depends-on: []", "mode: contract"):
            with self.subTest(field=field):
                rc, out = self.r.run("verify", 1, plan.replace(
                    "- card: |", f"- {field}\n  - src/evil.py\n- card: |"))
                self.assertEqual(rc, 1, out)
                self.assertIn("src/evil.py", out)

    def test_h_trailing_spaces_still_parse(self):
        self.base_change()
        self.r.commit("x")
        plan = plan_text(cards={1: {"files": ["src/a.py"], "deps": []}})
        inline = plan.replace("- files:\n  - src/a.py", "- files: src/a.py   ") \
                     .replace("- tests:\n  - test_a.py", "- tests: test_a.py   ") \
                     .replace("- depends-on: []", "- depends-on: []   ")
        self.assertNotEqual(plan, inline)
        rc, out = self.r.run("verify", 1, inline)
        self.assertEqual(rc, 0, out)
        rc, out = self.verify(files=["src/a.py   "], tests=["test_a.py   "])  # bullet items
        self.assertEqual(rc, 0, out)

    # j. generated / binary deletions are never non-test deletions
    def test_j_generated_header_deletions_never_count(self):
        self.on_main("src/gen.go", "// Code generated by tool. DO NOT EDIT.\nx\ny\nz\n")
        self.base_change()
        self.r.write("src/gen.go", "// Code generated by tool. DO NOT EDIT.\n")  # 3 deletions
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "src/gen.go"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("deletions:", out)

    def test_j_binary_removal_never_counts(self):
        self.on_main("blob.bin", BIN)
        self.base_change()
        self.r.git("rm", "-q", "blob.bin")
        self.r.commit("x")
        rc, out = self.verify(files=["src/a.py", "blob.bin"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("deletions:", out)


class StackedVerify(unittest.TestCase):
    """Slice 10: deliver: stacked verifies against the parent slice branch."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.r = Repo(self._t.name)
        self.r.branch("slice/feat-1")
        self.r.write("s1.txt", "1\n")
        self.r.write("test_s1.py", "t\n")
        self.r.commit("s1")
        self.r.branch("slice/feat-2")
        self.r.write("src/a.py", "a\nb\nc\nd\n")
        self.r.write("test_a.py", "t\n")
        self.r.commit("s2")

    def cards(self):
        return {1: {"files": ["s1.txt"], "tests": ["test_s1.py"]},
                2: {"deps": [1], "files": ["src/a.py"], "tests": ["test_a.py"]}}

    def test_stacked_diffs_against_parent(self):
        rc, out = self.r.run("verify", 2, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 0, out)
        self.assertIn("vs slice/feat-1;", out)

    def test_parallel_same_repo_parent_files_out_of_scope(self):
        rc, out = self.r.run("verify", 2, plan_text("parallel", self.cards()))
        self.assertEqual(rc, 1)
        self.assertIn("s1.txt", out)
        self.assertIn("scope:", out)

    def test_stacked_highest_dep_is_parent(self):
        self.r.git("branch", "slice/feat-0", "main")  # lower dep must not be picked
        cards = self.cards()
        cards[2]["deps"] = [0, 1]
        rc, out = self.r.run("verify", 2, plan_text("stacked", cards))
        self.assertEqual(rc, 0, out)
        self.assertIn("vs slice/feat-1;", out)

    def test_stacked_no_depends_on_uses_plan_base(self):
        self.r.git("checkout", "-q", "slice/feat-1")
        rc, out = self.r.run("verify", 1, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 0, out)
        self.assertIn("vs main;", out)

    def test_stacked_missing_parent_branch_fails(self):
        self.r.git("branch", "-m", "slice/feat-1", "elsewhere")
        rc, out = self.r.run("verify", 2, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 1)
        self.assertIn("parent branch slice/feat-1 not found — stacked verify needs it checked "
                      "out locally; git fetch origin slice/feat-1:slice/feat-1", out)

    # slice 16 item 1: depends-on is parsed like plan_lint (comment stripped, bracketed list)
    def test_depends_on_comment_is_not_a_dependency(self):
        self.r.git("checkout", "-q", "slice/feat-1")
        plan = plan_text("stacked", self.cards()).replace("- depends-on: []", "- depends-on: []  # was 2")
        rc, out = self.r.run("verify", 1, plan)
        self.assertEqual(rc, 0, out)
        self.assertIn("vs main;", out)

    def test_depends_on_numbers_only_inside_brackets(self):
        plan = plan_text("stacked", self.cards()).replace("- depends-on: [1]", "- depends-on: [1]  # was 5")
        rc, out = self.r.run("verify", 2, plan)
        self.assertEqual(rc, 0, out)
        self.assertIn("vs slice/feat-1;", out)

    def test_depends_on_must_be_bracketed(self):
        plan = plan_text("stacked", self.cards()).replace("- depends-on: [1]", "- depends-on: 1")
        rc, out = self.r.run("verify", 2, plan)
        self.assertEqual(rc, 1, out)
        self.assertIn("depends-on must be a bracketed list, got '1'", out)

    # i. deliver header grammar: b_deliver_dollar, _indent, _nobreak, _rstrip
    def test_i_deliver_header_extra_word_exits_1(self):
        rc, out = self.r.run("verify", 2, plan_text("stacked extra", self.cards()))
        self.assertEqual(rc, 1, out)
        self.assertIn("deliver must be parallel or stacked, got 'stacked extra'", out)

    def test_i_indented_deliver_in_card_prose_is_not_a_header(self):
        cards = self.cards()
        cards[2]["prose"] = "  deliver: stacked\n"
        plan = plan_text("parallel", cards).replace("deliver: parallel\n", "")  # no header: parallel
        rc, out = self.r.run("verify", 2, plan)
        self.assertEqual(rc, 1, out)  # parallel diff vs main: slice 1's files are out of scope
        self.assertIn("s1.txt", out)

    def test_i_first_deliver_header_wins(self):
        base = plan_text("parallel", self.cards())
        rc, out = self.r.run("verify", 2, base.replace("deliver: parallel\n", "deliver: parallel\ndeliver: stacked\n"))
        self.assertEqual(rc, 1, out)
        self.assertIn("s1.txt", out)
        rc, out = self.r.run("verify", 2, base.replace("deliver: parallel\n", "deliver: stacked\ndeliver: parallel\n"))
        self.assertEqual(rc, 0, out)
        self.assertIn("vs slice/feat-1;", out)

    def test_i_deliver_trailing_spaces_accepted(self):
        rc, out = self.r.run("verify", 2, plan_text("stacked   ", self.cards()))
        self.assertEqual(rc, 0, out)
        self.assertIn("vs slice/feat-1;", out)


class Ready(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.r = Repo(self._t.name)
        origin = Path(self._t.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        self.r.git("remote", "add", "origin", str(origin))
        self.r.git("push", "-q", "origin", "main")

    def cards(self):
        return {1: {"deps": []}, 2: {"deps": [1]}, 3: {"deps": [2]}}

    def stack(self, push2=True, push1=True):
        """slice/feat-1 <- slice/feat-2 (2 sits on 1's tip)."""
        self.r.branch("slice/feat-1")
        self.r.write("s1.txt", "1\n")
        self.r.commit("s1")
        self.r.branch("slice/feat-2")
        self.r.write("s2.txt", "2\n")
        self.r.commit("s2")
        if push1:
            self.r.git("push", "-q", "origin", "slice/feat-1")
        if push2:
            self.r.git("push", "-q", "origin", "slice/feat-2")
        self.r.git("checkout", "-q", "main")

    # B11 stacked
    def test_b11_stacked_pass(self):
        self.stack()
        rc, out = self.r.run("ready", 3, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 0, out)

    def test_b11_stacked_not_pushed_fails(self):
        self.stack(push2=False)
        rc, out = self.r.run("ready", 3, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 1)
        self.assertIn("not pushed", out)

    def test_b11_stacked_branch_missing_fails(self):
        rc, out = self.r.run("ready", 3, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 1)
        self.assertIn("not found locally", out)

    def test_b11_stacked_parent_advanced_fails(self):
        self.stack()
        self.r.git("checkout", "-q", "slice/feat-1")
        self.r.write("s1b.txt", "x\n")
        self.r.commit("parent moves")
        self.r.git("push", "-q", "origin", "slice/feat-1")
        rc, out = self.r.run("ready", 3, plan_text("stacked", self.cards()))
        self.assertEqual(rc, 1)
        self.assertIn("does not descend from parent tip", out)

    # B11 parallel unchanged
    def test_b11_parallel_merged_passes(self):
        self.stack()
        self.r.git("merge", "-q", "slice/feat-2")  # main now contains 1 and 2
        rc, out = self.r.run("ready", 3, plan_text("parallel", self.cards()))
        self.assertEqual(rc, 0, out)
        self.assertIn("merged into main", out)

    def test_b11_parallel_unmerged_fails(self):
        self.stack()
        rc, out = self.r.run("ready", 3, plan_text("parallel", self.cards()))
        self.assertEqual(rc, 1)
        self.assertIn("not merged into main", out)

    def test_b11_deliver_omitted_defaults_parallel(self):
        self.stack()
        plan = plan_text("parallel", self.cards()).replace("deliver: parallel\n", "")
        rc, out = self.r.run("ready", 3, plan)
        self.assertEqual(rc, 1)
        self.assertIn("not merged into main", out)


    # slice 18 item 6: git merge-base --is-ancestor exits 128 for a missing ref; only 1 means "not merged"
    def test_s18_parallel_missing_base_ref_fails_naming_base(self):
        self.stack()
        plan = plan_text("parallel", self.cards()).replace("Base branch: main", "Base branch: nosuch")
        rc, out = self.r.run("ready", 2, plan)  # dep branch slice/feat-1 exists, base does not
        self.assertEqual(rc, 1, out)
        self.assertIn("nosuch", out)
        self.assertNotIn("OK", out)

    def test_s18_parallel_missing_dependency_branch_fails_naming_it(self):
        rc, out = self.r.run("ready", 2, plan_text("parallel", self.cards()))  # nothing stacked
        self.assertEqual(rc, 1, out)
        self.assertIn("slice/feat-1", out)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main()
