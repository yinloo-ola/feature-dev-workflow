#!/usr/bin/env python3
"""Inspect and restack the slice branches of a .specs/<feature>/03-plan.md plan.

Usage: stack.py {status,restack,continue,abort,push} <path-to-03-plan.md> [--dry-run]

status prints each slice branch in stack order with its local/remote SHA and PR.
restack --from n replays branches n..N onto their parents; on a conflict it stops,
writes conflict-report.md beside the plan and leaves the rebase in progress.
continue finishes the stopped branch (tree resolved, no conflict markers, tests green:
--test-cmd "<shell command>" overrides discovery) and carries on;
abort restores every branch to its recorded SHA (--force even over later commits). push prints the cost report, asks to
proceed (--yes skips; a closed stdin refuses), then force-pushes every changed branch in one
atomic command, each with a lease on the remote SHA captured at restack time; push --from n
restacks first. A finished restack, push or abort switches back to the starting branch.
Every mutating command supports --dry-run.
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
DEPS_RE = re.compile(r"^\[([^\]]*)\]$")  # same grammar as plan_lint/check_slice
DELIVER_RE = re.compile(r"^deliver:\s*(parallel|stacked)$")  # same grammar as plan_lint
MUTATING = ("restack", "continue", "abort", "push")
RESUMING = ("continue", "abort")  # run mid-conflict: tree is dirty, a rebase is open
MARKER_RE = re.compile(r"^(<{7}|>{7})( |$)")  # a lone ======= is a setext heading, not a marker
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
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else None


def parse_plan(path):
    """Return (base_branch, feature, order, deliver) — order is the linearized stack."""
    text = Path(path).read_text()
    base = re.search(r"^Base branch:\s*(\S+)", text, re.M)
    if not base:
        die(f"{path}: missing 'Base branch:' header — add it to the plan")
    m = re.search(r"^# Plan:\s*(\S+)", text, re.M)
    feature = m.group(1) if m else Path(path).resolve().parent.name
    deliver = "parallel"
    for line in text.splitlines():
        if line.startswith("deliver:"):
            m = DELIVER_RE.match(line.rstrip())
            if not m:
                die(f"{path}: deliver must be parallel or stacked, got "
                    f"{line[len('deliver:'):].strip()!r}")
            deliver = m.group(1)
            break
    deps, cur = {}, None
    for line in text.splitlines():
        m = SLICE_RE.match(line)
        if m:
            cur = int(m.group(1))
            deps[cur] = []
        elif cur is not None and line.startswith("- depends-on:"):
            rest = re.sub(r"\s*#.*$", "", line[len("- depends-on:"):]).strip()
            b = DEPS_RE.match(rest)
            if not b:
                die(f"{path}: slice {cur}: depends-on must be a bracketed list, got {rest!r}")
            deps[cur] = [int(d) for d in re.findall(r"\d+", b.group(1))]
    if not deps:
        die(f"{path}: no '### Slice N:' cards found — run plan_lint.py on the plan")
    for n, ds in deps.items():
        bad = [d for d in ds if d >= n or d not in deps]
        if bad:
            die(f"{path}: slice {n} depends-on {bad} — edges must point to earlier, "
                "existing slices; fix the plan (plan_lint.py reports this)")
    # Every edge points backward, so ascending slice number IS the topological
    # order with a numeric tiebreak — no graph walk needed.
    return base.group(1), feature, sorted(deps), deliver


def capture(plan, base, feature, order, deliver, start):
    """Snapshot the stack: the record continue/abort/push work from."""
    def sha(ref):
        return git("rev-parse", "--verify", "-q", ref)
    return {
        "plan_path": str(Path(plan).resolve()),
        "feature": feature,
        "base": base,
        "deliver": deliver,
        "order": order,
        "branches": {str(n): {
            "orig_sha": sha(f"refs/heads/slice/{feature}-{n}"),
            "remote_sha": sha(f"refs/remotes/origin/slice/{feature}-{n}"),
        } for n in order},
        "position": {"next_branch": f"slice/{feature}-{start}", "applied": []},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start_branch": git("symbolic-ref", "--short", "-q", "HEAD"),
        "start_worktree": str(Path(git("rev-parse", "--show-toplevel")).resolve()),
    }


def restore_head(state):
    """Switch back to the branch the first mutation started on: same worktree, clean tree."""
    b = state.get("start_branch")  # absent in a state recorded before slice 14
    if (b and git("symbolic-ref", "--short", "-q", "HEAD") != b
            and state.get("start_worktree") == str(Path(git("rev-parse", "--show-toplevel")).resolve())
            and not git("status", "--porcelain", "--untracked-files=no")):
        run("switch", "-q", b)


def unmerged():
    """Paths with unresolved conflicts, NUL-delimited so non-ASCII names survive."""
    return [f for f in run("diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if f]


def marker_files(diff):
    """Files with an added line that is a conflict marker, from git diff -U0 output."""
    hit, cur = set(), None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[4:].removeprefix("b/")
        elif line.startswith("+") and MARKER_RE.match(line[1:]):
            hit.add(cur)
    return hit


def create_state(sfile, state):
    """Create the state file exclusively: of two concurrent restacks only one wins."""
    try:
        with open(sfile, "x") as f:
            f.write(json.dumps(state, indent=2) + "\n")
    except FileExistsError:
        die_recorded(sfile)


def die_recorded(sfile):
    die(f"a restack is already recorded in {sfile} — finish it (stack.py continue) "
        "or restore the branches and clear it (stack.py abort), then re-run")


def clear_artifacts(plan_path):
    base = Path(plan_path).parent
    (base / "conflict-report.md").unlink(missing_ok=True)
    shutil.rmtree(base / ".stack-conflict", ignore_errors=True)


def rebasing(gitdir):
    return (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists()


def fork_base(parent, branch):
    """The commit branch was built on: parent's reflog fork point, else plain merge-base."""
    return (git("merge-base", "--fork-point", parent, branch)
            or git("merge-base", parent, branch))

