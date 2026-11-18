#!/usr/bin/env python3
"""Validate a .specs/<feature>/03-plan.md slice plan.

Enforces the contract between feature-dev-planning and slice-executor:
  - every slice card has the required fields
  - slice 1 exists and keeps deletes-existing: no (the foundation)
  - deletes-existing is 'no' or 'yes — <what and why>'; mode: is legacy, advisory
  - depends-on references exist and the graph is acyclic
  - parallel slices (no edge between them) declare disjoint file sets
  - WARN (never fail): plan shape (fan-out ratio, longest chain) and card identifiers
    that appear in test files the card does not declare

Usage: plan_lint.py <path-to-03-plan.md>
Exit 0 = plan is valid. Exit 1 = failures (printed).
"""
import re
import subprocess
import sys
from pathlib import Path

SLICE_RE = re.compile(r"^### Slice (\d+):(.*)$")
DELIVER_RE = re.compile(r"^deliver:\s*(parallel|stacked)$")  # matched per line, same grammar as check_slice
SLICE_HEAD_RE = re.compile(r"^###\s+slice\b", re.I)  # anything like a slice heading
DEPS_RE = re.compile(r"^\[([^\]]*)\]$")
DELETES_RE = re.compile(r"^yes\s*—\s*(\S.*)$")  # captures the reason text
# must stay byte-identical to TEST_RE in slice-executor/scripts/check_slice.py (a test enforces it)
TEST_RE = re.compile(r"(^|/)(test_[^/]*\.py|conftest\.py)$|_test\.(go|py|rs)$|\.(test|spec)\.(ts|tsx|js|jsx|mjs)$|(^|/)(tests?|__tests__)/|(Test|Tests)\.(java|kt)$|_spec\.rb$")
SKIP_DIRS = {"vendor", "node_modules", "dist", "build", "generated"}
IDENT_RE = re.compile(r"`([^`\s]{4,})`")
GREP_CAP = 200
REQUIRED = ("mode", "depends-on", "files", "tests", "card")


def deliver_line(text: str):
    """The first column-0 'deliver:' line, rstripped, or None (same scan as check_slice)."""
    return next((l.rstrip() for l in text.splitlines() if l.startswith("deliver:")), None)

def parse_deliver(text: str) -> str:
    """Plan header 'deliver: parallel | stacked'; parallel when omitted or invalid (main FAILs it)."""
    m = DELIVER_RE.match(deliver_line(text) or "")
    return m.group(1) if m else "parallel"


def shape(slices):
    """(fan-out ratio or None, longest chain as card numbers). Slice 1 is the foundation:
    excluded as a node, and a dependency on it does not count."""
    eff = {n: [d for d in s["deps"] or [] if d != 1] for n, s in slices.items() if n != 1}
    chain = {}  # n -> longest chain ending at n (earlier slices only: deps are validated)
    for n in sorted(eff):
        chain[n] = max(([chain[d] for d in eff[n] if d in chain] or [[]]), key=len) + [n]
    longest = max(chain.values(), key=len, default=[])
    roots = sum(1 for d in eff.values() if not d)
    return (roots / len(eff) if eff else None), longest

def undeclared_test_warnings(plan_dir, slices):
    """(warnings, note). Cards' backticked identifiers vs test files the card doesn't declare."""
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=plan_dir,
                              capture_output=True, text=True, check=True).stdout.strip()
        files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root,
                               capture_output=True, text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return [], "identifier grep skipped (not a git repo)"
    tests = {}  # path -> text, read once
    for f in files:
        if TEST_RE.search(f) and not SKIP_DIRS & set(f.split("/")):
            try:
                tests[f] = (Path(root) / f).read_text(errors="ignore")
            except OSError:
                pass
    warns, greps = [], 0
    for num, s in sorted(slices.items()):
        declared = set(s["files"]) | set(s["tests"])
        for ident in dict.fromkeys(IDENT_RE.findall(" ".join(s["card"]))):
            if "/" in ident:
                continue
            if greps >= GREP_CAP:
                return warns, f"identifier grep capped at {GREP_CAP}"
            greps += 1  # one search of the test corpus per identifier
            pat = re.compile(r"(?<!\w)" + re.escape(ident) + r"(?!\w)")
            hit = next((f for f, t in tests.items() if f not in declared and pat.search(t)), None)
            if hit:
                warns.append(f"slice {num}: identifier {ident} appears in undeclared test file "
                             f"{hit} — add {hit} to tests or note the required update")
    return warns, ""

def parse_plan(text: str):
    """Return {num: {title, mode, deps, files, tests, card}} preserving order."""
    if not re.search(r"^Base branch:\s*\S+", text, re.M):
        raise ValueError("missing 'Base branch:' header — slice-executor requires it")
    slices, current = {}, None
    field = None
    for line in text.splitlines():
        m = SLICE_RE.match(line)
        if not m and SLICE_HEAD_RE.match(line):
            raise ValueError(f"malformed slice heading: {line}")
        if m:
            num = int(m.group(1))
            if num in slices:
                raise ValueError(f"duplicate slice number: {num}")
            current = {
                "title": m.group(2).strip(),
                "mode": None,
                "deletes": "no",  # raw deletes-existing value; default when omitted
                "reason": None,  # text after 'yes —'
                "deps": None,
                "files": [],
                "tests": [],
                "card": [],
            }
            slices[num] = current
            field = None
            continue
        if current is None:
            continue
        m = re.match(r"^- (\w[\w-]*):(.*)$", line)
        if m:
            key, rest = m.group(1), m.group(2).strip()
            field = key if key in ("files", "tests", "card") else None
            if key == "mode":
                current["mode"] = rest
            elif key == "deletes-existing":
                m = DELETES_RE.match(rest)
                current["deletes"], current["reason"] = rest, m and m.group(1)
            elif key == "depends-on":
                b = DEPS_RE.match(re.sub(r"\s*#.*$", "", rest))
                if not b:
                    raise ValueError(f"slice {num}: depends-on must be a bracketed list, got {rest!r}")
                current["deps"] = [int(d) for d in re.findall(r"\d+", b.group(1))]
            elif key == "files" and rest:
                current["files"].append(rest)
            elif key == "tests":
                if rest and rest != "none":
                    current["tests"].append(rest)
                elif rest == "none":
                    current["tests"] = ["none"]
            continue
        if field and line.lstrip().startswith("- "):
            item = line.lstrip()[2:].strip()
            if field in ("files", "tests"):
                current[field].append(item)
            elif field == "card":
                current["card"].append(item)
        elif field == "card" and line.startswith(("    ", "\t")):
            current["card"].append(line.strip())
    return slices


