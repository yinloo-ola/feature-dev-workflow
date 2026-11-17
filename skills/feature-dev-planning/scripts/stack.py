#!/usr/bin/env python3
"""Inspect and restack the slice branches of a .specs/<feature>/03-plan.md plan.

Usage: stack.py {status,restack,continue,abort,push} <path-to-03-plan.md> [--dry-run]

status prints each slice branch in stack order with its local/remote SHA and PR.
Foundation only: the mutating commands run the safety checks, record
.git/stack-state.json, and support --dry-run; replay and push are not implemented
yet, so without --dry-run they exit 1 after recording state.
Run from inside the repo checkout. Exit 0 = ok, 1 = refused or not implemented.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SLICE_RE = re.compile(r"^### Slice (\d+):")
MUTATING = ("restack", "continue", "abort", "push")


def die(msg):
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args):
    """Return stripped stdout, or None when git exits non-zero."""
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def parse_plan(path):
    """Return (base_branch, feature, order) — order is the linearized stack."""
    text = Path(path).read_text()
    base = re.search(r"^Base branch:\s*(\S+)", text, re.M)
    if not base:
        die(f"{path}: missing 'Base branch:' header — add it to the plan")
    m = re.search(r"^# Plan:\s*(\S+)", text, re.M)
    feature = m.group(1) if m else Path(path).resolve().parent.name
    deps, cur = {}, None
    for line in text.splitlines():
        m = SLICE_RE.match(line)
        if m:
            cur = int(m.group(1))
            deps[cur] = []
        elif cur is not None and line.startswith("- depends-on:"):
            deps[cur] = [int(d) for d in re.findall(r"\d+", line)]
    if not deps:
        die(f"{path}: no '### Slice N:' cards found — run plan_lint.py on the plan")
    for n, ds in deps.items():
        bad = [d for d in ds if d >= n or d not in deps]
        if bad:
            die(f"{path}: slice {n} depends-on {bad} — edges must point to earlier, "
                "existing slices; fix the plan (plan_lint.py reports this)")
    # Every edge points backward, so ascending slice number IS the topological
    # order with a numeric tiebreak — no graph walk needed.
    return base.group(1), feature, sorted(deps)


def capture(plan, base, feature, order, start):
    """Snapshot the stack: the record continue/abort/push work from."""
    def sha(ref):
        return git("rev-parse", "--verify", "-q", ref)
    return {
        "plan_path": str(Path(plan).resolve()),
        "base": base,
        "order": order,
        "branches": {str(n): {
            "orig_sha": sha(f"refs/heads/slice/{feature}-{n}"),
            "remote_sha": sha(f"refs/remotes/origin/slice/{feature}-{n}"),
        } for n in order},
        "position": {"next_branch": f"slice/{feature}-{start}", "applied": []},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def safety(gitdir, base, feature, order):
    """Refuse to mutate from an unsafe checkout; each message says how to fix it."""
    lock = gitdir / "index.lock"
    if lock.exists():
        die(f"{lock} exists — another git process is running; wait for it to "
            f"finish, or if it crashed remove the lock: rm {lock}")
    if (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists():
        die("unfinished rebase — finish it (git rebase --continue) or cancel it "
            "(git rebase --abort), then re-run")
    if git("status", "--porcelain"):
        die("worktree is dirty — commit or stash (git stash -u), then re-run")
    head = git("rev-parse", "--abbrev-ref", "HEAD")
    if head != base and head not in [f"slice/{feature}-{n}" for n in order]:
        die(f"HEAD is on {head!r}, not {base!r} or a slice branch of {feature} — "
            f"git switch {base}, then re-run")


def pr_info(branch, parent):
    r = subprocess.run(["gh", "pr", "view", branch, "--json",
                        "number,headRefName,baseRefName,state"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return "no PR"
    pr = json.loads(r.stdout)
    out = f"PR #{pr['number']} {pr['state'].lower()}"
    if pr["state"] == "OPEN" and pr["baseRefName"] != parent:
        out += f" (PR base {pr['baseRefName']}, stack parent {parent})"
    return out


def cmd_status(plan, base, feature, order):
    gh = shutil.which("gh")
    print(f"stack {feature} on {base}: " + " -> ".join(map(str, order)))
    if not gh:
        print("gh absent — order from plan only")
    snap, parent = capture(plan, base, feature, order, order[0])["branches"], base
    for n in order:
        branch = f"slice/{feature}-{n}"
        local, remote = snap[str(n)]["orig_sha"], snap[str(n)]["remote_sha"]
        line = f"{n}  {branch}  local {local[:7] if local else 'missing'}  "
        line += f"remote {remote[:7]}" if remote else "not pushed"
        if gh:
            line += "  " + pr_info(branch, parent)
        print(line)
        parent = branch


def plan_lines(cmd, state, feature, start):
    """The actions `cmd` would take, plus its cost line — what --dry-run prints."""
    order, base, shas = state["order"], state["base"], state["branches"]
    todo = order[order.index(start):] if cmd == "restack" else order
    lines = []
    for n in todo:
        branch, s = f"slice/{feature}-{n}", shas[str(n)]
        i = order.index(n)
        parent = f"slice/{feature}-{order[i - 1]}" if i else base
        if cmd == "restack":
            lines.append(f"rebase {branch} onto {parent}")
        elif cmd == "push":
            lines.append(f"push {branch} --force-with-lease=refs/heads/{branch}:"
                         f"{s['remote_sha'] or ''}")
        elif cmd == "abort" and s["orig_sha"]:
            lines.append(f"restore {branch} to {s['orig_sha']}")
    if cmd == "continue":
        lines.append(f"continue the restack at {state['position']['next_branch']}")
    if cmd == "abort":
        lines.append("remove stack-state.json")
    pushed = sum(1 for n in todo if shas[str(n)]["remote_sha"])
    lines.append(f"cost: {len(todo)} branch(es) in play; {pushed} already pushed — "
                 "a moved head needs a force-push and a re-review of its PR")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status",) + MUTATING:
        sp = sub.add_parser(name)
        sp.add_argument("plan", help="path to 03-plan.md")
        sp.add_argument("--dry-run", action="store_true", help="mutate nothing")
        if name == "restack":
            sp.add_argument("--from", dest="start", type=int, help="first slice")
    args = ap.parse_args(argv)
    if not Path(args.plan).is_file():
        die(f"{args.plan} not found — pass the path to 03-plan.md")
    base, feature, order = parse_plan(args.plan)
    gitdir = git("rev-parse", "--absolute-git-dir")
    if not gitdir:
        die("not inside a git repository — cd into the repo checkout, then re-run")
    if args.cmd == "status":  # read-only, so --dry-run changes nothing
        cmd_status(args.plan, base, feature, order)
        return 0
    start = getattr(args, "start", None) or order[0]
    if start not in order:
        die(f"--from {start}: no such slice — the plan has slices {order}")
    safety(Path(gitdir), base, feature, order)
    sfile = Path(gitdir) / "stack-state.json"
    # An existing state file holds the original SHAs abort restores: never overwrite.
    state = (json.loads(sfile.read_text()) if sfile.exists()
             else capture(args.plan, base, feature, order, start))
    if args.dry_run:
        print(f"dry-run: {args.cmd} — nothing is changed")
        for line in plan_lines(args.cmd, state, feature, start):
            print(f"  {line}")
        return 0
    if not sfile.exists():
        sfile.write_text(json.dumps(state, indent=2) + "\n")
    print(f"state recorded: {sfile}")
    print(f"{args.cmd}: not implemented yet — no branch was changed; use --dry-run "
          f"to see the plan, or remove {sfile} to reset", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
