#!/usr/bin/env python3
"""Deterministic gates for slice execution. Run from inside the slice's worktree.

Modes:
  ready   <plan.md> <n>  — pre-flight. deliver: parallel (default) — are slice n's
                           depends-on branches merged into the plan's base branch?
                           deliver: stacked — does each depends-on branch exist,
                           exist on origin, and descend from its parent's tip?
                           (run from any checkout)
  verify  <plan.md> <n>  — post-implementation gates, before opening the PR:
                           scope, deletions, tests (all always-wrong), plus the
                           size report (code/tests/data-generated; never fails).
                           deliver: stacked with depends-on diffs against the
                           parent slice's branch (slice/<f>-<highest dep>), not
                           the plan base.
                           mode: is legacy, printed as advisory context only.
                           (run from the worktree)

Exit 0 = pass, 1 = fail (reasons printed).
"""
import re
import subprocess
import sys
from pathlib import Path

SLICE_RE = re.compile(r"^### Slice (\d+):")
# Files classed data/generated in the size report; exempt from scope and deletions
EXCLUDED = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "go.sum", "uv.lock", "Gemfile.lock",
]
EXCLUDED_SUFFIX = (".pb.go", "_pb2.py", ".min.js", ".map", ".po", ".pot")
EXCLUDED_PARTS = ("vendor", "node_modules", "dist", "build", "generated")  # directory names, any depth
TEST_RE = re.compile(r"(^|/)(test_[^/]*\.py|conftest\.py)$|_test\.(go|py|rs)$|\.(test|spec)\.(ts|tsx|js|jsx|mjs)$|(^|/)(tests?|__tests__)/|(Test|Tests)\.(java|kt)$|_spec\.rb$")
GENERATED_RE = re.compile(r"Code generated .* DO NOT EDIT\.")  # first two lines of file
DELIVER_RE = re.compile(r"^deliver:\s*(parallel|stacked)$")  # matched per line, same grammar as plan_lint
FIELD_RE = re.compile(r"^- (\w[\w-]*):(.*)$")  # top-level card field: column 0 only
DELETES_RE = re.compile(r"^yes\s*—\s*(\S.*)$")  # same grammar as plan_lint
UNDECLARED_TEST = "undeclared test file edited — orchestrator to resolve (add to card or revert)"


def fail(msgs):
    print("FAIL:")
    for m in msgs:
        print(f"  - {m}")
    sys.exit(1)


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout.strip()