def load_state(sfile, feature):
    """The recorded state, refused if unreadable or recorded for another feature."""
    try:
        state = json.loads(sfile.read_text())
    except (OSError, ValueError) as e:
        die(f"state file {sfile} is unreadable ({e}) - remove it to reset")
    if state.get("feature") != feature:
        die(f"state belongs to feature {state.get('feature')}; run stack.py with that "
            f"plan, or remove {sfile} to discard")
    return state

def refuse_diverged(state, feature):
    """A local branch must contain every origin commit before a restack rewrites it."""
    for n, s in state["branches"].items():
        b, orig, remote = f"slice/{feature}-{n}", s["orig_sha"], s["remote_sha"]
        if orig and remote and remote != orig and run("merge-base", "--is-ancestor",
                                                     remote, orig).returncode:
            lost = (git("log", "--oneline", f"{orig}..{remote}") or "").splitlines()
            die(f"local {b} lacks origin commits ({'; '.join(lost)}); integrate them "
                f"(git rebase origin/{b}) before restacking")

def refuse_foreign_checkouts(branches):
    """No branch we may move can be checked out in a worktree other than this one."""
    here = Path(git("rev-parse", "--show-toplevel")).resolve()
    for block in (git("worktree", "list", "--porcelain") or "").split("\n\n"):
        f = dict(x.split(" ", 1) for x in block.splitlines() if " " in x)
        b = f.get("branch", "").removeprefix("refs/heads/")
        if b in branches and Path(f["worktree"]).resolve() != here:
            die(f"branch {b} is checked out at {f['worktree']} - switch that worktree "
                "off it, then re-run")

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


def cmd_status(plan, base, feature, order, deliver):
    gh = shutil.which("gh")
    print(f"stack {feature} on {base}: " + " -> ".join(map(str, order)))
    if not gh:
        print("gh absent — order from plan only")
    snap, parent = capture(plan, base, feature, order, deliver, order[0])["branches"], base
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
    if r.returncode == 0:
        return json.loads(r.stdout)
    err = (r.stderr or r.stdout).strip()
    if "no pull requests found" not in err:  # a real failure, not just "no PR yet"
        print(f"gh: {err.splitlines()[0] if err else f'exited {r.returncode}'} - merged-PR "
              f"protection unavailable for {branch}", file=sys.stderr)
    return None