def main():
    if len(sys.argv) != 2:
        print("usage: plan_lint.py <path-to-03-plan.md>", file=sys.stderr)
        return 1
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 1
    try:
        slices = parse_plan(path.read_text())
    except ValueError as e:
        print(f"FAIL: {e}")
        return 1

    text = path.read_text()
    deliver = parse_deliver(text)
    errors = []
    warnings = []
    raw = deliver_line(text)
    if raw is not None and not DELIVER_RE.match(raw):
        errors.append(f"deliver must be parallel or stacked, got {raw[len('deliver:'):].strip()!r}")
    if not slices:
        errors.append("no slice cards found (expected '### Slice N: title' blocks)")

    for num, s in sorted(slices.items()):
        label = f"slice {num}"
        if not s["title"]:
            errors.append(f"{label}: missing title")
        if s["mode"] is not None:
            warnings.append(f"{label}: legacy mode field — v2 plans use deletes-existing")
        if s["deletes"] != "no" and not s["reason"]:
            errors.append(f"{label}: deletes-existing must be 'no' or 'yes — <what and why>' "
                          f"(reason text required after 'yes —'), got {s['deletes']!r}")
        if s["deps"] is None:
            errors.append(f"{label}: missing depends-on (use '[]' if parallelizable)")
        if not s["files"]:
            errors.append(f"{label}: files must list at least one non-test file")
        if not s["tests"]:
            errors.append(f"{label}: tests must list test files or be 'none'")
        if s["tests"] == ["none"] and not any(s["card"]):
            errors.append(f"{label}: 'tests: none' requires a justification in the card")
        for dep in s["deps"] or []:
            if dep not in slices:
                errors.append(f"{label}: depends-on {dep} does not exist")
            elif dep >= num:
                errors.append(f"{label}: depends-on {dep} must reference an earlier slice")
        if not any(s["card"]):
            errors.append(f"{label}: card is empty (fails the cold-start test)")

    if 1 in slices and slices[1]["deletes"] != "no":
        errors.append("slice 1 is the foundation — deletes-existing must be no")

    # Note: deps must reference earlier slices (enforced above), which makes
    # cycles structurally impossible — no separate cycle check needed.

    # Disjoint files across parallel slices
    nums = sorted(slices)
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            sa, sb = slices[a], slices[b]
            if b in (sa["deps"] or []) or a in (sb["deps"] or []):
                continue  # sequenced: overlap allowed
            overlap = set(sa["files"]) & set(sb["files"])
            if overlap:
                errors.append(
                    f"slices {a} and {b} share files {sorted(overlap)} "
                    "without a dependency edge — add 'depends-on' or re-slice"
                )

    if deliver == "stacked":  # linear stack: each slice's highest dependency is the one before it
        for prev, n in zip([None] + nums, nums):
            deps = slices[n]["deps"] or []
            if prev is None and deps:
                errors.append(f"stacked plan: slice {n} is first and must have depends-on [] "
                              "(stacked branches form one linear chain)")
            elif prev is not None and max(deps, default=None) != prev:
                errors.append(f"stacked plan: slice {n} must depend on slice {prev} "
                              "(stacked branches form one linear chain)")

    ratio, chain, note = None, [], ""
    if not errors:
        ratio, chain = shape(slices)
        order = " → ".join(map(str, chain))
        if deliver == "stacked":
            if chain:
                print(f"chain order: {order}")
        elif ratio is not None and (ratio < 0.3 or len(chain) > 5):
            warnings.append(f"pipeline, not parallel plan (fan-out {ratio:.0%}, longest chain "
                            f"{len(chain)}) — collapse sequential cards or declare deliver: "
                            f"stacked (chain: {order})")
        w, note = undeclared_test_warnings(path.resolve().parent, slices)
        warnings += w
    for w in warnings:
        print(f"WARN: {w}")
    if errors:
        print(f"FAIL: {path}")
        for e in errors:
            print(f"  - {e}")
        return 1
    parallel = [n for n, s in slices.items() if not (s["deps"] or []) and n != 1]
    n_label = f"{len(slices)} slice" + ("" if len(slices) == 1 else "s")
    n_del = sum(1 for s in slices.values() if s["reason"])
    print(f"OK: {path} — {n_label}; deliver: {deliver}; "
          f"deletes-existing: {n_del}; parallelizable immediately: "
          f"{sorted(parallel) if parallel else 'none (all sequenced)'}; "
          f"fan-out {'n/a' if ratio is None else format(ratio, '.0%')}, longest chain {len(chain)}"
          + (f"; {note}" if note else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
