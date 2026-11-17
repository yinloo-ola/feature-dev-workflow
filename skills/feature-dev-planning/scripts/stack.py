#!/usr/bin/env python3
"""Inspect and restack the slice branches of a .specs/<feature>/03-plan.md plan.

Usage: stack.py {status,restack,continue,abort,push} <path-to-03-plan.md> [--dry-run]

status prints each slice branch in stack order with its local/remote SHA and PR.
restack --from n replays branches n..N onto their parents; on a conflict it stops,
writes conflict-report.md beside the plan and leaves the rebase in progress.
continue finishes the stopped branch (tree resolved + tests green) and carries on;
abort restores every branch to its recorded SHA. push prints the cost report, asks to
proceed (--yes skips), then force-pushes each changed branch with a lease on the remote
SHA captured at restack time; push --from n restacks first. Every mutating command
supports --dry-run.
Exit 1 also means a restack stopped (conflict or red tests): read conflict-report.md.
Run from inside the repo checkout. Exit 0 = ok, 1 = refused.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SLICE_RE = re.compile(r"^### Slice (\d+):")
MUTATING = ("restack", "continue", "abort", "push")
RESUMING = ("continue", "abort")  # run mid-conflict: tree is dirty, a rebase is open
IMPORT_RE = re.compile(r"\s*(import\s|from\s+\S+\s+import\s|#include\b|use\s|"
                       r"(const|let|var)\s.*=\s*require\()")


def die(msg):
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(1)


def run(*args, env=None):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          errors="replace", env=env)


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


def rebasing(gitdir):
    return (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists()


def safety(gitdir, base, feature, order, resuming=False):
    """Refuse to mutate from an unsafe checkout; each message says how to fix it.

    resuming (continue/abort) skips the dirty-tree and open-rebase refusals: mid-conflict
    both are true by definition. Untracked files never count as dirty.
    """
    lock = gitdir / "index.lock"
    if lock.exists():
        die(f"{lock} exists — another git process is running; wait for it to "
            f"finish, or if it crashed remove the lock: rm {lock}")
    if rebasing(gitdir) and not resuming:
        die("unfinished rebase — finish it (git rebase --continue) or cancel it "
            "(git rebase --abort), then re-run")
    if not resuming and git("status", "--porcelain", "--untracked-files=no"):
        die("worktree is dirty — commit or stash (git stash -u), then re-run")
    head = git("rev-parse", "--abbrev-ref", "HEAD")
    for d in ("rebase-merge", "rebase-apply"):  # mid-rebase HEAD is detached
        if (gitdir / d / "head-name").exists():
            head = (gitdir / d / "head-name").read_text().strip().removeprefix("refs/heads/")
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


def pr_state(branch):
    """{'number', 'state'} of the branch's PR, or None (gh absent or no PR)."""
    if not shutil.which("gh"):
        return None
    r = subprocess.run(["gh", "pr", "view", branch, "--json", "number,state"],
                       capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None

def push_skip(branch, s, pr):
    """Why branch needs no push, or None."""
    tip = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if not tip:
        return "no local branch"
    if pr and pr["state"] == "MERGED":
        return "PR merged"
    if tip == s["remote_sha"]:
        return "tip equals remote"

def cost_report(state, feature, prs):
    """What a push costs: restacked range, patches range-diff says changed, PRs that move."""
    order, shas = state["order"], state["branches"]
    done = [int(b.rsplit("-", 1)[1]) for b in state["position"]["applied"]]
    out = ["cost report:", "  branches restacked: "
           + (f"{min(done)}..{max(done)}" if done else "none")]
    moving = []
    for n in order:
        branch, s = f"slice/{feature}-{n}", shas[str(n)]
        if not s["orig_sha"]:
            continue
        parent, old = parent_of(state, feature, n)
        rd = git("range-diff", f"{old}..{s['orig_sha']}", f"{git('rev-parse', parent)}..{branch}") or ""
        marks = re.findall(r"^\s*\S+:\s+\S+\s+([=!<>])\s+\S+:", rd, re.M)
        changed = max(marks.count("!") + marks.count("<"), marks.count("!") + marks.count(">"))
        out.append(f"  {branch}: {changed} patch(es) changed")
        pr = prs.get(branch)
        if pr and pr["state"] == "OPEN" and not push_skip(branch, s, pr):
            moving.append(f"#{pr['number']} ({branch})")
    out += ["  PRs whose heads will move: " + (", ".join(moving) or "none (or gh absent)"),
            "  reviewers of moved heads must re-review"]
    return out

def plan_lines(cmd, state, feature, start):
    """The actions `cmd` would take, plus its cost line — what --dry-run prints."""
    order, base, shas = state["order"], state["base"], state["branches"]
    todo = order[order.index(start):] if cmd == "restack" else order
    lines = []
    prs = {}
    for n in todo:
        branch, s = f"slice/{feature}-{n}", shas[str(n)]
        i = order.index(n)
        parent = f"slice/{feature}-{order[i - 1]}" if i else base
        if cmd == "restack":
            lines.append(f"rebase {branch} onto {parent}")
        elif cmd == "push":
            prs[branch] = pr_state(branch)
            why = push_skip(branch, s, prs[branch])
            lines.append(f"push {branch} --force-with-lease=refs/heads/{branch}:"
                         f"{s['remote_sha'] or ''}")
            if why:
                lines[-1] += f"  [skip: {why}]"
        elif cmd == "abort" and s["orig_sha"]:
            lines.append(f"restore {branch} to {s['orig_sha']}")
    if cmd == "continue":
        nxt = state["position"]["next_branch"]
        lines.append(f"continue the restack at {nxt}" if nxt else "nothing to continue")
    if cmd == "abort":
        lines.append("remove stack-state.json")
    pushed = sum(1 for n in todo if shas[str(n)]["remote_sha"])
    lines.append(f"cost: {len(todo)} branch(es) in play; {pushed} already pushed — "
                 "a moved head needs a force-push and a re-review of its PR")
    if cmd == "push":
        lines += cost_report(state, feature, prs)
    return lines


def save(sfile, state):
    sfile.write_text(json.dumps(state, indent=2) + "\n")


def parent_of(state, feature, n):
    """(parent ref, old parent tip) for slice n: nearest earlier existing branch, else base."""
    order, shas = state["order"], state["branches"]
    for m in reversed(order[:order.index(n)]):
        if shas[str(m)]["orig_sha"]:
            return f"slice/{feature}-{m}", shas[str(m)]["orig_sha"]
    return state["base"], git("merge-base", state["base"], shas[str(n)]["orig_sha"])


def discover_test_cmd(root):
    """First of: Makefile test target, package.json test script, go vet + go test."""
    mk, pkg = root / "Makefile", root / "package.json"
    if mk.is_file() and re.search(r"^test:", mk.read_text(), re.M):
        return ["make", "test"]
    if pkg.is_file() and "test" in json.loads(pkg.read_text()).get("scripts", {}):
        return ["npm", "test"]
    if (root / "go.mod").is_file():
        return ["sh", "-c", "go vet ./... && go test ./..."]
    return None


def conflict_hunks(base, ours, theirs):
    """Overlapping or adjacent change regions of the two sides, in base line numbers."""
    def changes(side):
        sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
        return [o for o in sm.get_opcodes() if o[0] != "equal"]
    return [(a, b) for a in changes(ours) for b in changes(theirs)
            if a[1] <= b[2] and b[1] <= a[2]]


def classify(base, ours, theirs, a, b):
    o, t = ours[a[3]:a[4]], theirs[b[3]:b[4]]
    ws = lambda ls: "".join("".join(ls).split())  # noqa: E731
    if ws(o) == ws(t) or ws(o) == ws(base[a[1]:a[2]]) or ws(t) == ws(base[b[1]:b[2]]):
        return "auto-resolvable (whitespace-only)"
    if o and t and all(IMPORT_RE.match(x) for x in o + t):
        return "likely mechanical (both add imports)"
    if not o or not t:
        return "modify/delete — decision required"
    return "likely semantic"


def conflict_report(state, feature, n, plan_path, script):
    """Build the seven-section brief for the conflict the rebase just stopped on."""
    branch = f"slice/{feature}-{n}"
    parent, old_tip = parent_of(state, feature, n)
    parent_tip = git("rev-parse", parent)
    orig = state["branches"][str(n)]["orig_sha"]
    sha = git("rev-parse", "REBASE_HEAD") or "?"
    subject, message = git("log", "-1", "--format=%s", sha), git("log", "-1", "--format=%B", sha)
    text = Path(plan_path).read_text()
    m = re.search(rf"^### Slice {n}:.*?(?=^### |\Z)", text, re.M | re.S)
    card = m.group(0).rstrip() if m else f"(no card found for slice {n} in {plan_path})"
    cited = sorted(set(re.findall(r"\bB\d+\b", card)), key=lambda b: int(b[1:]))
    art = Path(plan_path).parent / ".stack-conflict" / str(n)
    art.mkdir(parents=True, exist_ok=True)
    files = (git("diff", "--name-only", "--diff-filter=U") or "").splitlines()
    out = [f"# Conflict report — slice {n}", "",
           "## 1. Position", f"- branch: {branch} (slice {n})",
           f"- commit being applied: {sha[:7]} {subject}", f"- parent branch: {parent}", "",
           "## 2. Incoming intent", "Commit message:", "```", message, "```",
           f"Slice card (from {plan_path}):", "```", card, "```",
           f"RFC Behaviors cited by the card: {', '.join(cited) or 'none'}", "",
           f"## 3. Underneath intent — {parent} changes since the old parent tip",
           "```", git("log", "--oneline", f"{old_tip}..{parent_tip}") or "(parent unchanged)",
           "```", "", "## 4. Conflicted files"]
    classes = []
    for f in files:
        seq = len(list(art.glob("*-base-*"))) + 1
        side = {}
        for stage, role in ((1, "base"), (2, "ours"), (3, "theirs")):
            r = run("show", f":{stage}:{f}")
            side[role] = r.stdout if r.returncode == 0 else None
            if side[role] is not None:
                (art / f"{seq}-{role}-{f.replace('/', '__')}").write_text(side[role])
        base_l, ours_l, theirs_l = (None if side[k] is None else side[k].splitlines()
                                    for k in ("base", "ours", "theirs"))
        out += [f"### {f}", f"Versions saved under {art}/{seq}-{{base,ours,theirs}}-*"
                + ("" if None not in side.values() else
                   f" (absent: {', '.join(k for k, v in side.items() if v is None)})")]
        for title, x, y in (("parent-side diff (base -> ours)", base_l, ours_l),
                            ("incoming diff (base -> theirs)", base_l, theirs_l)):
            diff = list(difflib.unified_diff(x or [], y or [], "base", title, lineterm=""))
            out += [f"{title}:", "```diff", *diff, "```"]
        if ours_l is None or theirs_l is None:
            classes.append(f"- {f}: modify/delete — decision required")
        else:
            for a, b in conflict_hunks(base_l or [], ours_l, theirs_l):
                lo, hi = min(a[1], b[1]) + 1, max(a[2], b[2])
                classes.append(f"- {f} base lines {lo}-{hi}: "
                               f"{classify(base_l or [], ours_l, theirs_l, a, b)}")
    if git("rev-parse", "HEAD") == parent_tip:  # nothing applied yet: range-diff rejects an empty range
        rd, marks = "", ["<"] * int(git("rev-list", "--count", f"{old_tip}..{orig}") or 0)
    else:
        rd = git("range-diff", f"{old_tip}..{orig}", f"{parent_tip}..HEAD") or ""
        marks = re.findall(r"^\s*\S+:\s+\S+\s+([=!<>])\s+\S+:", rd, re.M)
    cmd = f"python3 {script} {{}} {plan_path}"
    out += ["", "## 5. Hunk classification", *(classes or ["- no hunk-level overlap found"]), "",
            "## 6. Blast radius (git range-diff, original vs rebased-so-far)",
            f"{marks.count('=')} identical, {marks.count('!')} changed, "
            f"{marks.count('<')} not yet applied, {marks.count('>')} new", "```", rd, "```", "",
            "## 7. Decision rules and next commands",
            "- mechanical: reconcile keeping both intents, and note it against the card.",
            "- semantic: do not resolve; escalate to the amendment table (SKILL.md fan-out).",
            "", "Next:", "1. edit the conflicted files listed in section 4",
            "2. git add <the files>", f"3. {cmd.format('continue')}",
            f"(to give up: {cmd.format('abort')})"]
    return "\n".join(out) + "\n"


def stop(state, feature, n, plan_path):
    """Stop the run: brief + recorded position, rebase left in progress."""
    report = Path(plan_path).parent / "conflict-report.md"
    report.write_text(conflict_report(state, feature, n, plan_path,
                                      Path(__file__).resolve()))
    print(f"CONFLICT: restack stopped at slice {n}; the rebase is left in progress.\n"
          f"Read {report}, resolve, git add, then stack.py continue (or stack.py abort).",
          file=sys.stderr)
    return 1


def replay(state, feature, sfile, gitdir, todo):
    """Rebase each branch in todo onto its parent's new tip, recording progress."""
    for n in todo:
        branch = f"slice/{feature}-{n}"
        if not state["branches"][str(n)]["orig_sha"]:
            print(f"skip {branch}: no local branch")
            continue
        safety(gitdir, state["base"], feature, state["order"])
        parent, old_tip = parent_of(state, feature, n)
        state["position"]["next_branch"] = branch
        save(sfile, state)
        r = run("rebase", "--onto", parent, old_tip, branch)
        if r.returncode:
            if git("diff", "--name-only", "--diff-filter=U") and rebasing(gitdir):
                return stop(state, feature, n, state["plan_path"])
            die(f"git rebase of {branch} failed: {r.stderr.strip()} — fix that, then "
                "stack.py abort (restores every branch) or re-run continue")
        state["position"]["applied"].append(branch)
        save(sfile, state)
        print(f"rebased {branch} onto {parent}")
    state["position"]["next_branch"] = None
    save(sfile, state)
    print("restack complete")
    return 0


def cmd_continue(state, feature, sfile, gitdir):
    branch = state["position"]["next_branch"]
    if not branch or not rebasing(gitdir):
        die("no conflicted rebase to continue — the restack is complete (stack.py push) "
            "or stopped without a rebase; stack.py abort restores every branch")
    n = int(branch.rsplit("-", 1)[1])
    left = git("diff", "--name-only", "--diff-filter=U")
    if left or run("diff", "--check").returncode or run("diff", "--cached", "--check").returncode:
        die(f"unresolved conflicts or conflict markers{': ' + left.replace(chr(10), ', ') if left else ''}"
            " — edit the files, git add them, then re-run continue")
    root = Path(git("rev-parse", "--show-toplevel"))
    cmd = discover_test_cmd(root)
    if not cmd:
        die("no test command found — add a Makefile 'test' target, a package.json 'test' "
            "script or a go.mod (verification.md), then re-run continue")
    t = subprocess.run(cmd, cwd=root, capture_output=True, text=True, errors="replace")
    if t.returncode:
        tail = "\n".join((t.stdout + t.stderr).splitlines()[-40:])
        report = Path(state["plan_path"]).parent / "conflict-report.md"
        report.write_text(f"# Verification failed — slice {n}\n\n`{' '.join(cmd)}` exited "
                          f"{t.returncode} on the resolved tree:\n```\n{tail}\n```\n\n"
                          "Fix the code, git add it, then re-run stack.py continue "
                          "(or stack.py abort).\n")
        print(f"VERIFICATION FAILED: {' '.join(cmd)} exited {t.returncode}; see {report}. "
              "The rebase is still in progress.", file=sys.stderr)
        return 1
    print(f"tests green: {' '.join(cmd)}")
    r = run("rebase", "--continue", env={**os.environ, "GIT_EDITOR": "true"})
    if r.returncode:
        if git("diff", "--name-only", "--diff-filter=U") and rebasing(gitdir):
            return stop(state, feature, n, state["plan_path"])  # next commit conflicts too
        die(f"git rebase --continue failed: {r.stderr.strip()}")
    state["position"]["applied"].append(branch)
    order = state["order"]
    return replay(state, feature, sfile, gitdir, order[order.index(n) + 1:])


def cmd_abort(state, feature, sfile, gitdir):
    if rebasing(gitdir) and run("rebase", "--abort").returncode:
        die("git rebase --abort failed — run it by hand, then re-run abort")
    pushed = state["position"].get("pushed", [])  # branches already published stay put
    todo = [(f"slice/{feature}-{n}", state["branches"][str(n)]["orig_sha"])
            for n in state["order"] if state["branches"][str(n)]["orig_sha"]
            and f"slice/{feature}-{n}" not in pushed]
    head = git("symbolic-ref", "--short", "-q", "HEAD")
    if head in dict(todo) and git("status", "--porcelain", "--untracked-files=no"):
        die(f"{head} is checked out with uncommitted changes — commit or stash "
            "(git stash -u), then re-run abort")
    for branch, sha in todo:
        git("update-ref", "-m", "stack-abort", f"refs/heads/{branch}", sha)
    if head in dict(todo):
        git("reset", "--hard", "-q")  # checked-out branch: bring the tree along
    base = Path(state["plan_path"]).parent
    (base / "conflict-report.md").unlink(missing_ok=True)
    shutil.rmtree(base / ".stack-conflict", ignore_errors=True)
    sfile.unlink()
    print("abort: restored" + "".join(f"\n  {b} -> {s[:7]}" for b, s in todo))
    return 0


def cmd_push(state, feature, sfile, gitdir, fresh, start, yes):
    if fresh:
        if not start:
            die("no completed restack recorded — run stack.py restack <plan> --from <n> "
                "first, or push --from <n> to restack and push in one step")
        save(sfile, state)
        if replay(state, feature, sfile, gitdir, state["order"][state["order"].index(start):]):
            return 1  # stopped on a conflict: resolve, stack.py continue, then push
    elif start:
        die("a restack is already recorded — run push without --from")
    elif state["position"]["next_branch"]:
        die("the restack is unfinished — stack.py continue (or abort), then push")
    shas, pushed = state["branches"], state["position"].setdefault("pushed", [])
    prs = {f"slice/{feature}-{n}": pr_state(f"slice/{feature}-{n}") for n in state["order"]}
    print("\n".join(cost_report(state, feature, prs)))
    if not yes:
        try:
            ok = input("Proceed with the force-push? [y/N] ").strip().lower() == "y"
        except EOFError:
            ok = False
        if not ok:
            print("push declined — nothing pushed (--yes skips this prompt)", file=sys.stderr)
            return 1
    count = 0
    for n in state["order"]:
        branch, s = f"slice/{feature}-{n}", shas[str(n)]
        why = "already pushed" if branch in pushed else push_skip(branch, s, prs[branch])
        if why:
            print(f"skip {branch}: {why}")
            continue
        r = run("push", f"--force-with-lease=refs/heads/{branch}:{s['remote_sha'] or ''}",
                "origin", branch)
        if r.returncode:
            what = ("origin/" + branch + " moved since the restack captured it — a concurrent "
                    "push? Fetch and inspect it") if "stale info" in r.stderr else "push failed"
            print(f"REJECTED: {what}. {r.stderr.strip()}\nNothing after {branch} was pushed; "
                  "re-run push once resolved, or stack.py abort restores the unpushed branches.",
                  file=sys.stderr)
            return 1
        pushed.append(branch)
        save(sfile, state)
        count += 1
        print(f"pushed {branch} -> {git('rev-parse', branch)[:7]}")
    print(f"push complete: {count} pushed, {len(state['order']) - count} skipped")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status",) + MUTATING:
        sp = sub.add_parser(name)
        sp.add_argument("plan", help="path to 03-plan.md")
        sp.add_argument("--dry-run", action="store_true", help="mutate nothing")
        if name == "restack":
            sp.add_argument("--from", dest="start", type=int, help="first slice")
        if name == "push":
            sp.add_argument("--from", dest="start", type=int, help="restack from this slice first")
            sp.add_argument("--yes", action="store_true", help="skip the cost-report prompt")
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
    safety(Path(gitdir), base, feature, order, resuming=args.cmd in RESUMING)
    sfile = Path(gitdir) / "stack-state.json"
    if args.cmd in RESUMING and not sfile.exists():
        die(f"no restack in progress — nothing to {args.cmd}; start one with "
            "stack.py restack <plan> --from <n>")
    if args.cmd == "restack" and sfile.exists() and not args.dry_run:
        die(f"a restack is already recorded in {sfile} — finish it (stack.py continue) "
            "or restore the branches and clear it (stack.py abort), then re-run")
    # An existing state file holds the original SHAs abort restores: never overwrite.
    state = (json.loads(sfile.read_text()) if sfile.exists()
             else capture(args.plan, base, feature, order, start))
    if args.dry_run:
        print(f"dry-run: {args.cmd} — nothing is changed")
        for line in plan_lines(args.cmd, state, feature, start):
            print(f"  {line}")
        return 0
    if args.cmd == "push":
        return cmd_push(state, feature, sfile, Path(gitdir), not sfile.exists(),
                        args.start, args.yes)
    if not sfile.exists():
        sfile.write_text(json.dumps(state, indent=2) + "\n")
    gitdir = Path(gitdir)
    if args.cmd == "restack":
        return replay(state, feature, sfile, gitdir, order[order.index(start):])
    if args.cmd == "continue":
        return cmd_continue(state, feature, sfile, gitdir)
    return cmd_abort(state, feature, sfile, gitdir)


if __name__ == "__main__":
    sys.exit(main())