def push_skip(branch, s, pr):
    """Why branch needs no push, or None."""
    tip = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if not tip:
        return "no local branch"
    if pr and pr["state"] == "MERGED":
        return "PR merged"
    if tip == s["remote_sha"]:
        return "tip equals remote"

def push_set(state, feature, prs):
    """[(branch, remote sha or None, tip)] — exactly what one push would send."""
    pushed = state["position"].get("pushed", [])
    out = []
    for n in state["order"]:
        b, s = f"slice/{feature}-{n}", state["branches"][str(n)]
        if b not in pushed and not push_skip(b, s, prs.get(b)):
            out.append((b, s["remote_sha"], git("rev-parse", f"refs/heads/{b}")))
    return out

def cost_report(state, feature, prs, show_set=True):
    """What a push costs: restacked range, patches range-diff says changed, the push set, PRs that move."""
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
    if show_set:
        sent = push_set(state, feature, prs)
        out.append("  push set:" + ("" if sent else " none"))
        out += [f"    {b}: {old[:7] if old else 'new branch'} -> {tip[:7]}"
                + ("" if b in state["position"]["applied"] else " (not restacked in this run)")
                for b, old, tip in sent]
    out += ["  PRs whose heads will move: " + (", ".join(moving) or "none (or gh absent)"),
            "  reviewers of moved heads must re-review"]
    return out

def plan_lines(cmd, state, feature, start, restack_from=None):
    """The actions `cmd` would take, plus its cost line — what --dry-run prints.

    restack_from: push --from n on a fresh state; the restack is not simulated.
    """
    order, shas = state["order"], state["branches"]
    todo = order[order.index(start):] if cmd == "restack" else order
    lines = []
    prs = {}
    for n in todo:
        branch, s = f"slice/{feature}-{n}", shas[str(n)]
        parent = parent_of(state, feature, n)[0]
        if cmd == "restack":
            base_old = f" (base_old {(fork_base(parent, branch) or '?')[:7]})" if s["orig_sha"] else ""
            lines.append(f"rebase {branch} onto {parent}" + base_old)
        elif cmd == "push" and restack_from is not None and n >= restack_from:
            continue  # the restack would rewrite it: its push line is unknowable until then
        elif cmd == "push":
            prs[branch] = pr_state(branch)
            why = push_skip(branch, s, prs[branch])
            lines.append(f"push {branch} --force-with-lease=refs/heads/{branch}:"
                         f"{s['remote_sha'] or ''}")
            if why:
                lines[-1] += f"  [skip: {why}]"
        elif cmd == "abort" and s["orig_sha"] and branch not in state["position"].get("pushed", []):
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
        if restack_from is not None:
            lines.append(f"restack {restack_from}..{order[-1]} would run first; "
                         "push set is computed after it")
        lines += cost_report(state, feature, prs, show_set=restack_from is None)
    return lines


def save(sfile, state):
    sfile.write_text(json.dumps(state, indent=2) + "\n")


def parent_of(state, feature, n):
    """(parent ref, old parent tip) for slice n.

    stacked: nearest earlier existing branch, else base (a linear stack).
    parallel: always the plan base — each slice replays onto the updated base,
    never onto a sibling.
    """
    order, shas = state["order"], state["branches"]
    orig = shas[str(n)]["orig_sha"]
    base_old = shas[str(n)].get("base_old")

    def fork():  # where n was built: unknown while n has no branch (a dry-run may ask)
        return base_old or (git("merge-base", state["base"], orig) if orig else None)

    if state.get("deliver", "stacked") != "stacked":
        return state["base"], fork()
    for m in reversed(order[:order.index(n)]):
        if shas[str(m)]["orig_sha"]:
            return f"slice/{feature}-{m}", base_old or shas[str(m)]["orig_sha"]
    return state["base"], fork()


