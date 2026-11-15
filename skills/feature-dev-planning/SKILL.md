---
name: feature-dev-planning
description: "Turn a PRD into an RFC and a parallel-safe PR slice plan under .specs/{feature}/ (01-prd.md, 02-rfc.md, 03-plan.md). Use when the user provides a PRD, feature request, or spec and wants it planned, broken into small reviewable PRs, or prepared for parallel slice execution by sub-agents. Covers Phase 1 (PRD interrogation + RFC draft) and Phase 2 (slicing with dependency edges). Execution of individual slices is handled by the slice-executor skill."
---

# Feature Dev Planning

Plan a feature as a sequence of small, independently reviewable, independently executable PRs.
All state lives in `.specs/<feature>/` in the repo — the highest-numbered doc that exists tells
you where you are. No other progress tracking exists or is needed.

```
.specs/<feature>/
├── 01-prd.md    # the input (user-provided or transcribed by you)
├── 02-rfc.md    # the design — audited against the real codebase
└── 03-plan.md   # the slice checklist — the contract for slice-executor
```

Never start Phase 2 before the user approves the RFC — and approval is only valid with
**zero open questions**: each one answered, or explicitly accepted as an assumption by the
user. Never fan out before PR 1 (the foundation) has landed on the base branch.

## Phase 1 — PRD → RFC

Read [references/rfc.md](references/rfc.md) for the full procedure and the RFC template. Summary:

1. Audit the codebase first: find the modules, tables, and conventions the PRD touches. The RFC
   must describe the change against the *actual* code, not a generic architecture.
2. Interrogate the PRD until nothing material is unknown: requirements, flows, edge cases,
   failure modes, scale, and alternative designs. Exhaust the codebase audit and sensible
   defaults first; ask the human only what genuinely remains, in batched rounds where every
   question carries a proposed default. Full protocol: [references/rfc.md](references/rfc.md).
3. Draft `02-rfc.md` using the template in the reference. The RFC must pin the stack
   (language, framework, datastore) — the Design section records the repo-derived patterns
   for that stack, and the executor implements what it records.
4. Present the RFC and stop. Wait for user approval.

   The RFC must pin the stack (language, framework, datastore) and derive its concrete
   patterns from the repo's closest analogous code — the executor implements what the RFC
   records, so a pattern-free Design is an RFC defect.

## Phase 2 — RFC → Slice plan

Read [references/slices.md](references/slices.md) for the slice-card format and the full slicing
rules. Summary of the rules that are enforced, not advisory:

- **One reviewable decision per PR.** ~350 changed lines is the backstop, not the goal.
- **Expand → migrate → contract, never mixed.** Slice 1 is the strictly additive foundation
  (new columns/tables/endpoints, no deletions); destructive cleanup is the last slice.
- **Every slice independently green and mergeable.** If slice N only makes sense with slice N+1
  applied, re-slice.
- **Disjoint file sets across parallel slices.** Two slices that need the same file are not
  independent — sequence them with a `depends-on` edge, or re-slice until they are disjoint.
  When in doubt, add the edge: a false dependency costs wall-clock time; a missing one costs a
  broken parallel build.
- **Slice cards must pass the cold-start test:** a competent dev (or sub-agent) who has never
  seen the feature could ship the slice from the card alone, without asking questions.

Write `03-plan.md`, then run:

```bash
python3 <skill-dir>/scripts/plan_lint.py .specs/<feature>/03-plan.md
```

The linter enforces: required card fields, file overlap without a dependency edge, dependency
cycles, and that slice 1 is additive. Fix failures in the plan — never waive the linter. For
idioms to follow in cards (additive-first, one path per behavior, test-covered,
at-least-once execution), read [references/idioms.md](references/idioms.md).

## Fan-out — handoff to slice-executor

After `03-plan.md` lints clean and the foundation slice (slice 1) has landed on the base branch:

- **Assign, don't let agents self-claim.** You (the orchestrator) decide which agent gets which
  slice. Assignment needs no locking.
- Only assign slices whose `depends-on` targets are all merged. Re-evaluate after each merge.
- Spawn one sub-agent per ready slice, each instructed to run the `slice-executor` skill against
  `.specs/<feature>/` and its slice number. Each agent works in its **own git worktree** — never
  two agents in the same checkout — on a branch named `slice/<feature>-<n>`. The branch name is
  a contract: downstream readiness checks depend on it.
- After each slice's PR merges, rebase the remaining worktrees onto the updated base branch and
  have each re-run its verification before continuing.
- If an executor reports a card as ambiguous or blocking: fix the card in `03-plan.md`, re-run
  `plan_lint.py`, then re-dispatch. Never resolve ambiguity with verbal instructions to one
  agent — parallel agents won't hear them, and the plan stops matching the code.

Fallback if agents must self-claim (e.g., user fires off independent sessions): the cheapest
atomic lock is `mkdir .specs/<feature>/claims/slice-<n>` — exactly one agent succeeds; all
others move to the next unclaimed slice.
