---
name: slice-executor
description: "Execute one pre-planned slice from a .specs/{feature}/03-plan.md checklist: create an isolated git worktree, implement the slice per its card, pass deterministic verification gates, and open a small reviewable PR. Use when assigned a slice (e.g. 'implement slice 3 of the payments plan'), when picking up a planned feature slice, or when an orchestrator fans out parallel slice work. Designed to be run stateless by a fresh agent with no prior feature context."
---

# Slice Executor

You are handed two things: a plan file (`.specs/<feature>/03-plan.md`) and a slice number.
Your job: ship exactly that slice as one small, verified PR — nothing more, nothing less.
You have no conversation history and need none; the slice card is your complete spec.

**Hard rules:**

- **Never write code in a shared checkout.** Before implementing, verify you are in an
  isolated git worktree — whether the orchestrator dispatched you into one or you must
  create it yourself (Step 3 handles both). Two agents in the same checkout is a
  build-breaking race, and this check is your defense against sloppy dispatch.
- Never merge your own PR. You open it; a human reviews and merges.
- Never touch files outside your card's `files` + `tests` — the verify gate rejects it anyway.
- If blocked (missing dependency, ambiguous card, failing gate you can't satisfy honestly),
  report the blocker and stop. Do not silently reinterpret the card, and do not guess to
  keep moving — a wrong guess costs a re-slice and a broken parallel build; a stop costs
  minutes.

## Step 1 — Read the contract

Read, in order:

1. The full slice card for your number, plus the plan header (base branch).
2. The RFC sections the card points to (`02-rfc.md`, same directory).
3. The engineering principles: [references/idioms.md](references/idioms.md).
4. The verification loop: [references/verification.md](references/verification.md).

If the card fails the cold-start test — you would need to ask a question to proceed —
report exactly what's missing and stop. That's a plan defect, not your problem to fix.

If you were dispatched without an assignment (self-claim mode), claim your slice atomically
before starting: `mkdir .specs/<feature>/claims/slice-<n>` — if it already exists, another
agent has it; move to the next unclaimed slice.

## Step 2 — Pre-flight: readiness

From the main checkout (or any checkout of the repo), run:

```bash
python3 <skill-dir>/scripts/check_slice.py ready .specs/<feature>/03-plan.md <n>
```

What it verifies depends on the plan header's `deliver:` (default `parallel`):

- **`deliver: parallel`** — every `depends-on` slice's branch (`slice/<feature>-<k>`) is
  merged into the base branch. If not ready, report "slice <n> blocked on slice <k>" and
  stop. If a dependency branch is missing entirely (e.g. after a fresh clone), the merge
  can't be verified from git — report it and let the orchestrator confirm the merge before
  you proceed.
- **`deliver: stacked`** — each `depends-on` branch exists locally, is pushed
  (`origin/slice/<feature>-<k>` exists), and descends from its own parent slice's pushed
  tip. On failure the script prints which check failed (not found locally / not pushed /
  parent branch not pushed / does not descend from the parent tip — restack needed); report
  it and stop. `ready` reads the local `origin/*` refs and does not fetch, so run
  `git fetch origin` first or a freshly pushed parent looks missing.

## Step 3 — Enter (or create, or resume) your worktree

First check: if you are already in a linked worktree on branch `slice/<feature>-<n>`
(the orchestrator pre-created it), proceed to Step 4. Otherwise create yours:

```bash
git fetch origin <base>
git worktree add ../<repo>-<feature>-slice-<n> -b slice/<feature>-<n> origin/<base>
cd ../<repo>-<feature>-slice-<n>
```

**This step must be re-runnable.** If a previous attempt crashed, the worktree or branch
may already exist — resume it, never duplicate or destroy it. On any resume, **re-read your
slice card first**: if the orchestrator fixed it since the crashed attempt, reconcile your
work with the new card before continuing — continuing from a stale understanding is worse
than starting over.

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

## If rebased mid-flight

When another slice's PR merges, the orchestrator may rebase your worktree onto the updated
base and ask you to continue:

1. **Re-read your slice card.** If the orchestrator changed it, reconcile your work with the
   new card before continuing — discard work the new card no longer wants, and say so in
   your report.
2. **Re-run the full verification loop.** Your slice must be green against the *new* base,
   not the one you started from.
3. **Re-run `check_slice.py verify`.** Scope is still judged against the base branch; a
   rebase must not widen your diff beyond the card's declared files.
4. If rebase conflicts force edits outside your card's declared files, report and stop —
   never widen your own scope to make a merge fit.

## Step 5 — Verify

Run the full verification loop from [references/verification.md](references/verification.md)
(build → lint → targeted tests → full suite → prove the behaviors, using the repo's own
commands), then commit any remaining changes and run the mechanical gates:

```bash
python3 <skill-dir>/scripts/check_slice.py verify .specs/<feature>/03-plan.md <n>
```

The gates check, against the base branch (for deliver: stacked, the parent slice's branch):

- **scope** — a changed production file outside the card's declared `files` + `tests`
  fails; an undeclared *test* file edit only prints a `WARN` for the orchestrator to
  resolve (add it to the card or revert it).
- **deletions** — any non-test deleted line fails unless the card declares
  `deletes-existing: yes — <what and why>`.
- **tests** — every declared test file must change (`tests: none` is honored).

There is no size cap. The report prints a size breakdown — `code`, `tests`,
`data/generated` lines — which feeds the review bundle's risk class. `mode:` is legacy and
advisory: it is printed as context and gates nothing. Fix failures by changing your code or
(if the gate exposed a genuine plan defect) report and stop — never edit the plan to make
the gate pass.

## Step 6 — Open the PR

Push your branch and open a PR:

- Title: `[slice <n>] <card title>`
- Body: the card's goal and acceptance criteria (copy from the card), **proof** — for each
  Behavior the card cites, either the command and its actual output, or the test that maps
  to it (file + row names) — what was tested, and the one reviewable decision this PR makes.
- Base, by the plan header's `deliver:` — `parallel`: the plan's base branch; `stacked`:
  the parent slice's branch (`slice/<feature>-<k>` for the `depends-on` slice Step 2
  checked; slice n-1 in a linear stack), and the PR body names that base. Never merge your own PR, whatever the base.

Report back: PR link, slice number, gate output, and anything the orchestrator should know
(deviations, follow-ups discovered). After the PR merges, the worktree can be removed with
`git worktree remove ../<repo>-<feature>-slice-<n>` — that is the orchestrator's or your
final cleanup step once merge is confirmed, not before.