def discover_test_cmd(root, override=None):
    """The shell command that runs the tests: --test-cmd, else the first marker file found."""
    if override:
        return override
    mk, pkg = root / "Makefile", root / "package.json"
    try:
        makefile = mk.read_text(errors="replace") if mk.is_file() else ""
    except OSError:
        makefile = ""
    if re.search(r"^test:", makefile, re.M):
        return "make test"
    try:
        scripts = (json.loads(pkg.read_text(errors="replace")).get("scripts", {})
                   if pkg.is_file() else {})
    except (OSError, ValueError):  # ValueError covers JSONDecodeError
        scripts = {}
    if "test" in scripts:
        return "npm test"
    for marker, cmd in (("go.mod", "go vet ./... && go test ./..."), ("Cargo.toml", "cargo test")):
        if (root / marker).is_file():
            return cmd
    if any((root / f).is_file() for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")):
        return "python3 -m pytest -q"
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
    files = unmerged()
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
        parent = parent_of(state, feature, n)[0]
        old_tip = fork_base(parent, branch)
        if not old_tip:
            die(f"cannot find the commit {branch} was built on (no merge-base with {parent})")
        state["branches"][str(n)]["base_old"] = old_tip
        state["position"]["next_branch"] = branch
        save(sfile, state)
        r = run("rebase", "--onto", parent, old_tip, branch)
        if r.returncode:
            if unmerged() and rebasing(gitdir):
                return stop(state, feature, n, state["plan_path"])
            die(f"git rebase of {branch} failed: {r.stderr.strip()} — fix that, then "
                "stack.py abort (restores every branch) or re-run continue")
        state["position"]["applied"].append(branch)
        state["branches"][str(n)]["new_sha"] = git("rev-parse", f"refs/heads/{branch}")
        save(sfile, state)
        print(f"rebased {branch} onto {parent}")
    state["position"]["next_branch"] = None
    save(sfile, state)
    print("restack complete")
    restore_head(state)
    return 0


def cmd_continue(state, feature, sfile, gitdir, test_cmd=None):
    branch = state["position"]["next_branch"]
    if not branch or not rebasing(gitdir):
        die("no conflicted rebase to continue — the restack is complete (stack.py push) "
            "or stopped without a rebase; stack.py abort restores every branch")
    n = int(branch.rsplit("-", 1)[1])
    if run("ls-files", "-u").stdout.strip():
        die(f"unresolved conflicts: {', '.join(unmerged())} — edit the files, git add them, "
            "then re-run continue")
    # added lines only: trailing whitespace in a resolution is the author's business
    diff = "".join(run("-c", "core.quotepath=off", "diff", "--no-color", "-U0", *x).stdout
                   for x in ((), ("--cached",)))
    if marker_files(diff):
        die(f"conflict markers remain in {', '.join(sorted(marker_files(diff)))} — edit the "
            "files, git add them, then re-run continue")
    root = Path(git("rev-parse", "--show-toplevel"))
    cmd = discover_test_cmd(root, test_cmd)
    if not cmd:
        die("no test command found - pass --test-cmd \"<shell command>\" (or add a Makefile "
            "'test' target, a package.json 'test' script, go.mod, Cargo.toml or pytest config)")
    t = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True,
                       errors="replace", stdin=subprocess.DEVNULL)
    if t.returncode:
        tail = "\n".join((t.stdout + t.stderr).splitlines()[-40:])
        report = Path(state["plan_path"]).parent / "conflict-report.md"
        report.write_text(f"# Verification failed — slice {n}\n\n`{cmd}` exited "
                          f"{t.returncode} on the resolved tree:\n```\n{tail}\n```\n\n"
                          "Fix the code, git add it, then re-run stack.py continue "
                          "(or stack.py abort).\n")
        print(f"VERIFICATION FAILED: {cmd} exited {t.returncode}; see {report}. "
              "The rebase is still in progress.", file=sys.stderr)
        return 1
    print(f"tests green: {cmd}")
    r = run("rebase", "--continue", env={**os.environ, "GIT_EDITOR": "true"})
    if r.returncode:
        if unmerged() and rebasing(gitdir):
            return stop(state, feature, n, state["plan_path"])  # next commit conflicts too
        die(f"git rebase --continue failed: {r.stderr.strip()}")
    state["position"]["applied"].append(branch)
    state["branches"][str(n)]["new_sha"] = git("rev-parse", f"refs/heads/{branch}")
    order = state["order"]
    code = replay(state, feature, sfile, gitdir, order[order.index(n) + 1:])
    if code == 0:  # the whole restack is done: the brief and its artifacts are stale
        clear_artifacts(state["plan_path"])
    return code


