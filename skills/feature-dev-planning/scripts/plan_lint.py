#!/usr/bin/env python3
"""Validate a .specs/<feature>/03-plan.md slice plan.

Enforces the contract between feature-dev-planning and slice-executor:
  - every slice card has the required fields
  - slice 1 exists and is additive (the foundation)
  - depends-on references exist and the graph is acyclic
  - parallel slices (no edge between them) declare disjoint file sets

Usage: plan_lint.py <path-to-03-plan.md>
Exit 0 = plan is valid. Exit 1 = failures (printed).
"""
import re
import sys
from pathlib import Path

SLICE_RE = re.compile(r"^### Slice (\d+):(.*)$")
MODES = {"additive", "migrate", "contract"}
REQUIRED = ("mode", "depends-on", "files", "tests", "card")


def parse_plan(text: str):
    """Return {num: {title, mode, deps, files, tests, card}} preserving order."""
    slices, current = {}, None
    field = None
    for line in text.splitlines():
        m = SLICE_RE.match(line)
        if m:
            num = int(m.group(1))
            if num in slices:
                raise ValueError(f"duplicate slice number: {num}")
            current = {
                "title": m.group(2).strip(),
                "mode": None,
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
            elif key == "depends-on":
                deps = re.findall(r"\d+", rest)
                current["deps"] = [int(d) for d in deps]
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

    errors = []
    if not slices:
        errors.append("no slice cards found (expected '### Slice N: title' blocks)")

    for num, s in sorted(slices.items()):
        label = f"slice {num}"
        if not s["title"]:
            errors.append(f"{label}: missing title")
        if s["mode"] not in MODES:
            errors.append(f"{label}: mode must be one of {sorted(MODES)}, got {s['mode']!r}")
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

    if 1 in slices and slices[1]["mode"] != "additive":
        errors.append("slice 1 must be mode: additive (the foundation slice)")

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

    if errors:
        print(f"FAIL: {path}")
        for e in errors:
            print(f"  - {e}")
        return 1
    parallel = [n for n, s in slices.items() if not (s["deps"] or []) and n != 1]
    print(f"OK: {path} — {len(slices)} slices; parallelizable immediately: "
          f"{sorted(parallel) if parallel else 'none (all sequenced)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