def parse_plan(path):
    """Return (base_branch, feature, {num: card}, deliver);
    card = dict(mode, deletes, deps, files, tests)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    base = re.search(r"^Base branch:\s*(\S+)", text, re.M)
    if not base:
        fail([f"{path}: missing 'Base branch:' header"])
    m = re.search(r"^# Plan:\s*(\S+)", text, re.M)
    feature = m.group(1) if m else Path(path).resolve().parent.name
    deliver = "parallel"
    for line in text.splitlines():
        if line.startswith("deliver:"):
            m = DELIVER_RE.match(line.rstrip())
            if not m:
                fail([f"deliver must be parallel or stacked, got {line[len('deliver:'):].strip()!r}"])
            deliver = m.group(1)
            break
    slices, current, field = {}, None, None
    for line in text.splitlines():
        m = SLICE_RE.match(line)
        if m:
            current, field = int(m.group(1)), None
            slices[current] = {"mode": None, "deletes": "no", "deps": [], "files": [], "tests": []}
            continue
        if current is None:
            continue
        m = FIELD_RE.match(line)
        if m:
            key, rest = m.group(1), m.group(2).strip()
            field = key if key in ("files", "tests") else None  # any other field (card, ...) ends the bullets
            c = slices[current]
            if key == "mode":
                c["mode"] = rest
            elif key == "deletes-existing":
                c["deletes"] = rest
            elif key == "depends-on":
                c["deps"] = [int(d) for d in re.findall(r"\d+", rest)]
            elif key in ("files", "tests") and rest:
                c[key].append(rest)
            continue
        if field in ("files", "tests") and line.lstrip().startswith("- "):
            slices[current][field].append(line.lstrip()[2:].strip())
    return base.group(1), feature, slices, deliver


def get_card(plan_path, n):
    base, feature, slices, deliver = parse_plan(plan_path)
    if n not in slices:
        fail([f"slice {n} not found in {plan_path}"])
    return base, slices[n], feature, deliver, slices


def is_excluded(path):
    *dirs, name = path.split("/")
    return (name in EXCLUDED or name.endswith(EXCLUDED_SUFFIX)
            or any(d in EXCLUDED_PARTS for d in dirs))


def is_testfile(path, tests):
    return path in tests or bool(TEST_RE.search(path))


def is_generated(path):
    """Data/generated: EXCLUDED lists, or a 'Code generated ... DO NOT EDIT.' header
    in the first two lines of the file at HEAD."""
    if is_excluded(path):
        return True
    rc, out = git("show", f"HEAD:{path}")
    return rc == 0 and bool(GENERATED_RE.search("\n".join(out.splitlines()[:2])))


def cmd_ready(plan_path, n):
    base, card, feature, deliver, slices = get_card(plan_path, n)
    if not card["deps"]:
        print(f"OK: slice {n} has no dependencies — ready.")
        return
    blocked = []
    for dep in card["deps"]:
        branch = f"slice/{feature}-{dep}"
        if deliver == "stacked":
            blocked += stacked_problems(feature, dep, branch, slices[dep]["deps"])
            continue
        rc, _ = git("merge-base", "--is-ancestor", branch, base)
        if rc != 0:
            rc2, _ = git("rev-parse", "--verify", branch)
            if rc2 != 0:
                blocked.append(f"slice {dep}: branch {branch} not found "
                               "(if merged and deleted, confirm with the orchestrator)")
            else:
                blocked.append(f"slice {dep}: branch {branch} not merged into {base}")
    if blocked:
        fail([f"slice {n} is blocked:"] + [f"    {b}" for b in blocked])
    if deliver == "stacked":
        print(f"OK: all dependencies of slice {n} exist, are pushed, and sit on their parent tips.")
    else:
        print(f"OK: all dependencies of slice {n} merged into {base}.")


def stacked_problems(feature, dep, branch, parents):
    """Which stacked-readiness check failed for dependency slice `dep` (empty = ready)."""
    if git("rev-parse", "--verify", branch)[0] != 0:
        return [f"slice {dep}: branch {branch} not found locally"]
    if git("rev-parse", "--verify", f"origin/{branch}")[0] != 0:
        return [f"slice {dep}: branch {branch} not pushed (origin/{branch} not found)"]
    out = []
    for p in parents:  # the dep must sit on top of its own parent slice's pushed tip
        pb = f"slice/{feature}-{p}"
        if git("rev-parse", "--verify", f"origin/{pb}")[0] != 0:
            out.append(f"slice {dep}: parent branch {pb} not pushed (origin/{pb} not found)")
        elif git("merge-base", "--is-ancestor", f"origin/{pb}", f"origin/{branch}")[0] != 0:
            out.append(f"slice {dep}: origin/{branch} does not descend from parent tip "
                       f"origin/{pb} — restack needed")
    return out


def cmd_verify(plan_path, n):
    base, card, feature, deliver, _ = get_card(plan_path, n)
    branch = f"slice/{feature}-{n}"

    # working tree must be clean (gates evaluate committed diff)
    rc, out = git("status", "--porcelain")
    if out:
        fail(["working tree is not clean — commit or remove changes first:",
              *[f"    {l}" for l in out.splitlines()]])

    if deliver == "stacked" and card["deps"]:  # linear stack: highest depends-on is the parent
        base = f"slice/{feature}-{max(card['deps'])}"
        if git("rev-parse", "--verify", base)[0] != 0:
            fail([f"parent branch {base} not found — stacked verify needs it checked out "
                  f"locally; git fetch origin {base}:{base}"])
    elif git("rev-parse", "--verify", base)[0] != 0:
        fail([f"base branch {base!r} not found in this repo"])

    rc, out = git("-c", "core.quotepath=off", "diff", "--numstat", "-z", "--no-renames",
                  f"{base}...HEAD")
    if rc != 0:
        fail([f"cannot diff {base}...HEAD: {out}"])
    if not out.strip():
        fail([f"no changes between {base} and HEAD — nothing to verify"])

    errors, warnings, changed = [], [], []
    code = tests = generated = dels_non_test = 0
    for rec in out.split("\0"):  # adds TAB dels TAB path NUL; renames arrive as delete + add
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            continue
        adds, dels, path = parts
        changed.append(path)
        binary = adds == "-"  # numstat "-\t-": never opened, no lines to count
        lines = 0 if binary else int(adds) + int(dels)
        dels = 0 if binary else int(dels)
        # size report: classify every changed line (never gates)
        if binary or is_generated(path):
            generated += lines
        elif is_testfile(path, card["tests"]):
            tests += lines
        else:
            code += lines
            dels_non_test += dels

    # scope gate: production file outside the card = FAIL; test file = WARN
    allowed = set(card["files"]) | set(card["tests"])
    outside = [p for p in changed if p not in allowed and not is_excluded(p)]
    prod = [p for p in outside if not is_testfile(p, card["tests"])]
    if prod:
        errors.append(f"scope: changed files outside the card's declared files/tests: {prod} "
                      "(renames need both paths declared)")
    warnings += [f"scope: {p}: {UNDECLARED_TEST}" for p in outside if p not in prod]

    # deletions gate: non-test deletions need deletes-existing: yes — <what and why>
    if dels_non_test > 0 and not DELETES_RE.match(card["deletes"]):
        errors.append(f"deletions: {dels_non_test} non-test lines deleted — declare "
                      "deletes-existing: yes — <what and why> on the card")

    # test gate: declared test files must be in the diff; "tests: none" is honored
    if card["tests"] and card["tests"] != ["none"]:
        missing = [t for t in card["tests"] if t not in changed]
        if missing:
            errors.append(f"tests: declared test files not changed in this diff: {missing}")

    advisory = f" (legacy mode: {card['mode']}, advisory)" if card["mode"] else ""
    print(f"slice {n} ({branch}) vs {base}; deliver: {deliver}{advisory}")
    for w in warnings:
        print(f"WARN: {w}")
    print(f"size report — code: {code}, tests: {tests}, data/generated: {generated} (no cap)")
    if errors:
        fail([f"slice {n} ({branch}) vs {base}:"] + errors)
    print(f"OK: slice {n} — {len(changed)} files; scope/deletions/tests gates passed.")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("ready", "verify"):
        print("usage: check_slice.py ready|verify <plan.md> <slice-number>", file=sys.stderr)
        sys.exit(1)
    mode, plan, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    (cmd_ready if mode == "ready" else cmd_verify)(plan, n)