def abort_plan(state, feature, force):
    """(branches to restore, branches with later commits); refuses the latter without force."""
    pushed = state["position"].get("pushed", [])  # branches already published stay put
    todo = [(f"slice/{feature}-{n}", s["orig_sha"], s.get("new_sha"))
            for n, s in ((n, state["branches"][str(n)]) for n in state["order"])
            if s["orig_sha"] and f"slice/{feature}-{n}" not in pushed]
    lost = []  # branches whose tip is neither the original nor the restacked one
    for b, orig, new in todo:
        cur = git("rev-parse", "--verify", "-q", f"refs/heads/{b}")
        if cur and cur not in (orig, new):
            lost.append((b, cur))
    if lost and not force:
        die("; ".join(f"{b} has commits not in the recorded restack ({cur})" for b, cur in lost)
            + "; abort would discard them - re-run with --force to restore anyway "
            "(the tip stays in the reflog)")
    return todo, lost


def cmd_abort(state, feature, sfile, gitdir, force):
    todo, lost = abort_plan(state, feature, force)
    if rebasing(gitdir) and run("rebase", "--abort").returncode:
        die("git rebase --abort failed — run it by hand, then re-run abort")
    head = git("symbolic-ref", "--short", "-q", "HEAD")
    if head in [b for b, _, _ in todo] and git("status", "--porcelain", "--untracked-files=no"):
        die(f"uncommitted changes on checked-out {head} - commit or stash first")
    for b, cur in lost:
        print(f"discarding {b} tip {cur}")
    for branch, sha, _ in todo:
        if branch == head:
            git("reset", "--hard", "-q", sha)  # checked out here: move the tree along
        else:
            git("update-ref", "-m", "stack-abort", f"refs/heads/{branch}", sha)
    clear_artifacts(state["plan_path"])
    sfile.unlink()
    print("abort: restored" + "".join(f"\n  {b} -> {s[:7]}" for b, s, _ in todo))
    restore_head(state)
    return 0

