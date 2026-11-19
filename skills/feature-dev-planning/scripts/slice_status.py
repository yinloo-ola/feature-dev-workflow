#!/usr/bin/env python3
"""Show every slice of a .specs/<feature>/03-plan.md plan and its current state.

Usage: slice_status.py <path-to-03-plan.md> [--no-pr]

Prints one line per slice (number, state, detail) in plan order, then a summary line
last, so `tail -1` answers "what can I dispatch now". State is derived at read time by
joining the plan, git, claims/ and (when gh is present) the slice's PR — first match
wins: merged, in review, in flight, blocked, ready, not started.
The detail column names the PR, the worktree holding the claim, the blocking slice
numbers, or a stale claim with the rmdir that clears it.
Nothing is written and no ref moves: no fetch, no checkout, no worktree, no state file,
so re-running is trivially safe.
--no-pr skips gh entirely; so does a checkout without gh, which prints one line saying
PR state is unavailable and then judges merged by the local branch alone.
Run from inside the repo checkout. Exit 0 = printed, 1 = refused.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack  # noqa: E402  sibling module: git(), die(), parse_plan(), pr_state()

SLICE_RE = re.compile(r"^### Slice (\d+):")
DEPS_RE = re.compile(r"^\[([^\]]*)\]$")  # same grammar as stack/plan_lint/check_slice
STATES = ("merged", "in review", "in flight", "blocked", "ready", "not started")
NO_PR = "PR state unavailable — merged is judged by local branch only"


def parse_cards(path):
    """{num: {'deps': [int], 'amended': bool}} — the per-card facts stack.parse_plan omits."""
    cards, cur = {}, None
    for line in Path(path).read_text().splitlines():
        m = SLICE_RE.match(line)
        if m:
            cur = int(m.group(1))
            cards[cur] = {"deps": [], "amended": False}
            continue
        if cur is None:
            continue
        if line.strip().startswith("amended:"):  # the card records its own rewrite
            cards[cur]["amended"] = True
        if line.startswith("- depends-on:"):  # column 0: indented prose is not a field
            rest = re.sub(r"\s*#.*$", "", line[len("- depends-on:"):]).strip()
            b = DEPS_RE.match(rest)
            if not b:
                stack.die(f"{path}: slice {cur}: depends-on must be a bracketed list, got {rest!r}")
            cards[cur]["deps"] = [int(d) for d in re.findall(r"\d+", b.group(1))]
    return cards


def worktrees():
    """{branch: worktree path} — one `git worktree list --porcelain` call, no writes."""
    trees = {}
    for block in (stack.git("worktree", "list", "--porcelain") or "").split("\n\n"):
        f = dict(x.split(" ", 1) for x in block.splitlines() if " " in x)
        branch = f.get("branch", "").removeprefix("refs/heads/")
        if branch and "worktree" in f:
            trees[branch] = f["worktree"]
    return trees


def resolve(pr, base, branch, claimed, states, deps):
    """One slice's state: RFC §1's six-row table, first match wins."""
    if (pr and pr["state"] == "MERGED") or stack.git(
            "merge-base", "--is-ancestor", branch, base) is not None:
        return "merged"
    if pr and pr["state"] == "OPEN":
        return "in review"
    if claimed:  # a claims/slice-<n> directory, or a worktree holding the branch
        return "in flight"
    if any(states.get(d) != "merged" for d in deps):
        return "blocked"
    if not deps or all(states.get(d) == "merged" for d in deps):
        return "ready"
    # Unreachable while deps point backward: every dep is merged by the line above. Kept
    # because the six-row table names it, and a plan that lies must not read "ready".
    return "not started"


def detail(pr, claim, at, stale, waits, amended):
    """The detail column: the PR, else the claim, else what a blocked slice waits on.

    Staleness and amendment are notes appended to that, never excluded by it: they are
    facts about the claim directory, so a merged slice whose claim was never cleared
    prints both `PR #<n>` and the rmdir that ends the leftover claim.
    """
    if pr:
        out = f"PR #{pr['number']}"
    elif at:
        out = f"claimed at {at}"
    elif claim.exists() and not stale:
        out = "claimed, no worktree"
    else:
        out = ""
    notes = []
    if stale:  # the claim is the lie; nothing else about this row is as actionable
        notes.append(f"stale claim — clear with rmdir {claim}")
    elif not out and waits:
        notes.append("waits on " + ", ".join(map(str, waits)))
    if amended:
        notes.append("amended since claim")
    return "; ".join(([out] if out else []) + notes)


def summary(rows):
    """The last line: dispatchable slices, what waits, what to clean up."""
    ready = [f"{r['n']}" for r in rows if r["state"] == "ready"]
    flight = [f"{r['n']}" for r in rows if r["state"] == "in flight"]
    blocked = [f"{r['n']} (on {', '.join(map(str, r['waits']))})"
               for r in rows if r["state"] == "blocked"]
    stale = sum(1 for r in rows if r["stale"])
    parts = []
    if ready:
        parts.append("ready: " + ", ".join(ready))
    elif not flight:  # nothing can be dispatched: say so, do not leave the section blank
        parts.append("ready: none")
    if blocked:
        parts.append("blocked: " + ", ".join(blocked))
    if flight:
        parts.append("in flight: " + ", ".join(flight))
    if stale:
        parts.append(f"stale claims: {stale}")
    return "  ".join(parts)


def cmd_status(plan, no_pr):
    """Print one line per slice in plan order, then the summary line. Read-only."""
    base, feature, order, _deliver = stack.parse_plan(plan)
    if stack.git("rev-parse", "--absolute-git-dir") is None:
        stack.die("not inside a git repository — cd into the repo checkout, then re-run")
    cards, trees = parse_cards(plan), worktrees()
    plan_dir, plan_mtime = Path(plan).parent, Path(plan).stat().st_mtime
    gh = not no_pr and stack.shutil.which("gh") is not None
    if not gh:
        print(NO_PR)
    states, rows = {}, []
    for n in order:
        branch = f"slice/{feature}-{n}"
        claim = plan_dir / "claims" / f"slice-{n}"
        pr = stack.pr_state(branch) if gh else None  # one call per slice, in order
        has_branch = stack.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}") is not None
        at = trees.get(branch)
        deps = cards[n]["deps"]
        state = resolve(pr, base, branch, claim.exists() or at, states, deps)
        states[n] = state
        waits = [d for d in deps if states.get(d) != "merged"]
        # Stale is purely local: a claim dir with nothing under it any more. PR state is
        # NOT a qualifier — a merged (or in-review) slice whose claim was never cleared is
        # exactly the case this detail exists to surface. The PR is already the `state`
        # column's job, so it must not suppress the cleanup hint or the summary count.
        stale = claim.exists() and not has_branch and not at
        amended = (cards[n]["amended"] and claim.exists()
                   and claim.stat().st_mtime < plan_mtime)
        rows.append({"n": n, "state": state, "stale": stale, "waits": waits,
                     "detail": detail(pr, claim, at, stale, waits, amended)})
        print(f"{n:<3} {state:<12} {rows[-1]['detail']}")
    print(summary(rows))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plan", help="path to 03-plan.md")
    ap.add_argument("--no-pr", action="store_true",
                    help="never ask gh: judge merged by the local branch alone")
    args = ap.parse_args(argv)
    if not Path(args.plan).is_file():
        stack.die(f"{args.plan} not found — pass the path to 03-plan.md")
    cmd_status(args.plan, args.no_pr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
