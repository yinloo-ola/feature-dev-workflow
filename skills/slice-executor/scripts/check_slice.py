#!/usr/bin/env python3
"""Deterministic gates for slice execution. Run from inside the slice's worktree.

Modes:
  ready   <plan.md> <n>  — pre-flight: are slice n's depends-on branches merged
                           into the plan's base branch? (run from any checkout)
  verify  <plan.md> <n>  — post-implementation gates, before opening the PR:
                           scope, size, mode, tests. (run from the worktree)

Exit 0 = pass, 1 = fail (reasons printed).
"""
import re
import subprocess
import sys
from pathlib import Path

SLICE_RE = re.compile(r"^### Slice (\d+):")
MODES = {"additive", "migrate", "contract"}
# Files excluded from the size gate and allowed without declaration in `files`
EXCLUDED = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "go.sum", "uv.lock", "Gemfile.lock",
]
EXCLUDED_SUFFIX = (".pb.go", "_pb2.py", ".min.js", ".map")
EXCLUDED_PARTS = ("vendor/", "node_modules/", "dist/", "build/", "generated/")
TEST_RE = re.compile(r"(_test\.go$|_test\.py$|\.test\.(ts|tsx|js|mjs)$|\.spec\.(ts|tsx|js)$|_test\.rs$)")
SIZE_CAP = 350          # changed lines (adds+dels), non-excluded files
ADDITIVE_DEL_CAP = 10   # deletions outside test files allowed in additive mode


def fail(msgs):
    print("FAIL:")
    for m in msgs:
        print(f"  - {m}")
    sys.exit(1)


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def parse_plan(path):
    """Return (base_branch, {num: card}) where card = dict(mode, deps, files, tests)."""
    text = Path(path).read_text()
    base = re.search(r"^Base branch:\s*(\S+)", text, re.M)
    if not base:
        fail([f"{path}: missing 'Base branch:' header"])
    slices, current, field = {}, None, None
    for line in text.splitlines():
        m = SLICE_RE.match(line)
        if m:
            current, field = int(m.group(1)), None
            slices[current] = {"mode": None, "deps": [], "files": [], "tests": []}
            continue
        if current is None:
            continue
        stripped = line.strip()
        m = re.match(r"^- (mode|depends-on|files|tests):(.*)$", stripped)
        if m:
            key, rest = m.group(1), m.group(2).strip()
            field = key if key in ("files", "tests") else None
            c = slices[current]
            if key == "mode":
                c["mode"] = rest
            elif key == "depends-on":
                c["deps"] = [int(d) for d in re.findall(r"\d+", rest)]
            elif key in ("files", "tests") and rest:
                c[key].append(rest)
            continue
        if line.startswith("  - ") and field in ("files", "tests"):
            slices[current][field].append(line[4:].strip())
    return base.group(1), slices


def get_card(plan_path, n):
    base, slices = parse_plan(plan_path)
    if n not in slices:
        fail([f"slice {n} not found in {plan_path}"])
    return base, slices[n], slices


def is_excluded(path):
    name = path.rsplit("/", 1)[-1]
    return (name in EXCLUDED or name.endswith(EXCLUDED_SUFFIX)
            or any(p in path for p in EXCLUDED_PARTS))


def is_testfile(path, tests):
    return path in tests or bool(TEST_RE.search(path))


def cmd_ready(plan_path, n):
    base, card, _ = get_card(plan_path, n)
    if not card["deps"]:
        print(f"OK: slice {n} has no dependencies — ready.")
        return
    feature = Path(plan_path).resolve().parent.name
    blocked = []
    for dep in card["deps"]:
        branch = f"slice/{feature}-{dep}"
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
    print(f"OK: all dependencies of slice {n} merged into {base}.")


def cmd_verify(plan_path, n):
    base, card, _ = get_card(plan_path, n)
    feature = Path(plan_path).resolve().parent.name
    branch = f"slice/{feature}-{n}"

    # working tree must be clean (gates evaluate committed diff)
    rc, out = git("status", "--porcelain")
    if out:
        fail(["working tree is not clean — commit or remove changes first:",
              *[f"    {l}" for l in out.splitlines()]])

    rc, out = git("rev-parse", "--verify", base)
    if rc != 0:
        fail([f"base branch {base!r} not found in this repo"])

    rc, out = git("diff", "--numstat", f"{base}...HEAD")
    if rc != 0:
        fail([f"cannot diff {base}...HEAD: {out}"])
    if not out.strip():
        fail([f"no changes between {base} and HEAD — nothing to verify"])

    errors, changed = [], []
    size = dels_non_test = 0
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        adds, dels, path = parts
        changed.append(path)
        if is_excluded(path):
            continue
        adds = 0 if adds == "-" else int(adds)
        dels = 0 if dels == "-" else int(dels)
        size += adds + dels
        if not is_testfile(path, card["tests"]):
            dels_non_test += dels

    # scope gate
    allowed = set(card["files"]) | set(card["tests"])
    scope_violations = [p for p in changed if p not in allowed and not is_excluded(p)]
    if scope_violations:
        errors.append(f"scope: changed files outside the card's declared files/tests: "
                      f"{scope_violations}")

    # size gate
    if size > SIZE_CAP:
        errors.append(f"size: {size} changed lines (cap {SIZE_CAP}, excluding "
                      "lockfiles/generated) — split the slice or trim the diff")

    # mode gate
    if card["mode"] == "additive" and dels_non_test > ADDITIVE_DEL_CAP:
        errors.append(f"mode: additive slice deletes {dels_non_test} lines outside test "
                      f"files (cap {ADDITIVE_DEL_CAP}) — additive means additions; "
                      "deletions belong to the contract slice")

    # test gate: declared test files must be in the diff; "tests: none" (declared)
    # is honored, an undeclared/empty tests field on a behavior change is not
    if card["tests"] and card["tests"] != ["none"]:
        missing = [t for t in card["tests"] if t not in changed]
        if missing:
            errors.append(f"tests: declared test files not changed in this diff: {missing}")
    elif card["tests"] != ["none"] and card["mode"] in ("additive", "migrate") and not any(
            is_testfile(p, card["tests"]) for p in changed):
        errors.append("tests: behavior-changing slice with no test files declared or changed "
                      "(declare 'tests: none' with justification in the plan if intended)")

    if errors:
        fail([f"slice {n} ({branch}) vs {base}:"] + errors)
    print(f"OK: slice {n} — {len(changed)} files, {size} changed lines; "
          f"scope/size/mode/tests gates passed.")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("ready", "verify"):
        print("usage: check_slice.py ready|verify <plan.md> <slice-number>", file=sys.stderr)
        sys.exit(1)
    mode, plan, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    (cmd_ready if mode == "ready" else cmd_verify)(plan, n)
