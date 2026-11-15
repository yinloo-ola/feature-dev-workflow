# Phase 1 — PRD → RFC

## Procedure

1. **Transcribe the PRD.** If the user gave the PRD inline or as a link/attachment, save it as
   `.specs/<feature>/01-prd.md` verbatim (add nothing). The feature name is kebab-case, derived
   from the PRD title.

2. **Audit the codebase.** Before writing a single design line, establish:
   - The modules, packages, and files the feature touches (by search, not assumption).
   - Existing schemas/migrations and the migration conventions (tool, naming, direction).
   - Existing patterns for the closest analogous feature already in the repo. They are the
   default to extend — not a mandate. A better pattern may be chosen when the RFC records
   the deviation and its reason. The audit's job is to find them either way: you cannot
   decide to deviate from a pattern you never knew existed.
   - Anything the PRD assumes exists but doesn't.

3. **Interrogate the PRD.** Interrogate until nothing *material* is unknown — an RFC drafted
   over unresolved requirements, edge cases, or flows is a confident document that is wrong.
   But interrogation has an order of cost; exhaust each source before moving to the next:

   1. **The codebase.** How does the repo handle this today? What does the closest analogous
      feature do? Many "questions" are answerable by reading code — never ask a human what
      the audit can tell you.
   2. **Sensible defaults.** For each remaining unknown, is there an industry-standard or
      repo-consistent default? If yes, adopt it and record it as an assumption.
   3. **The human.** Only what survives steps 1–2: unknowns where the answer would change
      the design and no safe default exists.

   When asking the human:
   - **Batch into rounds.** One message per round, questions grouped by theme (scope, edge
     cases, data model, …). Never drip-feed single questions.
   - **Every question carries a proposed default.** The user can answer selectively or just
     say "defaults are fine" — unanswered questions never block progress.
   - **Materiality filter.** Ask only what could change the design. Curiosities become
     recorded assumptions, not questions.

   Before drafting, run the **coverage checklist** — every item resolved by audit, default,
     or human answer:
   - Scope: what's explicitly in, explicitly out, and deferred
   - Happy-path flows, end to end
   - Edge cases: empty/zero/boundary values, concurrent access, partial failure, rollback
   - Idempotency and re-execution: which operations can run more than once (client retries,
     duplicate delivery, worker restarts, re-run migrations/backfills), and is each safe to
     re-run? Every write path gets an explicit answer: idempotent by natural key, guarded by
     an existence check or dedup — or explicitly documented as at-most-once with the caller
     contract that implies
   - Scale and limits: expected volumes, growth, hot paths
   - Data model and migrations: schema changes, backfills, reversibility
   - Observability: how success and failure are detected in production
   - Security and access: who may call or see each new path, what data is exposed,
     PII handling — depth proportional to the surface the feature adds
   - Alternatives: at least two designs considered, one recommended with reasons

   Each checklist item's disposition (answered where, assumed as what, defaulted how)
   is recorded in the RFC's `## Coverage` section — the checklist leaves a trace.

   **Stop condition:** the RFC may be drafted when no remaining unknown is *both*
   unresolvable by audit or default *and* material to the design. Everything unasked or
   unanswered lands in `## Assumptions and open questions` with its default.

4. **Draft the RFC** as `.specs/<feature>/02-rfc.md` using the template below. Keep it additive
   in spirit: the design should be shippable in stages, each stage leaving the system working.

5. **Present and stop.** Show the user the RFC path, the assumptions taken, and the open
   questions — they are the agenda for the approval conversation. Approval requires **zero
   open questions**: each must be answered, or the user explicitly accepts its default and it
   is promoted to an assumption (recorded as such). "Approved, defaults fine" is a valid,
   complete approval; approving while questions remain unanswered is not. Do not begin
   Phase 2 until approval is complete.

## RFC template

```markdown
# RFC: <feature>

- Stack: <language / framework / datastore — pins the concrete patterns derived below>
- Status: draft | approved
- PRD: [01-prd.md](01-prd.md)

## Summary
Two to four sentences. What changes and why.

## Current state
What the audited codebase does today, with file paths. Written from the
audit, not from the PRD's assumptions.

## Design
The chosen approach. For each significant change: what is added, what is
extended, what is eventually removed. Include schema/API sketches where
concrete. Follow the principles in references/idioms.md, and record the chosen
concrete patterns here (naming, error-handling style, test framework, migration
style) so the executor implements them instead of guessing — repo-derived by
default, or a recorded deviation with its reason. State the
re-execution semantics of every write path (endpoints under client retry,
webhooks, jobs, backfills): idempotent — by what key or guard — or at-most-once
and why that's safe for its caller.

## Behaviors
Observable outcomes, one line each: given <precondition>, when <action>,
then <observable result>. Include failure and re-execution rows (e.g.
"given the webhook was already processed, when it is redelivered, then
no duplicate side effect"). These become acceptance criteria in slice
cards and rows in table-driven tests.

## Alternatives considered
Brief. One alternative minimum — one you would defend, not a strawman —
with the reason it lost.

## Staging
How the change ships in stages (expand → migrate → contract), each stage
leaving the system working. This feeds Phase 2 slicing.

## Assumptions and open questions
- Assumption: <what was adopted and why it's safe>
- [ ] Open: <question> — <why it matters / default if unanswered>

## Coverage
One line per interrogation checklist item, each with a disposition:
- Scope: <answered — PRD §n | assumed — see assumptions #n | defaulted — ...>
- Edge cases: ...
- Idempotency and re-execution: ...
- Scale and limits: ...
- Data model and migrations: ...
- Observability: ...
- Security and access: ...
- Alternatives: see Alternatives considered

## Change log
Append-only. Every post-approval design change: date, what changed,
why, and re-approved (yes/no).
```

Rules for the template:

- Every assumption and open question carries a **default answer**. Unanswered questions must
  not block slicing; the default is used and noted in the plan.
- Assumptions are the audit- and default-resolved unknowns; open questions are the ones put
  to the user. If an open question's answer arrives and contradicts an assumption, resolve
  the contradiction before Phase 2 — not during execution.
- `Staging` is the bridge to Phase 2: each stage that can stand alone becomes one or more
  slices; stages that can't stand alone are sequencing constraints, not parallel work.
- `Stack` is mandatory, and `Design` must carry the concrete patterns — repo-derived from
the closest analogous code, or a recorded deviation with its reason. A missing stack pin
or a pattern-free Design leaves the executor guessing — an RFC defect.
- **Coverage is the proof the interrogation happened.** Every checklist item appears with a
  disposition pointing at its answer (PRD section, assumption number, or default). An item
  marked "considered" with no pointer is a defect — a traceless process didn't happen.
- **Behaviors must be testable as written.** If you cannot phrase the `then`, the design
  isn't concrete yet — go back to Design. Slice cards cite the Behaviors entries they
  implement; their table-driven tests map rows to those entries, so vague behaviors here
  become vague tests downstream.
- **Amendments never rewrite history.** After approval, the RFC changes only by appending
  to `## Change log`. A **material amendment** — changing a behavior, altering the data
  model, reordering staging — requires user re-approval before affected slices are dispatched
  again. Clarifications that change no behavior need no re-approval. Already-merged slices
  are facts: amend forward (a new slice), never retro-fit merged code to a new design.