def cmd_push(state, feature, sfile, gitdir, fresh, start, yes):
    if fresh:
        if not start:
            die("no completed restack recorded — run stack.py restack <plan> --from <n> "
                "first, or push --from <n> to restack and push in one step")
        create_state(sfile, state)
        if replay(state, feature, sfile, gitdir, state["order"][state["order"].index(start):]):
            return 1  # stopped on a conflict: resolve, stack.py continue, then push
    elif start:
        die("a restack is already recorded — run push without --from")
    elif state["position"]["next_branch"]:
        die("the restack is unfinished — stack.py continue (or abort), then push")
    shas, pushed = state["branches"], state["position"].get("pushed", [])
    prs = {f"slice/{feature}-{n}": pr_state(f"slice/{feature}-{n}") for n in state["order"]}
    print("\n".join(cost_report(state, feature, prs)))
    if not yes:
        try:
            ok = input("Proceed with the force-push? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, RuntimeError):  # no stdin to ask: that is a NO, said plainly
            print("refusing to push without confirmation (stdin closed) - pass --yes",
                  file=sys.stderr)
            return 1
        if not ok:
            print("push declined — nothing pushed (--yes skips this prompt)", file=sys.stderr)
            return 1
    sent = push_set(state, feature, prs)
    if sent:  # one atomic command: every lease holds or nothing moves
        r = run("push", "--atomic",
                *[f"--force-with-lease=refs/heads/{b}:{old or ''}" for b, old, _ in sent],
                "origin", *[b for b, _, _ in sent])
        if r.returncode:
            m = re.search(r"\[rejected\]\s+\S+ -> (\S+)\s+\(stale info\)", r.stderr)
            print((f"push rejected - nothing was pushed (atomic). origin changed since the "
                   f"restack: inspect with git fetch && git log origin/{m.group(1)}, then "
                   "stack.py abort and restack") if m else
                  "push failed - nothing was pushed (atomic)", f"\n{r.stderr.strip()}",
                  file=sys.stderr, sep="")
            return 1
        state["position"]["pushed"] = pushed + [b for b, _, _ in sent]
        save(sfile, state)
    names = {b for b, _, _ in sent}
    for n in state["order"]:
        branch = f"slice/{feature}-{n}"
        if branch in names:
            print(f"pushed {branch} -> {git('rev-parse', branch)[:7]}")
        else:
            print(f"skip {branch}: " + ("already pushed" if branch in pushed
                                        else push_skip(branch, shas[str(n)], prs[branch])))
    print(f"push complete: {len(sent)} pushed, {len(state['order']) - len(sent)} skipped")
    restore_head(state)
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
        if name == "continue":
            sp.add_argument("--test-cmd", help="shell command that runs the tests")
        if name == "abort":
            sp.add_argument("--force", action="store_true",
                            help="restore even over commits made after the restack")
        if name == "push":
            sp.add_argument("--from", dest="start", type=int, help="restack from this slice first")
            sp.add_argument("--yes", action="store_true", help="skip the cost-report prompt")
    args = ap.parse_args(argv)
    if not Path(args.plan).is_file():
        die(f"{args.plan} not found — pass the path to 03-plan.md")
    base, feature, order, deliver = parse_plan(args.plan)
    gitdir = git("rev-parse", "--absolute-git-dir")
    if not gitdir:
        die("not inside a git repository — cd into the repo checkout, then re-run")
    if args.cmd == "status":  # read-only, so --dry-run changes nothing
        cmd_status(args.plan, base, feature, order, deliver)
        return 0
    start = getattr(args, "start", None)
    if start is None:
        start = order[0]
    elif start not in order:
        die(f"--from {start}: no such slice (plan has {', '.join(map(str, order))})")
    safety(Path(gitdir), base, feature, order, resuming=args.cmd in RESUMING)
    # one state file per repo, shared by every worktree
    sfile = Path(git("rev-parse", "--git-common-dir")).resolve() / "stack-state.json"
    if args.cmd in RESUMING and not sfile.exists():
        die(f"no restack in progress — nothing to {args.cmd}; start one with "
            "stack.py restack <plan> --from <n>")
    if args.cmd == "restack" and sfile.exists() and not args.dry_run:
        die_recorded(sfile)
    # An existing state file holds the original SHAs abort restores: never overwrite.
    fresh = not sfile.exists()
    if fresh:
        state = capture(args.plan, base, feature, order, deliver, start)
        refuse_diverged(state, feature)
    else:
        state = load_state(sfile, feature)
    feature = state["feature"]
    pushed = state["position"].get("pushed", [])
    scope = {"continue": [], "restack": order[order.index(start):],
             "push": order[order.index(start):] if fresh else state["order"],
             "abort": [n for n in state["order"] if f"slice/{feature}-{n}" not in pushed]}[args.cmd]
    refuse_foreign_checkouts([f"slice/{feature}-{n}" for n in scope])
    if args.dry_run:
        print(f"dry-run: {args.cmd} — nothing is changed")
        if args.cmd == "abort":
            abort_plan(state, feature, args.force)  # previews the lost-commits refusal
        for line in plan_lines(args.cmd, state, feature, start,
                               args.start if args.cmd == "push" and fresh else None):
            print(f"  {line}")
        return 0
    if args.cmd == "push":
        return cmd_push(state, feature, sfile, Path(gitdir), not sfile.exists(),
                        args.start, args.yes)
    if fresh:
        create_state(sfile, state)
    gitdir = Path(gitdir)
    if args.cmd == "restack":
        return replay(state, feature, sfile, gitdir, order[order.index(start):])
    if args.cmd == "continue":
        return cmd_continue(state, feature, sfile, gitdir, args.test_cmd)
    return cmd_abort(state, feature, sfile, gitdir, args.force)


if __name__ == "__main__":
    sys.exit(main())
