---
name: slice-executor
description: "Execute one pre-planned slice from a .specs/{feature}/03-plan.md checklist: create an isolated git worktree, implement the slice per its card, pass deterministic verification gates, and open a small reviewable PR. Use when assigned a slice (e.g. 'implement slice 3 of the payments plan'), when picking up a planned feature slice, or when an orchestrator fans out parallel slice work. Designed to be run stateless by a fresh agent with no prior feature context."
---

# Slice Executor

You are handed two things: a plan file (`.specs/<feature>/03-plan.md`) and a slice number.
Your job: ship exactly that slice as one small, verified PR — nothing more, nothing less.
You have no conversation history and need none; the slice card is your complete spec.

**Hard rules:**

- Work **only in your own git worktree**, never in a shared checkout. Two agents in the same
  checkout is a build-breaking race.
- Never merge your own PR. You open it; a human reviews and merges.
- Never touch files outside your card's `files` + `tests` — the verify gate rejects it anyway.
- If blocked (missing dependency, ambiguous card, failing gate you can't satisfy honestly),
  report the blocker and stop. Do not silently reinterpret the card, and do not guess to
  keep moving — a wrong guess costs a re-slice and a broken parallel build; a stop costs
  minutes.

## Step 1 — Read the contract

Read, in order:

1. The full slice card for your number, plus the plan header (base branch, stack).
2. The RFC sections the card points to (`02-rfc.md`, same directory).
3. The engineering principles: [references/idioms.md](references/idioms.md).
4. The verification loop: [references/verification.md](references/verification.md).

If the card fails the cold-start test — you would need to ask a question to proceed —
report exactly what's missing and stop. That's a plan defect, not your problem to fix.

## Step 2 — Pre-flight: readiness

From the main checkout (or any checkout of the repo), run:

```bash
python3 <skill-dir>/scripts/check_slice.py ready .specs/<feature>/03-plan.md <n>
```

This verifies every `depends-on` slice's branch (`slice/<feature>-<k>`) is merged into the
base branch. If not ready, report "slice <n> blocked on slice <k>" and stop.

## Step 3 — Create (or resume) your worktree

```bash
git fetch origin <base>
git worktree add ../<repo>-<feature>-slice-<n> -b slice/<feature>-<n> origin/<base>
cd ../<repo>-<feature>-slice-<n>
```

**This step must be re-runnable.** If a previous attempt crashed, the worktree or branch
may already exist — resume it, never duplicate or destroy it:

- Branch `slice/<feature>-<n>` exists but no worktree: re-attach with
  `git worktree add ../<repo>-<feature>-slice-<n> slice/<feature>-<n>`.
- Worktree already exists: `cd` into it, run `git status` and `git log --oneline
  <base>..HEAD` to see where the attempt stopped, and continue from there.
- Only if the prior attempt's committed work is wrong, reset deliberately
  (`git reset --hard origin/<base>`) and say so in your report — never silently.

Branch naming is a contract: `slice/<feature>-<n>`. The readiness gate for downstream slices
depends on it. Verify you are in a linked worktree (`git rev-parse --git-dir` differs from
`git rev-parse --git-common-dir`).

## Step 4 — Implement

Implement exactly what the card says, following the principles in [references/idioms.md](references/idioms.md)
and the concrete stack patterns recorded in the RFC's Design section. Behavior changes ship with
tests in the same diff (table-driven where the input space is combinatorial). Commit in small
steps if you like; the gates below evaluate the full branch diff against the base.

## Step 5 — Verify

Run the full verification loop from [references/verification.md](references/verification.md)
(build → lint → targeted tests → full suite, using the repo's own commands), then commit any
remaining changes and run the mechanical gates:

```bash
python3 <skill-dir>/scripts/check_slice.py verify .specs/<feature>/03-plan.md <n>
```

The gates check, against the base branch: scope (only declared `files` + `tests` changed),
size (≤350 changed lines excluding lockfiles/generated code), mode (an `additive` slice
deletes almost nothing), and tests (declared test files actually changed). Fix failures by
changing your code or (if the gate exposed a genuine plan defect) report and stop — never
edit the plan to make the gate pass.

## Step 6 — Open the PR

Push your branch and open a PR:

- Title: `[slice <n>] <card title>`
- Body: the card's goal and acceptance criteria (copy from the card), what was tested, and
  the one reviewable decision this PR makes.
- Base: the plan's base branch.

Report back: PR link, slice number, gate output, and anything the orchestrator should know
(deviations, follow-ups discovered). After the PR merges, the worktree can be removed with
`git worktree remove ../<repo>-<feature>-slice-<n>` — that is the orchestrator's or your
final cleanup step once merge is confirmed, not before.
